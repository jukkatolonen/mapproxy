# This file is part of the MapProxy project.
# Copyright (C) 2010 Omniscale <http://omniscale.de>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement, division
import sys
import re
import math
import time
import yaml
import datetime
import multiprocessing
from functools import partial

from mapproxy.core.srs import SRS
from mapproxy.core import seed
from mapproxy.core.grid import MetaGrid, bbox_intersects, bbox_contains
from mapproxy.core.cache import TileSourceError
from mapproxy.core.utils import cleanup_directory
from mapproxy.core.config import base_config, load_base_config, abspath


try:
    import shapely.wkt
    import shapely.prepared 
    import shapely.geometry
except ImportError:
    shapely_present = False
else:
    shapely_present = True


NONE = 0
CONTAINS = -1
INTERSECTS = 1

"""
>>> g = grid.TileGrid()
>>> seed_bbox = (-20037508.3428, -20037508.3428, 20037508.3428, 20037508.3428)
>>> seed_level = 2, 4
>>> seed(g, seed_bbox, seed_level)

"""

class SeedPool(object):
    """
    Manages multiple SeedWorker.
    """
    def __init__(self, cache, size=2, dry_run=False):
        self.tiles_queue = multiprocessing.Queue(32)
        self.cache = cache
        self.dry_run = dry_run
        self.procs = []
        for _ in xrange(size):
            worker = SeedWorker(cache, self.tiles_queue, dry_run=dry_run)
            worker.start()
            self.procs.append(worker)
    
    def seed(self, tiles, progress):
        self.tiles_queue.put((tiles, progress))
    
    def stop(self):
        for _ in xrange(len(self.procs)):
            self.tiles_queue.put((None, None))
        
        for proc in self.procs:
            proc.join()

class SeedWorker(multiprocessing.Process):
    def __init__(self, cache, tiles_queue, dry_run=False):
        multiprocessing.Process.__init__(self)
        self.cache = cache
        self.tiles_queue = tiles_queue
        self.dry_run = dry_run
    def run(self):
        while True:
            tiles, progress = self.tiles_queue.get()
            if tiles is None:
                return
            print '[%s] %6.2f%% %s\r' % (timestamp(), progress[1]*100, progress[0]),
            sys.stdout.flush()
            if not self.dry_run:
                load_tiles = lambda: self.cache.cache_mgr.load_tile_coords(tiles)
                seed.exp_backoff(load_tiles, exceptions=(TileSourceError, IOError))

class Seeder(object):
    def __init__(self, cache, task, seed_pool):
        self.cache = cache
        self.task = task
        self.seed_pool = seed_pool
        
        num_seed_levels = task.max_level - task.start_level + 1
        self.report_till_level = task.start_level + int(num_seed_levels * 0.7)
        self.grid = MetaGrid(cache.grid, meta_size=base_config().cache.meta_size)
        self.progress = 0.0
        self.start_time = time.time()
    
    def seed(self):
        self._seed(self.task.bbox, self.task.start_level)
            
    def _seed(self, cur_bbox, level, progess_str='', progress=1.0, all_subtiles=False):
        """
        :param cur_bbox: the bbox to seed in this call
        :param level: the current seed level
        :param all_subtiles: seed all subtiles and do not check for
                             intersections with bbox/geom
        """
        bbox_, tiles_, subtiles = self.grid.get_affected_level_tiles(cur_bbox, level)
        subtiles = list(subtiles)
        if level <= self.report_till_level:
            print '[%s] %2s %s' % (timestamp(), level, format_bbox(cur_bbox))
            sys.stdout.flush()
        
        if level < self.task.max_level:
            sub_seeds = self._sub_seeds(subtiles, all_subtiles)
            progress = progress / len(sub_seeds)
            if sub_seeds:
                total_sub_seeds = len(sub_seeds)
                for i, (sub_bbox, intersection) in enumerate(sub_seeds):
                    cur_progess_str = progess_str + status_symbol(i, total_sub_seeds)
                    all_subtiles = True if intersection == CONTAINS else False
                    self._seed(sub_bbox, level+1, cur_progess_str,
                               all_subtiles=all_subtiles, progress=progress)
        else:
            self.progress += progress
        self.seed_pool.seed(subtiles, (progess_str, self.progress))

    def _sub_seeds(self, subtiles, all_subtiles):
        """
        Return all sub tiles that intersect the 
        """
        sub_seeds = []
        for subtile in subtiles:
            if subtile is None: continue
            sub_bbox = self.grid.meta_bbox(subtile)
            intersection = CONTAINS if all_subtiles else self.task.intersects(sub_bbox)
            if intersection:
                sub_seeds.append((sub_bbox, intersection))
        return sub_seeds


class CacheSeeder(object):
    """
    Seed multiple caches with the same option set.
    """
    def __init__(self, caches, remove_before, progress_meter, dry_run=False, concurrency=2):
        self.remove_before = remove_before
        self.progress = progress_meter
        self.dry_run = dry_run
        self.caches = caches
        self.concurrency = concurrency
    
    def seed_view(self, bbox, level, srs, cache_srs, geom=None):
        for cache in self.caches:
            if not cache_srs or cache.grid.srs in cache_srs:
                if self.remove_before:
                    cache.cache_mgr.expire_timestamp = lambda tile: self.remove_before
                seed_pool = SeedPool(cache, dry_run=self.dry_run, size=self.concurrency)
                seed_task = SeedTask(bbox, level, srs, cache.grid.srs, geom)
                seeder = Seeder(cache, seed_task, seed_pool)
                seeder.seed()
                seed_pool.stop()
    
    def cleanup(self):
        for cache in self.caches:
            for i in range(cache.grid.levels):
                level_dir = cache.cache_mgr.cache.level_location(i)
                if self.dry_run:
                    def file_handler(filename):
                        self.progress.print_msg('removing ' + filename)
                else:
                    file_handler = None
                self.progress.print_msg('removing oldfiles in ' + level_dir)
                cleanup_directory(level_dir, self.remove_before,
                    file_handler=file_handler)

class SeedTask(object):
    def __init__(self, bbox, level, bbox_srs, seed_srs, geom=None):
        self.start_level = level[0]
        self.max_level = level[1]
        self.bbox_srs = bbox_srs
        self.seed_srs = seed_srs
    
        if bbox_srs != seed_srs:
            if geom is not None:
                geom = transform_geometry(bbox_srs, seed_srs, geom)
                bbox = geom.bounds
                geom = shapely.prepared.prep(geom)
            else:
                bbox = bbox_srs.transform_bbox_to(seed_srs, bbox)
        
        self.bbox = bbox
        self.geom = geom
        
        if geom is not None:
            self.intersects = self._geom_intersects
        else:
            self.intersects = self._bbox_intersects
    

    def _geom_intersects(self, bbox):
        bbox_poly = shapely.geometry.Polygon((
            (bbox[0], bbox[1]),
            (bbox[2], bbox[1]),
            (bbox[2], bbox[3]),
            (bbox[0], bbox[3]),
            ))
        if self.geom.contains(bbox_poly): return CONTAINS
        if self.geom.intersects(bbox_poly): return INTERSECTS
        return NONE
    
    def _bbox_intersects(self, bbox):
        if bbox_contains(self.bbox, bbox): return CONTAINS
        if bbox_intersects(self.bbox, bbox): return INTERSECTS
        return NONE


def timestamp():
    return datetime.datetime.now().strftime('%H:%M:%S')

def format_bbox(bbox):
    return ('(%.5f, %.5f, %.5f, %.5f)') % bbox

def status_symbol(i, total):
    """
    >>> status_symbol(0, 1)
    '0'
    >>> [status_symbol(i, 4) for i in range(5)]
    ['.', 'o', 'O', '0', 'X']
    >>> [status_symbol(i, 10) for i in range(11)]
    ['.', '.', 'o', 'o', 'o', 'O', 'O', '0', '0', '0', 'X']
    """
    symbols = list(' .oO0')
    i += 1
    if 0 < i > total:
        return 'X'
    else:
        return symbols[int(math.ceil(i/(total/4)))]

def seed_from_yaml_conf(conf_file, verbose=True, rebuild_inplace=True, dry_run=False,
    concurrency=2):
    from mapproxy.core.conf_loader import load_services
    
    if hasattr(conf_file, 'read'):
        seed_conf = yaml.load(conf_file)
    else:
        with open(conf_file) as conf_file:
            seed_conf = yaml.load(conf_file)
    
    if verbose:
        progress_meter = seed.TileProgressMeter
    else:
        progress_meter = seed.NullProgressMeter
    
    services = load_services()
    if 'wms' in services:
        server  = services['wms']
    elif 'tms' in services:
        server  = services['tms']
    else:
        print 'no wms or tms server configured. add one to your proxy.yaml'
        return
    for layer, options in seed_conf['seeds'].iteritems():
        remove_before = seed.before_timestamp_from_options(options)
        caches = caches_from_layer(server.layers[layer])
        seeder = CacheSeeder(caches, remove_before=remove_before,
                            progress_meter=progress_meter(), dry_run=dry_run,
                            concurrency=concurrency)
        for view in options['views']:
            view_conf = seed_conf['views'][view]
            if 'ogr_datasource' in view_conf:
                check_shapely()
                srs = view_conf['ogr_srs']
                datasource = view_conf['ogr_datasource']
                if not re.match(r'^\w{2,}:', datasource):
                    # looks like a file and not PG:, MYSQL:, etc
                    # make absolute path
                    datasource = abspath(datasource)
                where = view_conf.get('ogr_where', None)
                bbox, geom = load_datasource(datasource, where)
            elif 'polygons' in view_conf:
                check_shapely()
                srs = view_conf['polygons_srs']
                poly_file = abspath(view_conf['polygons'])
                bbox, geom = load_polygons(poly_file)
            else:
                srs = view_conf.get('bbox_srs', None)
                bbox = view_conf.get('bbox', None)
                geom = None
            
            cache_srs = view_conf.get('srs', None)
            if cache_srs is not None:
                cache_srs = [SRS(s) for s in cache_srs]
            if srs is not None:
                srs = SRS(srs)
            level = view_conf.get('level', None)
            assert len(level) == 2
            seeder.seed_view(bbox, level=level, srs=srs, 
                             cache_srs=cache_srs, geom=geom)
        
        if remove_before:
            seeder.cleanup()

def check_shapely():
    if not shapely_present:
        raise ImportError('could not import shapley.'
            ' required for polygon/ogr seed areas')

def caches_from_layer(layer):
    caches = []
    if hasattr(layer, 'layers'): # MultiLayer
        layers = layer.layers
    else:
        layers = [layer]
    for layer in layers:
        if hasattr(layer, 'sources'): # VLayer
            caches.extend([source.cache for source in layer.sources
                                if hasattr(source, 'cache')])
        else:
            caches.append(layer.cache)
    return caches

def load_datasource(datasource, where=None):
    from mapproxy.core.ogr_reader import OGRShapeReader
    
    polygons = []
    for wkt in OGRShapeReader(datasource).wkts(where):
        polygons.append(shapely.wkt.loads(wkt))
        
    mp = shapely.geometry.MultiPolygon(polygons)
    return mp.bounds, mp

def load_polygons(geom_file):
    polygons = []
    with open(geom_file) as f:
        for line in f:
            polygons.append(shapely.wkt.loads(line))
    
    mp = shapely.geometry.MultiPolygon(polygons)
    return mp.bounds, mp

def transform_geometry(from_srs, to_srs, geometry):
    transf = partial(transform_xy, from_srs, to_srs)
    
    if geometry.type == 'Polygon':
        return transform_polygon(transf, geometry)
    
    if geometry.type == 'MultiPolygon':
        return transform_multipolygon(transf, geometry)

def transform_polygon(transf, polygon):
    ext = transf(polygon.exterior.xy)
    ints = [transf(ring.xy) for ring in polygon.interiors]
    return shapely.geometry.Polygon(ext, ints)

def transform_multipolygon(transf, multipolygon):
    transformed_polygons = []
    for polygon in multipolygon:
        transformed_polygons.append(transform_polygon(transf, polygon))
    return shapely.geometry.MultiPolygon(transformed_polygons)


def transform_xy(from_srs, to_srs, xy):
    return list(from_srs.transform_to(to_srs, zip(*xy)))

def load_config(conf_file=None):
    if conf_file is not None:
        load_base_config(conf_file)

def set_service_config(conf_file=None):
    if conf_file is not None:
        base_config().services_conf = conf_file

def main():
    from optparse import OptionParser
    usage = "usage: %prog [options] seed_conf"
    parser = OptionParser(usage)
    parser.add_option("-q", "--quiet",
                      action="store_false", dest="verbose", default=True,
                      help="don't print status messages to stdout")
    parser.add_option("-f", "--proxy-conf",
                      dest="conf_file", default=None,
                      help="proxy configuration")
    parser.add_option("-c", "--concurrency", type="int",
                      dest="concurrency", default=2,
                      help="number of parallel seed processes")
    parser.add_option("-s", "--services-conf",
                      dest="services_file", default=None,
                      help="services configuration")
    parser.add_option("-n", "--dry-run",
                      action="store_true", dest="dry_run", default=False,
                      help="do not seed, just print output")    
    
    (options, args) = parser.parse_args()
    if len(args) != 1:
        parser.error('missing seed_conf file as last argument')
    
    if not options.conf_file:
        parser.error('set proxy configuration with -f')
    
    load_config(options.conf_file)
    set_service_config(options.services_file)
    
    seed_from_yaml_conf(args[0], verbose=options.verbose,
                        dry_run=options.dry_run, concurrency=options.concurrency)

if __name__ == '__main__':
    main()