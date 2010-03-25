import os, sys
from itertools import izip
from optparse import OptionParser
import wemd
from wemd import Segment, WESimIter

from wemd.util.wetool import WECmdLineMultiTool
from wemd.environment import *
from wemd.sim_managers import make_sim_manager

from logging import getLogger
log = getLogger(__name__)
    
class WEMDAnlTool(WECmdLineMultiTool):
    def __init__(self):
        super(WEMDAnlTool,self).__init__()
        cop = self.command_parser
        
        cop.add_command('lstrajs',
                        'list trajectories',
                        self.cmd_lstrajs, True)
        cop.add_command('transanl',
                        'analyze transitions',
                        self.cmd_transanl, True)
        cop.add_command('fluxanl',
                        'analyze probability flux',
                        self.cmd_fluxanl, True)
        cop.add_command('tracetraj',
                        'trace an individual trajectory and report',
                        self.cmd_tracetraj, True)
        self.add_rc_option(cop)
        
    def add_iter_param(self, parser):
        parser.add_option('-i', '--iteration', dest='we_iter', type='int',
                          help = 'use trajectories as of iteration number '
                               + 'WE_ITER (default: the last complete '
                               + ' iteration)'
                         )
        
    def get_sim_manager(self):
        self.sim_manager = make_sim_manager(self.runtime_config)
        return self.sim_manager
        
    def get_sim_iter(self, we_iter):
        dbsession = self.sim_manager.data_manager.require_dbsession()
        if we_iter is None:
            we_iter = dbsession.execute(
'''SELECT MAX(n_iter) FROM we_iter 
     WHERE NOT EXISTS (SELECT * FROM segments 
                         WHERE segments.n_iter = we_iter.n_iter 
                           AND segments.status != :status)''',
                           params = {'status': Segment.SEG_STATUS_COMPLETE}).fetchone()[0]
        
        self.we_iter = dbsession.query(WESimIter).get([we_iter])
        return self.we_iter
            
    def cmd_lstrajs(self, args):
        parser = self.make_parser(description = 'list trajectories')
        self.add_iter_param(parser)
        parser.add_option('-t', '--type', dest='traj_type',
                          type='choice', choices=('live', 'complete', 
                                                  'merged', 'recycled', 'all'),
                          default='live',
                          help='''list all trajectories ("all"), those that are 
                               alive ("live", default), those that are complete
                               for any reason ("complete"), those that have been
                               terminated because of a merge ("merged"),
                               or those that have been terminated and
                               recycled ("recycled")''')
        (opts,args) = parser.parse_args(args)
        
        self.get_sim_manager()
        data_manager = self.sim_manager.data_manager
        we_iter = self.get_sim_iter(opts.we_iter)
        from wemd.core import Segment
        from sqlalchemy.sql import select
        dbsession = data_manager.require_dbsession()
        
        self.output_stream.write('# %s trajectories as of iteration %d:\n'
                                 % (opts.traj_type, we_iter.n_iter))
        self.output_stream.write('#%-11s    %-12s    %-21s\n'
                                 % ('seg_id', 'n_iter', 'weight'))
        segsel = select([Segment.seg_id, Segment.n_iter, Segment.weight])
        
        if opts.traj_type == 'live':
            query = segsel.where(Segment.n_iter == we_iter.n_iter)
        elif opts.traj_type == 'complete':
            query = segsel.where(Segment.n_iter <= we_iter.n_iter)\
                          .where(Segment.endpoint_type != Segment.SEG_ENDPOINT_TYPE_CONTINUATION)
            #query = segsel.where( (Segment.n_iter <= we_iter.n_iter)
            #                     &(Segment.endpoint_type != Segment.SEG_ENDPOINT_TYPE_CONTINUATION) )
        elif opts.traj_type == 'merged':
            query = segsel.where(Segment.n_iter <= we_iter.n_iter)\
                          .where(Segment.endpoint_type == Segment.SEG_ENDPOINT_TYPE_MERGED)
        elif opts.traj_type == 'recycled':
            query = segsel.where(Segment.n_iter <= we_iter.n_iter)\
                          .where(Segment.endpoint_type == Segment.SEG_ENDPOINT_TYPE_RECYCLED)
        elif opts.traj_type == 'all':
            query = segsel.where((Segment.n_iter == we_iter.n_iter)
                                  |((Segment.n_iter <= we_iter.n_iter)
                                    &(Segment.endpoint_type != Segment.SEG_ENDPOINT_TYPE_CONTINUATION)))

        query = query.order_by(Segment.n_iter)
        segments = dbsession.execute(query)
        for segment in segments:
            self.output_stream.write('%-12d     %-12d    %21.16g\n'
                                     % (segment.seg_id, segment.n_iter,
                                        segment.weight))
                
    def cmd_transanl(self, args):
        # Find transitions
        parser = self.make_parser()
        self.add_iter_param(parser)
        parser.add_option('-a', '--analysis-config', dest='anl_config',
                          help='use ANALYSIS_CONFIG as configuration file '
                              +'(default: analysis.cfg)')
        parser.add_option('-t', '--type', dest='traj_type', type='choice',
                          choices=('complete', 'recycled'),
                          default='complete',
                          help='''Analyze only recycled ("recycled")
                          trajectories, or all completed ("complete", default)
                          trajectories, which include both recycled and
                          merged particles''')
        parser.set_defaults(anl_config = 'analysis.cfg')
        (opts,args) = parser.parse_args(args)
        
        from wemd.util.config_dict import ConfigDict, ConfigError
        transcfg = ConfigDict({'data.squeeze': True,
                               'data.timestep.units': 'ps'})
        transcfg.read_config_file(opts.anl_config)
        transcfg.require('regions.edges')
        transcfg.require('regions.names')
        
        region_names = transcfg.get_list('regions.names')
        region_edges = [float(v) for v in transcfg.get_list('regions.edges')]
        if len(region_edges) != len(region_names) + 1:
            self.error_stream.write('region names and boundaries do not match\n')
            self.exit(EX_ERROR)
            
        try:
            translog = transcfg.get_file_object('output.transition_log', mode='w')
        except KeyError:
            translog = None 
        
        squeeze_data = transcfg.get_bool('data.squeeze')
        regions = []
        for (irr,rname) in enumerate(region_names):
            regions.append((rname, (region_edges[irr], region_edges[irr+1])))
            
        from sqlalchemy import select
        from wemd.analysis.transitions import AltOneDimTransitionEventFinder
        
        self.get_sim_manager()
        final_we_iter = self.get_sim_iter(opts.we_iter)
        max_iter = final_we_iter.n_iter
        data_manager = self.sim_manager.data_manager
        dbsession = data_manager.require_dbsession()
            
        import numpy
        
        event_durations = {}
        for irr1 in xrange(0, len(regions)):
            for irr2 in xrange(0, len(regions)):
                if abs(irr1-irr2) > 1:
                    event_durations[irr1,irr2] = numpy.empty((0,2), numpy.float64)                    
                    
        event_counts = numpy.zeros((len(regions), len(regions)), numpy.uint64)
        if opts.traj_type == 'complete':
            leafsel = select([Segment.seg_id]).where((Segment.n_iter <= max_iter)
                                                     &(Segment.endpoint_type != Segment.SEG_ENDPOINT_TYPE_CONTINUATION))
        elif opts.traj_type == 'recycled':
            leafsel = select([Segment.seg_id]).where((Segment.n_iter <= max_iter)
                                         &(Segment.endpoint_type == Segment.SEG_ENDPOINT_TYPE_RECYCLED))

        leaves = [row[0] for row in dbsession.execute(leafsel)]
        ntraj = len(leaves)
        
        blksize = 512
        
        for ileaf in xrange(0, ntraj, blksize):
            leaf_block = leaves[ileaf:ileaf+blksize]
            self.output_stream.write('retrieving block of %d trajectories\n'
                                     % len(leaf_block))
            trajs = data_manager.get_trajectories(leaf_block,
                                                  squeeze_data)
            for (itraj, traj) in enumerate(trajs):
        
                self.output_stream.write('analyzing trajectory %d of %d\n'
                                         % (ileaf+itraj+1, ntraj))
                try:
                    dt = traj.data[0]['dt']
                except KeyError:
                    dt = transcfg.get_float('data.timestep', 1.0)
                    
                trans_finder = AltOneDimTransitionEventFinder(regions,
                                                           traj.pcoord,
                                                           dt = dt,
                                                           traj_id=traj.seg_ids[-1],
                                                           weights = traj.weight,
                                                           transition_log = translog)
                trans_finder.identify_regions()
                trans_finder.identify_transitions()
                event_counts += trans_finder.event_counts
                
                for ((region1, region2), tfed_array) in trans_finder.event_durations.iteritems():
                    event_durations[region1, region2].resize((event_durations[region1, region2].shape[0] + tfed_array.shape[0], 2))
                    event_durations[region1, region2][-tfed_array.shape[0]:,:] = tfed_array[:,0:2]
                
        
        for ((region1, region2), ed_array) in event_durations.iteritems():
            region1_name = regions[region1][0]
            region2_name = regions[region2][0]
            if ed_array.shape[0] == 0:
                self.output_stream.write('No %s->%s transitions observed\n'
                                         % (region1_name, region2_name))
            else:
                self.output_stream.write('\nStatistics for %s->%s:\n'
                                         % (region1_name, region2_name))
                (ed_mean, ed_norm) = numpy.average(ed_array[:,0],
                                                   weights = ed_array[:,1],
                                                   returned = True)    
                self.output_stream.write('Number of events:    %d\n' % ed_array.shape[0])
                self.output_stream.write('ED average:          %g\n' % ed_array[:,0].mean())
                self.output_stream.write('ED weighted average: %g\n' % ed_mean)
                self.output_stream.write('ED min:              %g\n' % ed_array[:,0].min())
                self.output_stream.write('ED median:           %g\n' % ed_array[ed_array.shape[0]/2,0])
                self.output_stream.write('ED max:              %g\n' % ed_array[:,0].max())
                
                ed_file = open('ed_%s_%s.txt' % (region1_name, region2_name), 'wt')
                for irow in xrange(0, ed_array.shape[0]):
                    ed_file.write('%20.16g    %20.16g\n'
                                  % tuple(ed_array[irow,0:2]))
                ed_file.close()
                    
            
    def cmd_tracetraj(self, args):
        parser = self.make_parser('TRAJ_ID', description = 'trace trajectory '
                                  +'path')
        (opts, args) = parser.parse_args(args)
        if len(args) != 1:
            parser.print_help(self.error_stream)
            self.exit(EX_USAGE_ERROR)
        
        self.get_sim_manager()
        seg = self.sim_manager.data_manager.get_segment(None, long(args[0]))
        segs = [seg]
        while seg.p_parent and seg.n_iter:
            seg = seg.p_parent
            segs.append(seg)
        traj = list(reversed(segs))
        
        for seg in traj:
            self.output_stream.write('%5d  %10d  %10d  %21.16g  %21.16g\n'
                                     % (seg.n_iter,
                                        seg.seg_id,
                                        seg.p_parent_id or 0,
                                        seg.weight,
                                        seg.pcoord[-1]))
    def cmd_fluxanl(self, args):
        parser = self.make_parser()
        parser.add_option('-T', '--tau', dest='tau', type='float',
                          default=1.0,
                          help='length of each WE iteration in simulation '
                              +'time is TAU (default: 1.0)')
        (opts,args) = parser.parse_args(args)
        sim_manager = self.get_sim_manager()
        latest_we_iter = self.get_sim_iter(None)


        import numpy
        fluxen = numpy.zeros((latest_we_iter.n_iter,), numpy.float64)
        for i_iter in xrange(1, latest_we_iter.n_iter+1):
            we_iter = sim_manager.data_manager.get_we_sim_iter(i_iter)
            fluxen[i_iter-1] = we_iter.data['recycled_population']
            
        fluxen /= opts.tau
        
        for irow in xrange(0, fluxen.shape[0]):
            self.output_stream.write('%-8d    %16.12g    %21.16g\n'
                                     % (irow+1, opts.tau*(irow+1), 
                                        fluxen[irow]))
        
            
            
            
        
        
        
if __name__ == '__main__':
    WEMDAnlTool().run()

