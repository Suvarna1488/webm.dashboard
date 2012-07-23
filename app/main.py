#!/usr/bin/env python
##  Copyright (c) 2012 The WebM project authors. All Rights Reserved.
##
##  Use of this source code is governed by a BSD-style license
##  that can be found in the LICENSE file in the root of the source
##  tree. An additional intellectual property rights grant can be found
##  in the file PATENTS.  All contributing project authors may
##  be found in the AUTHORS file in the root of the source tree.
##

# Setup django to silence deprecation warning for 0.96
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'settings'
from google.appengine.dist import use_library
use_library('django', '1.2')

from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp import util
from google.appengine.ext import db

import datetime
import hashlib
import json
import pickle
import StringIO

import urllib

from dbdefine import *
from drilldown import drilldown

import curve_compare

class ImportFileSetHandler(webapp.RequestHandler):
    def post(self):
        files_added = {}
        data = StringIO.StringIO(self.request.get("data"))
        for line in data:
            data = json.loads(line)

            # We first load the fileset into the database
            # For use later, we also add a list of filenames in the fileset
            f = FileSet(key_name=data["name"],
                        display_name=data["name"],
                        files=data["setfiles"])
            f.put()

            for filename in data["setfiles"]:
                if filename not in files_added:
                    files_added[filename] = [data["name"]]
                else:
                    files_added[filename].append(data["name"])

        # We now update the database with the elements in files_added
        for filename in files_added:
            # TODO: Is there a better way of assigning display names?
            split_index = filename.rfind("_")
            File(key_name=filename,
                 display_name=filename[:split_index],
                 file_sets=files_added[filename]).put()

class ImportCommitHandler(webapp.RequestHandler):
    def convert_time(self, time, zone):
        class GitTZInfo(datetime.tzinfo):
            def __init__(self, utcoffset):
                self.offset = utcoffset

            def dst(self, dt):
                return datetime.timedelta(0)

            def utcoffset(self, dt):
                return datetime.timedelta(seconds=self.offset)
        return datetime.datetime.fromtimestamp(time, GitTZInfo(zone))

    def load(self, data):
      author_time = self.convert_time(data["author_time"],
                                      data["author_timezone"])
      commit_time = self.convert_time(data["commit_time"],
                                      data["commit_timezone"])
      c = Commit(key_name=data["id"],
                 author=data["author"],
                 author_time=author_time,
                 committer=data["committer"],
                 commit_time=commit_time,
                 message=data["message"])
      c.put()

    def post(self):
        data = StringIO.StringIO(self.request.get("data"))
        for line in data:
            self.load(json.loads(line))


class ImportCodecMetricHandler(webapp.RequestHandler):
    def put_metric_index(self, parent, metrics, files):
        if metrics and files:
            metric_list = list(metrics)
            file_list = list(files)

            h = hashlib.sha1()
            h.update(parent.key().name())
            h.update(parent.commit)
            h.update(parent.config_name)
            map(h.update, metric_list)
            map(h.update, file_list)
            CodecMetricIndex(key_name=h.hexdigest(),
                             parent=parent,
                             commit=parent.commit,
                             config_name=parent.config_name,
                             metrics=metric_list,
                             files=file_list).put()

    def update_drilldown(self, parent, metrics, files):
        # TODO(jkoleszar): if drilldown is moved to a backend, maybe post this
        # through a task queue.
        commit = set([parent.commit])
        config = set([parent.config_name])
        drilldown.insert(metrics, set(config), files, set(commit))

    def post(self):
        for line in StringIO.StringIO(self.request.get("data")):
            # Key off a hash of the input line to make the import idempotent
            key = hashlib.sha1(line).hexdigest()

            # Deserialize data, populating optional fields
            data = {"config_flags": None,
                    "runtime_flags": None
                   }
            data.update(json.loads(line))

            # Put the data
            m = CodecMetric(key_name=key,
                            commit=data["commit"],
                            config_flags=data["config_flags"],
                            runtime_flags=data["runtime_flags"],
                            config_name=data["config"],
                            data=data["data"])
            m.put()

            # Build indexes
            metrics = set()
            files = set()
            for filename, metric_entries in data["data"].iteritems():
                for metric_entry in metric_entries:
                    this_metrics = set(metric_entry.keys())
                    if this_metrics != metrics:
                        self.put_metric_index(m, metrics, files)
                        self.update_drilldown(m, metrics, files)
                        metrics = this_metrics
                        files = set()
                    files.add(filename)
            self.put_metric_index(m, metrics, files)
            self.update_drilldown(m, metrics, files)
        drilldown.save()

def pretty_json(x):
    return json.dumps(x, indent=2, sort_keys=True)


def CodecMetricFetch(metric, config, filename, commit):
    '''This function fetches the data for a given metric, config, filename,
    commit tuple. This functionality is used multiple places, such as
    CodecMetricHandler and AverageImprovementHandler.'''
    indexes = CodecMetricIndex.all(keys_only = True)
    indexes = indexes.filter('metrics =', metric)
    indexes = indexes.filter('config_name =', config)
    indexes = indexes.filter('files =', filename)
    indexes = indexes.filter('commit =', commit)
    keys = [k.parent() for k in indexes]

    result=[]
    for cm in db.get(keys):
        for run in cm.data[filename]:
            this_run_data = []

            # TODO(jkoleszar): How do we handle this properly?
            if "Bitrate" in run:
                this_run_data.append(run["Bitrate"])

            this_run_data.append(run[metric])
            result.append(this_run_data)

    # Sanity checks
    for r in result[1:]:
        assert len(r) == len(result[0])

    # Result is a list of lists. Sort by the first element of the nested
    # list.
    result = sorted(result, key=lambda x:x[0])
    return result


class CodecMetricHandler(webapp.RequestHandler):
    def get(self, metric, config, filename, commit):
        """Fetches the requested metric data as JSON"""

        result = CodecMetricFetch(metric, config, filename, commit)

        # Return the result
        if result:
            self.response.headers['Content-Type'] = 'application/json'
            self.response.out.write(pretty_json(result))
        else:
            self.error(404)

class AverageImprovementHandler(webapp.RequestHandler):
    def get(self, metrics, configs, filenames, commits):
        """Calculates the requested composite metrics and outputs as JSON"""
        def split_field(field):
            if field:
                for f in urllib.unquote(field).split(","):
                    yield f
            else:
                yield None

        def field_list(field):
            '''Returns the field as a list of strings.'''
            result = urllib.unquote(field).split(",")
            if len(result[0]) == 0:
                return None
            return result

        # We first get a list of the filesets that we need to calculate data
        # for (and the filenames they contain)
        all_sets = set([])
        filename_list = field_list(filenames)
        if filename_list is not None:
            files = File.get_by_key_name(filename_list)
            for f in files:
                filesets = f.file_sets
                all_sets.update(filesets)

        filenames = set([])
        all_sets = list(all_sets)
        if len(all_sets) > 0:
            file_sets = FileSet.get_by_key_name(all_sets)
            for fs in file_sets:
                if fs.display_name == "All":
                    continue
                filenames.update(fs.files)

        result = []
        # For each file, we compare it with the baseline (for each set up)
        for m in split_field(metrics):
            for cfg in split_field(configs):
                for cm in split_field(commits):
                    col = [] # Each m, cfg, cm combination will be a column in
                             # the table
                    for f in filenames:
                        data = CodecMetricFetch(m, cfg, f, cm)

                        # We get the baseline data
                        base_data = [ [ 195.81200000000001, 6.4710957722174287 ],
                                      [ 241.80500000000001, 6.4482847562548358 ],
                                      [ 293.089, 5.4961160779715668 ],
                                      [ 339.92899999999997, 6.4915393603669882 ],
                                      [ 387.298, 5.1897726879562676 ],
                                      [ 435.745, 5.9512001586986711 ],
                                      [ 479.077, 6.1462814996926856 ] ]

                        composite = curve_compare.DataBetter(base_data, data)
                        col.append([f, composite])
                    if m and cfg and cm:
                        result.append({'col': m+ "/" + cfg + "/" + cm[:9],
                                       'data': col})
        # return the results
        #result = list(filenames)
        self.response.out.write(pretty_json(result))

class MainHandler(webapp.RequestHandler):
    def get(self):
        values = {} # This is the dictionary of template values passed to html
        self.response.out.write(template.render("index.html", values))

class ChartHandler(webapp.RequestHandler):
    def get(self):
        self.response.out.write(template.render("graph.html", {}))

def main():
    application = webapp.WSGIApplication([
        ('/', MainHandler),
        ('/import-commits', ImportCommitHandler),
        ('/import-filesets', ImportFileSetHandler),
        ('/import-codec-metrics', ImportCodecMetricHandler),
        (r'/metric-data/(.*)/(.*)/(.*)/(.*)', CodecMetricHandler),
        (r'/average-improvement/(.*)/(.*)/(.*)/(.*)', AverageImprovementHandler),
        ('/graph', ChartHandler)
    ], debug=True)
    util.run_wsgi_app(application)


if __name__ == '__main__':
    main()
