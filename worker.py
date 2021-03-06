# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import datetime
import jobs
import logging
import multiprocessing
import os
import posixpath
import socket
import sys
import tempfile
import time
import traceback

import buildserver
import phonetest
from logdecorator import LogDecorator
from mozdevice import DroidSUT, DMError
from multiprocessinghandlers import MultiprocessingTimedRotatingFileHandler
from options import *


class Crashes(object):

    CRASH_WINDOW = 30
    CRASH_LIMIT = 5

    def __init__(self, crash_window=CRASH_WINDOW, crash_limit=CRASH_LIMIT):
        self.crash_times = []
        self.crash_window = datetime.timedelta(seconds=crash_window)
        self.crash_limit = crash_limit

    def add_crash(self):
        self.crash_times.append(datetime.datetime.now())
        self.crash_times = [x for x in self.crash_times
                            if self.crash_times[-1] - x <= self.crash_window]

    def too_many_crashes(self):
        return len(self.crash_times) >= self.crash_limit


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    DEVICEMANAGER_RETRY_LIMIT = 8
    DEVICEMANAGER_SETTLING_TIME = 60
    PHONE_RETRY_LIMIT = 2
    PHONE_RETRY_WAIT = 15
    PHONE_MAX_REBOOTS = 3
    PHONE_PING_INTERVAL = 15*60
    PHONE_COMMAND_QUEUE_TIMEOUT = 10

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, user_cfg,
                 autophone_queue, logfile_prefix, loglevel, mailer,
                 build_cache_port):
        self.phone_cfg = phone_cfg
        self.user_cfg = user_cfg
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.crashes = Crashes(crash_window=user_cfg[PHONE_CRASH_WINDOW],
                               crash_limit=user_cfg[PHONE_CRASH_LIMIT])
        self.cmd_queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.subprocess = PhoneWorkerSubProcess(self.worker_num, self.ipaddr,
                                                tests,
                                                phone_cfg, user_cfg,
                                                autophone_queue,
                                                self.cmd_queue, logfile_prefix,
                                                loglevel, mailer,
                                                build_cache_port)
        self.logger = logging.getLogger('autophone.worker')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'phoneip': self.phone_cfg['ip']},
                                       '%(phoneid)s|%(phoneip)s|%(message)s')

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, status=phonetest.PhoneTestMessage.IDLE):
        self.subprocess.start(status)

    def stop(self):
        self.subprocess.stop()

    def new_job(self):
        self.cmd_queue.put_nowait(('job', None))

    def reboot(self):
        self.cmd_queue.put_nowait(('reboot', None))

    def disable(self):
        self.cmd_queue.put_nowait(('disable', None))

    def enable(self):
        self.cmd_queue.put_nowait(('enable', None))

    def debug(self, level):
        try:
            level = int(level)
        except ValueError:
            self.loggerdeco.error('Invalid argument for debug: %s' % level)
        else:
            self.user_cfg['debug'] = level
            self.cmd_queue.put_nowait(('debug', level))

    def ping(self):
        self.cmd_queue.put_nowait(('ping', None))

    def process_msg(self, msg):
        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if not self.last_status_msg or msg.status != self.last_status_msg.status:
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        self.last_status_msg = msg


class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    def __init__(self, worker_num, ipaddr, tests, phone_cfg, user_cfg,
                 autophone_queue, cmd_queue, logfile_prefix, loglevel, mailer,
                 build_cache_port):
        self.worker_num = worker_num
        self.ipaddr = ipaddr
        self.tests = tests
        self.phone_cfg = phone_cfg
        self.user_cfg = user_cfg
        self.autophone_queue = autophone_queue
        self.cmd_queue = cmd_queue
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.loglevel = loglevel
        self.mailer = mailer
        self.build_cache_port = build_cache_port
        self._stop = False
        self.p = None
        self.jobs = jobs.Jobs(self.mailer, self.phone_cfg['phoneid'])
        self.current_build = None
        self.last_ping = None
        self._dm = None
        self.status = None
        self.logger = logging.getLogger('autophone.worker.subprocess')
        self.loggerdeco = LogDecorator(self.logger,
                                       {'phoneid': self.phone_cfg['phoneid'],
                                        'phoneip': self.phone_cfg['ip']},
                                       '%(phoneid)s|%(phoneip)s|%(message)s')

    @property
    def dm(self):
        if not self._dm:
            self.loggerdeco.info('Connecting to %s:%d...' %
                                 (self.phone_cfg['ip'],
                                  self.phone_cfg['sutcmdport']))
            # Droids and other slow phones can take a while to come back
            # after a SUT crash or spontaneous reboot, so we up the
            # default retrylimit.
            self._dm = DroidSUT(self.phone_cfg['ip'],
                                self.phone_cfg['sutcmdport'],
                                retryLimit=self.user_cfg[DEVICEMANAGER_RETRY_LIMIT],
                                logLevel=self.loglevel)
            # Give slow devices chance to mount all devices.
            # Give slow devices chance to mount all devices.
            if self.user_cfg[DEVICEMANAGER_SETTLING_TIME] is not None:
                self._dm.reboot_settling_time =  self.user_cfg[DEVICEMANAGER_SETTLING_TIME]
            # Override mozlog.logger
            self._dm._logger = self.loggerdeco
            self.loggerdeco.info('Connected.')
        return self._dm

    def is_alive(self):
        """Call from main process."""
        return self.p and self.p.is_alive()

    def start(self, status):
        """Call from main process."""
        if self.p:
            if self.is_alive():
                return
            del self.p
        self.status = status
        self.p = multiprocessing.Process(target=self.run)
        self.p.start()

    def stop(self):
        """Call from main process."""
        if self.is_alive():
            self.cmd_queue.put_nowait(('stop', None))
            self.p.join(self.user_cfg[PHONE_COMMAND_QUEUE_TIMEOUT]*2)

    def has_error(self):
        return (self.status == phonetest.PhoneTestMessage.DISABLED or
                self.status == phonetest.PhoneTestMessage.DISCONNECTED)

    def disconnect_dm(self):
        self._dm = None

    def status_update(self, msg):
        self.status = msg.status
        self.loggerdeco.info(str(msg))
        try:
            self.autophone_queue.put_nowait(msg)
        except Queue.Full:
            self.loggerdeco.warning('Autophone queue is full!')

    def check_sdcard(self):
        self.loggerdeco.info('Checking SD card.')
        success = True
        try:
            dev_root = self.dm.getDeviceRoot()
            if dev_root:
                d = posixpath.join(dev_root, 'autophonetest')
                self.dm.removeDir(d)
                self.dm.mkDir(d)
                if self.dm.dirExists(d):
                    with tempfile.NamedTemporaryFile() as tmp:
                        tmp.write('autophone test\n')
                        tmp.flush()
                        self.dm.pushFile(tmp.name,
                                         posixpath.join(d, 'sdcard_check'))
                    self.dm.removeDir(d)
                else:
                    self.loggerdeco.error('Failed to create directory under '
                                          'device root!')
                    success = False
            else:
                self.loggerdeco.error('Invalid device root.')
                success = False
        except DMError:
            self.loggerdeco.exception('Exception while checking SD card!')
            success = False

        if not success:
            # FIXME: Should this be called under more circumstances than just
            # checking the SD card?
            self.clear_test_base_paths()
            return False

        # reset status if there had previous been an error.
        # FIXME: should send email that phone is back up.
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.IDLE))
        return True

    def clear_test_base_paths(self):
        for t in self.tests:
            t._base_device_path = ''

    def recover_phone(self):
        exc = None
        reboots = 0
        while reboots < self.user_cfg[PHONE_MAX_REBOOTS]:
            self.loggerdeco.info('Rebooting phone...')
            reboots += 1
            # FIXME: reboot() no longer indicates success/failure; instead
            # we just verify the device root.
            try:
                self.dm.reboot(ipAddr=self.ipaddr, wait=True)
                if self.dm.getDeviceRoot():
                    self.loggerdeco.info('Phone is back up.')
                    if self.check_sdcard():
                        return
                    self.loggerdeco.info('Failed SD card check.')
                else:
                    self.loggerdeco.info('Phone did not reboot successfully.')
            except DMError:
                self.loggerdeco.exception('Exception while checking SD card!')
            # DM can be in a weird state if reboot failed.
            self.disconnect_dm()

        self.loggerdeco.info('Phone has been rebooted %d times; giving up.' %
                             reboots)
        msg_body = 'Phone was rebooted %d times.' % reboots
        if exc:
            msg_body += '\n\n%s' % exc
        self.phone_disconnected(msg_body)

    def reboot(self):
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.REBOOTING))
        self.recover_phone()

    def phone_disconnected(self, msg_body):
        """Indicate that a phone has become unreachable or experienced a
        error from which we might be able to recover."""
        if self.has_error():
            return
        self.loggerdeco.info('Phone disconnected: %s.' % msg_body)
        if msg_body and self.mailer:
            self.loggerdeco.info('Sending notification...')
            try:
                self.mailer.send('Phone %s disconnected' % self.phone_cfg['phoneid'],
                                 '''Hello, this is Autophone. Phone %s appears to be disconnected:

%s

I'll keep trying to ping it periodically in case it reappears.
''' % (self.phone_cfg['phoneid'], msg_body))
                self.loggerdeco.info('Sent.')
            except socket.error:
                self.loggerdeco.exception('Failed to send disconnected-phone '
                                          'notification.')
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISCONNECTED))

    def disable_phone(self, errmsg, send_email=True):
        """Completely disable phone. No further attempts to recover it will
        be performed unless initiated by the user."""
        self.loggerdeco.info('Disabling phone: %s.' % errmsg)
        if errmsg and send_email and self.mailer:
            self.loggerdeco.info('Sending notification...')
            try:
                self.mailer.send('Phone %s disabled' % self.phone_cfg['phoneid'],
                                 '''Hello, this is Autophone. Phone %s has been disabled:

%s

I gave up on it. Sorry about that. You can manually re-enable it with
the "enable" command.
''' % (self.phone_cfg['phoneid'], errmsg))
                self.loggerdeco.info('Sent.')
            except socket.error:
                self.loggerdeco.exception('Failed to send disabled-phone '
                                          'notification.')
        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.DISABLED,
                msg=errmsg))

    def ping(self):
        self.loggerdeco.info('Pinging phone')
        # Verify that the phone is still responding.
        # It should always be possible to get the device root, so use this
        # command to ensure that the device is still reachable.
        try:
            if self.dm.getDeviceRoot():
                self.loggerdeco.info('Pong!')
                return True
        except DMError:
            self.loggerdeco.exception('Exception while pinging:')
        self.loggerdeco.error('Got empty device root!')
        return False

    def run_tests(self, build_metadata):
        if not self.has_error():
            self.loggerdeco.info('Rebooting...')
            self.reboot()

        # may have gotten an error trying to reboot, so test again
        if self.has_error():
            self.loggerdeco.info('Phone is in error state; not running test.')
            return False

        repo = build_metadata['tree']
        build_date = datetime.datetime.fromtimestamp(
            float(build_metadata['blddate']))

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'],
                phonetest.PhoneTestMessage.INSTALLING,
                build_metadata['blddate']))
        self.loggerdeco.info('Installing build %s.' % build_date)

        success = False
        for attempt in range(self.user_cfg[PHONE_RETRY_LIMIT]):
            try:
                pathOnDevice = posixpath.join(self.dm.getDeviceRoot(),
                                              'build.apk')
                self.dm.pushFile(os.path.join(build_metadata['cache_build_dir'],
                                              'build.apk'), pathOnDevice)
                self.dm.installApp(pathOnDevice)
                self.dm.removeFile(pathOnDevice)
                success = True
            except DMError:
                exc = 'Exception installing fennec attempt %d!\n\n%s' % (attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception installing fennec attempt %d!' % attempt)
                time.sleep(self.user_cfg[PHONE_RETRY_WAIT])
        if not success:
            self.phone_disconnected(exc)
            return False
        self.current_build = build_metadata['blddate']

        self.loggerdeco.info('Running tests...')
        for t in self.tests:
            if self.has_error():
                break
            try:
                repos = t.test_devices_repos[self.phone_cfg['phoneid']]
                if repos and repo not in repos:
                    self.loggerdeco.debug('run_tests: ignoring build %s '
                                          'repo %s not in '
                                          'defined repos: %s' %
                                          (build_date, repo, repos))
                    continue
            except KeyError:
                pass

            t.current_build = build_metadata['blddate']
            try:
                t.runjob(build_metadata, self)
            except DMError:
                exc = 'Uncaught device error while running test!\n\n%s' % \
                    traceback.format_exc()
                self.loggerdeco.exception('Uncaught device error while '
                                          'running test!')
                self.phone_disconnected(exc)
                return False
        return True

    def handle_timeout(self):
        if (self.status != phonetest.PhoneTestMessage.DISABLED and
            (not self.last_ping or
             (datetime.datetime.now() - self.last_ping >
              datetime.timedelta(seconds=self.user_cfg[PHONE_PING_INTERVAL])))):
            self.last_ping = datetime.datetime.now()
            if self.ping():
                if self.status == phonetest.PhoneTestMessage.DISCONNECTED:
                    self.recover_phone()
                if not self.has_error():
                    self.status_update(phonetest.PhoneTestMessage(
                            self.phone_cfg['phoneid'],
                            phonetest.PhoneTestMessage.IDLE,
                            self.current_build))
            else:
                self.loggerdeco.info('Ping unanswered.')
                # No point in trying to recover, since we couldn't
                # even perform a simple action.
                if not self.has_error():
                    self.phone_disconnected('No response to ping.')

    def handle_job(self, job):
        phoneid = self.phone_cfg['phoneid']
        abi = self.phone_cfg['abi']
        build_url = job['build_url']
        self.loggerdeco.debug('handle_job: job: %s, abi: %s' % (job, abi))
        incompatible_job = False
        if abi == 'x86':
            if 'x86' not in build_url:
                incompatible_job = True
        elif abi == 'armeabi-v6':
            if 'armv6' not in build_url:
                incompatible_job = True
        else:
            if 'x86' in build_url or 'armv6' in build_url:
                incompatible_job = True
        if incompatible_job:
            self.loggerdeco.debug('Ignoring incompatible job %s '
                                  'for phone abi %s' %
                                  (build_url, abi))
            self.jobs.job_completed(job['id'])
            return
        # Determine if we will test this build and if we need
        # to enable unittests.
        skip_build = True
        enable_unittests = False
        for test in self.tests:
            test_devices_repos = test.test_devices_repos
            if not test_devices_repos:
                # We know we will test this build, but not yet
                # if any of the other tests enable_unittests.
                skip_build = False
            elif not phoneid in test_devices_repos:
                # This device will not run this test.
                pass
            else:
                for repo in test_devices_repos[phoneid]:
                    if repo in build_url:
                        skip_build = False
                        enable_unittests = test.enable_unittests
                        break
            if not skip_build:
                break
        if skip_build:
            self.loggerdeco.debug('Ignoring job %s ' % build_url)
            self.jobs.job_completed(job['id'])
            return
        self.loggerdeco.info('Checking job %s.' % build_url)
        client = buildserver.BuildCacheClient(port=self.build_cache_port)
        self.loggerdeco.info('Fetching build...')
        cache_response = client.get(build_url, enable_unittests=enable_unittests)
        client.close()
        if not cache_response['success']:
            self.loggerdeco.warning('Errors occured getting build %s: %s' %
                                    (build_url, cache_response['error']))
            return
        self.loggerdeco.info('Starting job %s.' % build_url)
        starttime = datetime.datetime.now()
        if self.run_tests(cache_response['metadata']):
            self.loggerdeco.info('Job completed.')
            self.jobs.job_completed(job['id'])
            self.status_update(phonetest.PhoneTestMessage(
                    self.phone_cfg['phoneid'],
                    phonetest.PhoneTestMessage.IDLE,
                    self.current_build))
        else:
            self.loggerdeco.error('Job failed.')
        stoptime = datetime.datetime.now()
        self.loggerdeco.info('Job elapsed time: %s' % (stoptime - starttime))

    def handle_cmd(self, request):
        if not request:
            self.loggerdeco.debug('handle_cmd: No request')
            pass
        elif request[0] == 'stop':
            self.loggerdeco.info('Stopping at user\'s request...')
            self._stop = True
        elif request[0] == 'job':
            # This is just a notification that breaks us from waiting on the
            # command queue; it's not essential, since jobs are stored in
            # a db, but it allows the worker to react quickly to a request if
            # it isn't doing anything else.
            self.loggerdeco.debug('Received job command request...')
            pass
        elif request[0] == 'reboot':
            self.loggerdeco.info('Rebooting at user\'s request...')
            self.reboot()
        elif request[0] == 'disable':
            self.disable_phone('Disabled at user\'s request', False)
        elif request[0] == 'enable':
            self.loggerdeco.info('Enabling phone at user\'s request...')
            if self.has_error():
                self.status_update(phonetest.PhoneTestMessage(
                        self.phone_cfg['phoneid'],
                        phonetest.PhoneTestMessage.IDLE,
                        self.current_build))
                self.last_ping = None
        elif request[0] == 'debug':
            self.loggerdeco.info('Setting debug level %d at user\'s request...' % request[1])
            self.user_cfg['debug'] = request[1]
            DroidSUT.debug = self.user_cfg['debug']
            # update any existing DroidSUT objects
            if self._dm:
                self._dm.loglevel = self.user_cfg['debug']
            for t in self.tests:
                t.set_dm_debug(self.user_cfg['debug'])
        elif request[0] == 'ping':
            self.loggerdeco.info('Pinging at user\'s request...')
            self.ping()
        else:
            self.loggerdeco.debug('handle_cmd: Unknown request %s' % request[0])

    def main_loop(self):
        # Commands take higher priority than jobs, so we deal with all
        # immediately available commands, then start the next job, if there is
        # one.  If neither a job nor a command is currently available,
        # block on the command queue for PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT seconds.
        request = None
        while True:
            try:
                if not request:
                    request = self.cmd_queue.get_nowait()
                self.handle_cmd(request)
                request = None
                if self._stop:
                    return
            except Queue.Empty:
                request = None
                if self.has_error():
                    self.recover_phone()
                if not self.has_error():
                    job = self.jobs.get_next_job()
                    if job:
                        self.handle_job(job)
                    else:
                        try:
                            request = self.cmd_queue.get(
                                timeout=self.user_cfg[PHONE_COMMAND_QUEUE_TIMEOUT])
                        except Queue.Empty:
                            request = None
                            self.handle_timeout()

    def run(self):
        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout
        self.filehandler = MultiprocessingTimedRotatingFileHandler(self.logfile,
                                                                   when='midnight',
                                                                   backupCount=7)
        fileformatstring = ('%(asctime)s|%(levelname)s'
                            '|%(message)s')
        self.fileformatter = logging.Formatter(fileformatstring)
        self.filehandler.setFormatter(self.fileformatter)
        self.logger.addHandler(self.filehandler)

        self.loggerdeco.info('PhoneWorker starting up.')

        DroidSUT.loglevel = self.user_cfg.get('debug', 3)

        for t in self.tests:
            t.status_cb = self.status_update

        self.status_update(phonetest.PhoneTestMessage(
                self.phone_cfg['phoneid'], self.status))

        if self.status != phonetest.PhoneTestMessage.DISABLED:
            if not self.check_sdcard():
                self.recover_phone()
            if self.has_error():
                self.loggerdeco.error('Initial SD card check failed.')

        self.main_loop()
