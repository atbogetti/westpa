'''A system for parallel, remote execution of multiple arbitrary tasks.
Much of this, both in concept and execution, was inspired by (and in some cases based heavily on) the
``concurrent.futures`` package from Python 3.2 (thanks to Brian Quinlan and his futures implementation).
'''

import cPickle as pickle
__metaclass__ = type

import logging
import sys, time, uuid, threading
from collections import deque
from contextlib import contextmanager
log = logging.getLogger(__name__)

import wemd

class WEMDWorkManager:
    def __init__(self):
        self.shutdown_called = False
        self.mode = 'master'
        
        # Active requests (those whose results have not been received)
        self.active = dict()
                                
    def _register_future(self, future):
        '''Record a future whose result should be pending.'''
        # Dict assignment is atomic; no lock required
        self.active[future.task_id] = future
                
    def _remove_future(self, future):
        '''Remove a future from the list of those pending results'''
        assert future.done
        # No lock required, since dict.pop is an atomic operation (implemented as a C func)
        self.active.pop(future.task_id, None)
        
    def _receive_result_into(self, future, result):
        future._set_result(result)
        self._remove_future(future)

    def _receive_exception_into(self, future, exception):
        future._set_exception(exception)
        self._remove_future(future)
                        
    def parse_aux_args(self, aux_args, do_help = False):
        '''Parse any unprocessed command-line arguments, returning any arguments not proccessed
        by this object. By default, this does nothing except return the input.'''
        return aux_args
    
    def startup(self):
        '''Perform any necessary startup work, such as spawning clients'''
        pass
                                    
#    def prepare_iteration(self, n_iter, segments):
#        '''Prepare this work manager to run a new iteration. If overridden, this
#        must call propagator.prepare_iteration().'''
#        self.n_iter = n_iter
#        self.propagator.prepare_iteration(n_iter, segments)
#                        
#    def propagate(self, segments):
#        '''Propagate the given segments.'''
#        raise NotImplementedError
#    
#    def finalize_iteration(self, n_iter, segments):
#        '''Clean up at the end of an iteration.  If overridden, this must call
#        propagator.finalize_iteration().'''
#        self.propagator.finalize_iteration(n_iter, segments)
        
    def shutdown(self, exit_code=0):
        '''Cleanly shut down any active workers.'''
        pass
    
    def run_worker(self):
        raise NotImplementedError('this work manager does not support dedicated workers; run as master')
    
    def submit(self, fn, *args, **kwargs):
        raise NotImplementedError
    
    def as_completed(self, futures):
        pending = set(futures)
        
        # See which futures have results, and install a watcher on those that do not
        with WMFuture.all_acquired(pending):
            completed = {future for future in futures if future.done}
            pending -= completed
            watcher = FutureWatcher(pending, threshold=1)
    
        # Yield available results immediately
        for future in completed:
            yield future
        del completed
        
        # Wait on any remaining results
        while pending:
            watcher.wait()
            completed = watcher.reset()
            for future in completed:
                yield future
                pending.remove(future)
    
    def wait_any(self, futures):
        pending = set(futures)
        with WMFuture.all_acquired(pending):
            completed = {future for future in futures if future.done}
            
            if completed:
                # If any futures are complete, then we don't need to do anything else
                return completed.pop()
            else:
                # Otherwise, we need to install a watcher
                watcher = FutureWatcher(futures, threshold = 1)
        
        watcher.wait()
        completed = watcher.reset()
        return completed.pop()        
            
    def wait_all(self, futures):
        '''A convenience function which waits on all the given futures, then returns a list of the results.'''
        results = []
        for future in futures:
            results.append(future.result)
        return results

class FutureWatcher:
    def __init__(self, futures, threshold = 1):
        self.event = threading.Event()
        self.lock = threading.RLock()
        self.threshold = threshold
        self.completed = []
        
        for future in futures:
            future._add_watcher(self)
        
    def signal(self, future):
        '''Signal this watcher that the given future has results available. If this 
        brings the number of available futures above signal_threshold, this watcher's
        event object will be signalled as well.'''
        with self.lock:
            self.completed.append(future)
            if len(self.completed) >= self.threshold:
                self.event.set()
            assert self not in future._watchers
                
    def wait(self):
        return self.event.wait()
            
    def reset(self):
        '''Reset this watcher's list of completed futures, returning the list of completed futures
        prior to resetting it.''' 
        with self.lock:
            self.event.clear()
            completed = self.completed
            self.completed = []
            return completed
                    
class WMFuture:
    
    @contextmanager
    @staticmethod
    def all_acquired(futures):
        futures = list(futures)
        for future in futures:
            future._condition.acquire()
            
        yield # to contents of "with" block
        
        for future in futures:
            future._condition.release()
    
    def __init__(self, task_id=None):
        self.task_id = task_id or uuid.uuid4()

        self._condition = threading.Condition()
        self._done = False
        self._result = None
        self._exception = None

        # a set of Events representing who is waiting on results from this future
        # this set will be cleared after the result is updated and watchers are notified        
        self._watchers = set()
        
        # a set of functions that will be called with this future as an argument when it is updated with a
        # result. This list will be cleared after the result is updated and all callbacks invoked
        self._update_callbacks = []  
                        
    def __repr__(self):
        return '<WMFuture 0x{id:x}: {self.task_id!s}>'.format(id=id(self), self=self)

    def _notify_watchers(self):
        '''Notify all watchers that this future has been updated, then deletes the list of update watchers.'''
        with self._condition:
            assert self._done
            for watcher in self._watchers:
                watcher.signal(self)
            self._watchers.clear()

    def _invoke_callbacks(self):
        with self._condition:
            for callback in self._update_callbacks:
                try:
                    callback(self)
                except Exception:
                    # This may need to be a simple print to stderr, depending on the locking
                    # semantics of the logger.
                    log.exception('ignoring exception in result callback')
            self._update_callbacks.clear()
    
    def _add_watcher(self, watcher):
        '''Add the given update watcher  to the internal list of watchers. If a result is available,
        returns immediately without updating the list of watchers.'''
        with self._condition:
            if self._done:
                watcher.signal(self)
                return
            else:
                self._watchers.add(watcher)
                
    def _add_callback(self, callback):
        '''Add the given update callback to the internal list of callbacks. If a result is available,
        invokes the callback immediately without updating the list of callbacks.'''
        with self._condition:
            if self._done:
                try:
                    callback(self)
                except Exception:
                    log.exception('ignoring exception in result callback')
            else:
                self._update_callbacks.append(callback)
                
    def _set_result(self, result):
        with self._condition:
            self._result = result
            self._returned = True
            self._condition.notify_all()
        self._invoke_callbacks()
        self._notify_watchers()
        for cb in self._done_callbacks:
            try:
                cb(self)
            except Exception:
                log.exception('ignoring exception in result callback')
        
    def _set_exception(self, exception):
        with self._condition:
            self._exception = exception
            self._returned = True
            self._condition.notify_all()
        for cb in self._done_callbacks:
            try:
                cb(self)
            except Exception:
                log.exception('ignoring exception in result callback')
    
    def get_result(self):
        with self._condition:
            if self._done:
                if self._exception:
                    raise self._exception
                else:
                    return self._result
            else:
                self._condition.wait()
                assert self._done
                if self._exception:
                    raise self._exception
                else:
                    return self._result
    result = property(get_result, None, None, 
                      'Get the result associated with this future (may block if this future is being updated).')
    
    def get_exception(self):
        with self._condition:
            if self._returned:
                return self._exception
            else:
                assert self._done
                self._condition.wait()
                return self._exception
    exception = property(get_exception, None, None, 
                         'Get the exception associated with this future (may block if this future is being updated).')            
        
    def is_done(self):
        with self._condition:
            return self._returned
    done = property(is_done, None, None, 
                    'Indicates whether this future is done executing (may block if this future is being updated).')    
    
import serial, zeromq

