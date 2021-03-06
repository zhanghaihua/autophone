# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import logging
import os
import sqlite3
import time

logger = logging.getLogger('autophone.jobs')

class Jobs(object):

    MAX_ATTEMPTS = 3
    SQL_RETRY_DELAY = 60
    SQL_MAX_RETRIES = 10

    def __init__(self, mailer, default_device=None):
        self.mailer = mailer
        self.default_device = default_device
        self.filename = 'jobs.sqlite'
        if not os.path.exists(self.filename):
            conn = self._conn()
            c = conn.cursor()
            c.execute('create table jobs '
                      '(created text, last_attempt text, build_url text, '
                      'attempts int, device text)')
            conn.commit()

    def report_sql_error(self, attempt, email_sent,
                         email_subject, email_body,
                         exception_message):
        if attempt > self.SQL_MAX_RETRIES and not email_sent:
            email_sent = True
            self.mailer.send(email_subject, email_body)
            logger.info('Sent mail notification about jobs database sql error.')
            logger.exception(exception_message)
            time.sleep(self.SQL_RETRY_DELAY)
        return email_sent

    def _conn(self):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                return sqlite3.connect(self.filename)
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to connect to jobs '
                                                   'database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to connect '
                                                   'to jobs database. Waiting '
                                                   'for %d seconds.' %
                                                   (attempt,
                                                    self.SQL_RETRY_DELAY))

    def clear_all(self):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                conn.cursor().execute('delete from jobs')
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to clear all jobs in '
                                                   'jobs database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to delete '
                                                   'all jobs database. Waiting '
                                                   'for %d seconds.' %
                                                   (attempt,
                                                    self.SQL_RETRY_DELAY))

    def new_job(self, build_url, device=None):
        if not device:
            device = self.default_device
        now = datetime.datetime.now().isoformat()
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                conn.cursor().execute('insert into jobs values (?, ?, ?, 0, ?)',
                                      (now, None, build_url, device))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to insert job into '
                                                   'jobs database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to insert '
                                                   'job (%s, %s, %s, %s) into '
                                                   'database. Waiting for %d '
                                                   'seconds.' %
                                                   (attempt, now, None,
                                                    build_url, device,
                                                    self.SQL_RETRY_DELAY))

    def jobs_pending(self, device=None):
        if not device:
            device = self.default_device
        try:
            count = self._conn().cursor().execute(
                'select count(ROWID) from jobs where device=?',
                (device,)).fetchone()[0]
        except sqlite3.OperationalError:
            count = 0
        return count

    def get_next_job(self, device=None):
        if not device:
            device = self.default_device
        try:
            conn = self._conn()
            c = conn.cursor()
            c.execute('delete from jobs where device=? and attempts>=?',
                      (device, self.MAX_ATTEMPTS))
            conn.commit()
            jobs = [{'id': job[0],
                     'created': job[1],
                     'last_attempt': job[2],
                     'build_url': job[3],
                     'attempts': job[4]}
                    for job in c.execute(
                    'select ROWID as id,created,last_attempt,build_url,attempts'
                    ' from jobs where device=? order by created desc', (device,))]
            if not jobs:
                return None
            next_job = jobs[0]
            next_job['attempts'] += 1
            next_job['last_attempt'] = datetime.datetime.now().isoformat()
            c.execute('update jobs set attempts=?, last_attempt=? where ROWID=?',
                      (next_job['attempts'], next_job['last_attempt'],
                       next_job['id']))
            conn.commit()
        except sqlite3.OperationalError:
            next_job = None
        return next_job

    def job_completed(self, job_id):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = self._conn()
                c = conn.cursor()
                c.execute('delete from jobs where ROWID=?', (job_id,))
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   'Unable to delete completed '
                                                   'job from jobs database.',
                                                   'Please check the logs for '
                                                   'full details.',
                                                   'Attempt %d failed to delete '
                                                   'completed job %s from '
                                                   'database. Waiting for %d '
                                                   'seconds.' %
                                                   (attempt, job_id,
                                                    self.SQL_RETRY_DELAY))
