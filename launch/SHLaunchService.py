#*****************************************************************************
#
# SHLaunchService.py
#   Windows service ("SHLaunchPY3") that owns the Sighthound Video Py3 back end.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" The SHLaunchPY3 service.

This is the Python 3 replacement for the original project's native `shlaunch`
Windows service (launch/shlaunchWin, still in the tree for reference). It does
the same job -- own the back end's lifetime so recording keeps running with no
user logged in and no front end open -- but is written in Python on top of
pywin32 instead of C, which is how the rest of this port replaced the native
pieces.

Deliberately named SHLaunchPY3 so it cannot collide with an existing Python 2
"shlaunch" install on the same machine.

Design notes
------------
* NO app imports at module scope. The service starts under pythonservice.exe
  long before a desktop exists; importing wx / torch / the back end here would
  make service startup slow and fragile. The back end is spawned as a child
  process exactly the way the front end spawns it today.
* The control channel is the Service Control Manager itself plus two files in
  the data directory (`shlaunch.cfg` for settings, `shlaunch.state` for status).
  There is no custom shared-memory protocol like the C service had -- the SCM
  already provides start/stop/query, and a state file crosses the session-0
  boundary without any special permissions.
* Custom control codes let a front end ask for a back-end restart without
  restarting the service itself (see kControl* below).
"""

import json
import os
import subprocess
import sys
import threading
import time

import win32event
import win32service
import win32serviceutil
import servicemanager

# Identity, control codes and option names live in Launch.py, which is the
# module the rest of the app already talks to -- defining them twice would let
# the two halves drift apart. Launch.py is import-light on purpose.
from .Launch import kServiceName
from .Launch import kControlRestartBackend, kControlStopBackend
from .Launch import kControlStartBackend
from .Launch import kOptionInstallDir, kOptionPythonExe, kOptionDataDir
from .Launch import _kStateFile as kStateFile


###############################################################################

kServiceDisplayName = "Sighthound Video Py3 Launcher"
kServiceDescription = (
    "Runs the Sighthound Video Py3 back end (cameras, recording, detection and "
    "rule responses) so it keeps running when no user is logged in and the "
    "front end is closed."
)

# How long to let the back end shut down cleanly before we terminate it.
_kGracefulQuitSecs = 45.0

# Supervision backoff after the back end dies unexpectedly, in seconds.
_kRestartBackoffSecs = [5, 15, 30, 60, 120, 300]

# A back end that survives this long is considered healthy (backoff resets).
_kHealthyRunSecs = 120.0

# Main loop tick. Also how often the state file is refreshed.
_kPollSecs = 2.0

# Replacing the state file loses a race with anyone who has it open for
# reading (see writeState).  The reader holds it for microseconds, so a few
# short retries cover it without delaying the tick meaningfully.
_kStateReplaceTries = 5
_kStateReplaceWaitSecs = 0.02


###############################################################################
def _serviceOption(name, default=None):
    """Read one of the installer-written options from the service's registry key.

    @param  name     Option name, one of the kOption* constants.
    @param  default  Value to return when the option was never written.
    @return          The option value, or default.
    """
    try:
        return win32serviceutil.GetServiceCustomOption(kServiceName, name,
                                                       default)
    except Exception:
        return default


###############################################################################
def _defaultDataDir():
    """Best-effort data directory, used only when the installer didn't record one.

    Mirrors FrontEndUtils.getUserLocalDataDir()'s non-service branch without
    importing wx (which we must not do inside a service).

    @return  Absolute path to the user data directory.
    """
    localAppData = os.environ.get("LOCALAPPDATA")
    if not localAppData:
        localAppData = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(localAppData, "Sighthound Video Py3")


###############################################################################
class _BackendSupervisor(object):
    """Starts, stops and restarts the back-end process, and keeps a state file.

    Kept separate from the service class so it can be exercised from a normal
    console session (see the __main__ block's `debug` mode) without the SCM.
    """

    ###########################################################
    def __init__(self, installDir, pythonExe, dataDir, log):
        """Constructor.

        @param  installDir  Directory holding FrontEndLaunchpad.py (the cwd the
                            back end is started in).
        @param  pythonExe   Interpreter used to run the launchpad.
        @param  dataDir     User data directory, passed to the back end and
                            holding shlaunch.cfg / shlaunch.state.
        @param  log         Callable taking a single string.
        """
        super(_BackendSupervisor, self).__init__()
        self._installDir = installDir
        self._pythonExe = pythonExe
        self._dataDir = dataDir
        self._log = log

        self._proc = None
        self._startedAt = 0.0
        self._failures = 0
        self._retryAt = 0.0
        self._wantBackend = False
        self._shutdownPending = False
        self._lastSignal = 0

        # start()/stop() run on TWO threads: the service main loop calls poll(),
        # while the SCM's control dispatcher calls SvcOther() for a front end's
        # restart request.  Unsynchronised, both could pass isRunning() before
        # either had spawned, launch a back end each, and leave self._proc
        # pointing at only one of them -- the other became untracked and was
        # never stopped.  Observed 2026-08-15 07:56:24/25: two back ends a
        # second apart, and the tracked one was then killed while the untracked
        # one kept the IPC ports, so every later start found "Back end already
        # running" and exited.  Reentrant because start() calls stop().
        self._lock = threading.RLock()


    ###########################################################
    def _launchArgv(self):
        """Build the back-end command line.

        Intentionally identical in shape to what FrontEndApp spawns today (see
        frontEnd/GetLaunchParameters.py and FrontEndApp's `--backEnd` branch),
        including the two marker arguments that let process enumeration tell a
        back end apart from any other Python process.

        @return  argv list for subprocess.Popen.
        """
        # Imported here, not at module scope: appCommon pulls in a chain we do
        # not want resolved during service startup.
        sys.path.insert(0, self._installDir)
        try:
            from appCommon.CommonStrings import kBackendMarkerArg
            from appCommon.CommonStrings import kReservedMarkerSvcArg
        finally:
            try:
                sys.path.remove(self._installDir)
            except ValueError:
                pass

        # The SECOND marker must be kReservedMarkerSvcArg, not kReservedMarkerArg:
        # NetworkMessageServer decides whether the back end was service-launched
        # by testing for this exact value in sys.argv, and the native C service it
        # replaced sent this one too (shlaunchWin/shlaunch/shlaunch.c, BACKEND_ARG3).
        # Sending the front end's placeholder instead made launchedByService()
        # report False for every service-started back end.
        return [self._pythonExe,
                os.path.join(self._installDir, "FrontEndLaunchpad.py"),
                "--backEnd", self._dataDir,
                kBackendMarkerArg,
                kReservedMarkerSvcArg]


    ###########################################################
    def _killStrayBackends(self):
        """Kill any back-end process not started by us.

        A back end started by a front end (service disabled, or a crash that
        outlived us) holds the cameras' RTSP sessions and the databases, so it
        must go before we start our own. Matched by the marker argument, which
        only a back end carries.
        """
        try:
            import psutil
        except Exception as e:
            self._log("cannot import psutil, skipping stray scan: %r" % (e,))
            return

        sys.path.insert(0, self._installDir)
        try:
            from appCommon.CommonStrings import kBackendMarkerArg
        except Exception as e:
            self._log("cannot resolve backend marker: %r" % (e,))
            return
        finally:
            try:
                sys.path.remove(self._installDir)
            except ValueError:
                pass

        ourPid = None if self._proc is None else self._proc.pid
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] in (os.getpid(), ourPid):
                    continue
                cmdline = proc.info.get("cmdline") or []
                if kBackendMarkerArg in cmdline:
                    self._log("killing stray back end pid=%d" % proc.info["pid"])
                    proc.kill()
            except Exception:
                # Process vanished or is not ours to touch; both are fine.
                pass


    ###########################################################
    def _killProcessTree(self, pid):
        """Kill `pid` AND everything it spawned.

        Killing the back end alone is not enough: it runs a process per camera
        plus the detection service, the web server and the message server, and
        Windows does not reparent or terminate those when their parent dies.
        On 2026-08-15 that left six orphans behind, one still listening on the
        IPC ports -- so every back end started afterwards saw "Back end already
        running", exited cleanly, and the service retried into that same wall
        with escalating backoff.  Nothing recovered it but killing the orphans
        by hand.

        Children are collected BEFORE the parent dies, while the tree is still
        walkable, then killed youngest-first so nothing respawns underneath us.

        @param  pid  Root of the tree to kill.
        @return      True if the tree was walked and killed via psutil.
        """
        try:
            import psutil
        except Exception as e:
            self._log("cannot import psutil for tree kill: %r" % (e,))
            return False

        try:
            parent = psutil.Process(pid)
        except Exception:
            return True     # already gone; nothing to orphan

        try:
            children = parent.children(recursive=True)
        except Exception:
            children = []

        for child in reversed(children):
            try:
                self._log("killing back-end child pid=%d" % child.pid)
                child.kill()
            except Exception:
                # Vanished on its own, or not ours to touch.  Both are fine.
                pass
        try:
            parent.kill()
        except Exception:
            pass

        # Reap what we can so the pids are not left in a zombie state; a child
        # that outlives this is reported rather than silently ignored, because
        # it is exactly what wedges the next start.
        try:
            _gone, alive = psutil.wait_procs([parent] + children, timeout=15)
            for proc in alive:
                self._log("WARNING: back-end child pid=%d survived the kill"
                          % proc.pid)
        except Exception:
            pass
        return True


    ###########################################################
    def start(self, killFirst=True, signal=0):
        """Start the back end.

        @param  killFirst  Kill stray back ends before starting.
        @param  signal     Opaque value echoed back through the state file, so a
                           caller can tell its own request apart from an older
                           one (mirrors the C service's launch signal).
        @return            True if a back end is running when we return.
        """
        with self._lock:
            return self._startLocked(killFirst, signal)


    ###########################################################
    def _startLocked(self, killFirst, signal):
        """start(), with self._lock already held."""
        self._lastSignal = signal

        # Cancel any scheduled retry: this start supersedes it.  Left armed, a
        # retry that came due during a control-driven start fired immediately
        # afterwards and restarted a back end that had just come up.
        self._retryAt = 0.0

        if self.isRunning():
            if not killFirst:
                self._wantBackend = True
                return True
            self._stopLocked(graceful=True)

        # AFTER the stop, not before: _stopLocked clears _wantBackend, because
        # it is also the public "stop and stay stopped" path.  Setting our
        # intent first meant a restart-with-kill ended with supervision turned
        # OFF -- poll() returns immediately when _wantBackend is False, so the
        # back end it had just started would never have been restarted if it
        # later died.
        self._wantBackend = True

        if killFirst:
            self._killStrayBackends()

        argv = self._launchArgv()
        self._log("starting back end: %r (cwd=%s)" % (argv, self._installDir))
        try:
            # CREATE_NO_WINDOW: a service has no console to inherit, and without
            # this the child can fail to start on some Windows builds.
            creationFlags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationFlags |= subprocess.CREATE_NO_WINDOW
            # SV_DATA_DIR: the back end and its children resolve config,
            # enrollment and model paths from it. A service account's HOME is
            # not the user's, so without this the back end would build a second
            # set of settings under C:\Windows\System32\config\systemprofile.
            env = dict(os.environ)
            env["SV_DATA_DIR"] = self._dataDir
            self._proc = subprocess.Popen(
                argv, cwd=self._installDir, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationFlags)
            self._startedAt = time.time()
            self._log("back end started, pid=%d" % self._proc.pid)
            return True
        except Exception as e:
            self._proc = None
            self._failures += 1
            self._log("back end failed to start: %r" % (e,))
            return False


    ###########################################################
    def stop(self, graceful=True):
        """Stop the back end.

        @param  graceful  Ask the back end to quit through its own RPC first
                          (it closes databases and finalizes clips on the way
                          out); terminate only if that doesn't take.
        """
        with self._lock:
            self._stopLocked(graceful)


    ###########################################################
    def _stopLocked(self, graceful=True):
        """stop(), with self._lock already held."""
        self._wantBackend = False
        if not self.isRunning():
            self._proc = None
            return

        pid = self._proc.pid
        if graceful:
            self._log("asking back end pid=%d to quit" % pid)
            try:
                quitEnv = dict(os.environ)
                quitEnv["SV_DATA_DIR"] = self._dataDir
                subprocess.call(
                    [self._pythonExe,
                     os.path.join(self._installDir, "FrontEndLaunchpad.py"),
                     "--quit"],
                    cwd=self._installDir, env=quitEnv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_kGracefulQuitSecs)
            except Exception as e:
                self._log("graceful quit request failed: %r" % (e,))

            deadline = time.time() + _kGracefulQuitSecs
            while time.time() < deadline and self.isRunning():
                time.sleep(0.5)

        if self.isRunning():
            self._log("terminating back end pid=%d (and its children)" % pid)
            try:
                if not self._killProcessTree(pid):
                    # No psutil: at least take the parent down, and say so --
                    # its children will be orphaned and will wedge the next
                    # start, which is worth a log line rather than a mystery.
                    self._log("WARNING: no psutil; killing pid=%d only, "
                              "children may be orphaned" % pid)
                    self._proc.kill()
                self._proc.wait(timeout=15)
            except Exception as e:
                self._log("terminate failed: %r" % (e,))
        else:
            self._log("back end pid=%d exited cleanly" % pid)

        self._proc = None


    ###########################################################
    def isRunning(self):
        """@return  True if our back-end child is alive."""
        return self._proc is not None and self._proc.poll() is None


    ###########################################################
    def poll(self):
        """Supervision tick: restart the back end if it died unexpectedly.

        Uses the same escalating backoff idea as BackEndApp's camera restarts,
        so a back end that cannot start (bad config, corrupt DB) does not spin.
        """
        # Non-blocking: a start or stop already in flight on the control thread
        # is doing this tick's job, and a stop can sit in the graceful path for
        # the best part of a minute.  Blocking here would stall the main loop
        # behind it -- and the tick is cheap to skip, since another follows in
        # a second.
        if not self._lock.acquire(blocking=False):
            return
        try:
            if not self._wantBackend or self.isRunning():
                return

            if self._proc is not None:
                ranFor = time.time() - self._startedAt
                rc = self._proc.returncode
                self._proc = None
                if ranFor >= _kHealthyRunSecs:
                    self._failures = 0
                else:
                    self._failures += 1
                delay = _kRestartBackoffSecs[
                    min(self._failures, len(_kRestartBackoffSecs) - 1)]
                self._retryAt = time.time() + delay
                self._log("back end exited (rc=%s) after %.1fs; retry in %ds "
                          "(failure %d)" % (rc, ranFor, delay, self._failures))
                return

            if self._retryAt and time.time() >= self._retryAt:
                self._retryAt = 0.0
                self._startLocked(killFirst=True, signal=self._lastSignal)
        finally:
            self._lock.release()


    ###########################################################
    def setShutdownPending(self, pending):
        """Flag a service shutdown so the back end can notice it (BackEndApp
        polls this through Launch.shutdown()).
        """
        self._shutdownPending = pending


    ###########################################################
    def writeState(self):
        """Publish current status to <dataDir>/shlaunch.state.

        Written atomically: a front end polls this file and must never see a
        half-written one.
        """
        # Snapshot self._proc ONCE: stop() can clear it from another thread
        # between the isRunning() test and the .pid read.  Deliberately not
        # locked -- this runs on the main loop and must not queue behind a
        # graceful stop; one stale state file is harmless, since the front end
        # re-reads it continuously.
        proc = self._proc
        running = proc is not None and proc.poll() is None
        state = {
            "service_pid": os.getpid(),
            "service_name": kServiceName,
            "backend_pid": proc.pid if running else 0,
            "backend_running": running,
            "want_backend": self._wantBackend,
            "shutdown_pending": self._shutdownPending,
            "last_signal": self._lastSignal,
            "data_dir": self._dataDir,
            "install_dir": self._installDir,
            "updated_ms": int(time.time() * 1000),
        }
        path = os.path.join(self._dataDir, kStateFile)
        tmp = path + ".tmp"
        err = None
        try:
            os.makedirs(self._dataDir, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(state, f)

            # os.replace onto a file another process currently has OPEN fails
            # on Windows with "Access is denied": CPython's open() does not
            # pass FILE_SHARE_DELETE, so while Launch.py is reading
            # shlaunch.state the destination cannot be swapped.  It holds the
            # file for microseconds and we republish every tick, so retry
            # briefly instead of dropping the update -- 29 of these were logged
            # as failures over two days when each was one lost tick.
            for attempt in range(_kStateReplaceTries):
                try:
                    os.replace(tmp, path)
                    err = None
                    break
                except PermissionError as e:
                    err = e
                    if attempt + 1 < _kStateReplaceTries:
                        time.sleep(_kStateReplaceWaitSecs)
        except Exception as e:
            err = e

        if err is not None:
            # Still only a stale tick: the previous file is intact and the
            # front end re-reads it continuously.  Say what actually happened
            # rather than implying the state could not be written at all.
            self._log("state file not replaced this tick (%r); the previous "
                      "one is still current" % (err,))
            try:
                os.remove(tmp)
            except Exception:
                pass


    ###########################################################
    def clearState(self):
        """Remove the state file so nothing thinks the service is still up."""
        try:
            os.remove(os.path.join(self._dataDir, kStateFile))
        except Exception:
            pass


###############################################################################
class SHLaunchService(win32serviceutil.ServiceFramework):
    """The SHLaunchPY3 service itself."""

    _svc_name_ = kServiceName
    _svc_display_name_ = kServiceDisplayName
    _svc_description_ = kServiceDescription


    ###########################################################
    def __init__(self, args):
        super(SHLaunchService, self).__init__(args)
        self._stopEvent = win32event.CreateEvent(None, 0, 0, None)
        self._supervisor = None


    ###########################################################
    def _log(self, msg):
        """Log to <dataDir>/logs/SHLaunchPY3.log, and to the event log on error.

        A service has nowhere else to talk; the app's own logging machinery
        imports too much to be safe here.
        """
        line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            dataDir = _serviceOption(kOptionDataDir) or _defaultDataDir()
            logDir = os.path.join(dataDir, "logs")
            os.makedirs(logDir, exist_ok=True)
            with open(os.path.join(logDir, "%s.log" % kServiceName), "a") as f:
                f.write(line)
        except Exception:
            pass


    ###########################################################
    def SvcStop(self):
        """SCM asked us to stop: bring the back end down first."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,
                                 waitHint=int((_kGracefulQuitSecs + 30) * 1000))
        self._log("stop requested")
        if self._supervisor is not None:
            self._supervisor.setShutdownPending(True)
            self._supervisor.writeState()
            self._supervisor.stop(graceful=True)
        win32event.SetEvent(self._stopEvent)


    ###########################################################
    def SvcShutdown(self):
        """System is shutting down; same handling as a stop."""
        self.SvcStop()


    ###########################################################
    def SvcOther(self, control):
        """Handle our custom control codes (a front end asking about the back
        end, not about the service).
        """
        if control == kControlRestartBackend:
            self._log("control: restart back end")
            self._supervisor.start(killFirst=True,
                                   signal=int(time.time()) & 0xffff)
        elif control == kControlStopBackend:
            self._log("control: stop back end")
            self._supervisor.stop(graceful=True)
        elif control == kControlStartBackend:
            self._log("control: start back end")
            if not self._supervisor.isRunning():
                self._supervisor.start(killFirst=False,
                                       signal=int(time.time()) & 0xffff)
        else:
            super(SHLaunchService, self).SvcOther(control)
        if self._supervisor is not None:
            self._supervisor.writeState()


    ###########################################################
    def SvcDoRun(self):
        """Service main loop."""
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (kServiceName, ''))

        installDir = _serviceOption(kOptionInstallDir)
        pythonExe = _serviceOption(kOptionPythonExe)
        dataDir = _serviceOption(kOptionDataDir) or _defaultDataDir()

        if not installDir or not pythonExe:
            self._log("FATAL: service is not configured (InstallDir=%r "
                      "PythonExe=%r). Re-run the installer." %
                      (installDir, pythonExe))
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)
            return

        self._log("service starting (installDir=%s pythonExe=%s dataDir=%s)" %
                  (installDir, pythonExe, dataDir))

        self._supervisor = _BackendSupervisor(installDir, pythonExe, dataDir,
                                              self._log)

        # Config decides whether we own the back end at all, and whether it
        # comes up without waiting for a front end. Read through the same
        # helper the front end writes with, so the formats can never drift.
        autoStart, serviceOwnsBackend = _readLaunchConfig(installDir, dataDir,
                                                          self._log)
        self._log("config: autostart=%s backend=%s" %
                  (autoStart, serviceOwnsBackend))

        if serviceOwnsBackend and autoStart:
            self._supervisor.start(killFirst=True, signal=1)
        self._supervisor.writeState()

        while True:
            rc = win32event.WaitForSingleObject(self._stopEvent,
                                                int(_kPollSecs * 1000))
            if rc == win32event.WAIT_OBJECT_0:
                break
            self._supervisor.poll()
            self._supervisor.writeState()

        self._supervisor.clearState()
        self._log("service stopped")
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STOPPED,
                              (kServiceName, ''))


###############################################################################
def _readLaunchConfig(installDir, dataDir, log):
    """Read shlaunch.cfg using the app's own reader.

    @param  installDir  Root the launch package lives under.
    @param  dataDir     Where shlaunch.cfg lives.
    @param  log         Log callable.
    @return             (autoStart, serviceOwnsBackend) booleans.
    """
    sys.path.insert(0, installDir)
    try:
        from launch.Launch import Launch, kConfigKeyAutoStart
        from launch.Launch import kConfigKeyBackend, kConfigValueTrue
        cfg = Launch().getConfigOrDefaults(dataDir)
        autoStart = str(cfg.get(kConfigKeyAutoStart)).upper() == kConfigValueTrue
        backend = str(cfg.get(kConfigKeyBackend)).upper() == kConfigValueTrue
        return autoStart, backend
    except Exception as e:
        log("cannot read launch config (%r); assuming service owns the back "
            "end and does not autostart" % (e,))
        return False, True
    finally:
        try:
            sys.path.remove(installDir)
        except ValueError:
            pass


###############################################################################
if __name__ == '__main__':
    # `python -m launch.SHLaunchService install|start|stop|remove|debug ...`
    # Installation is normally done by InstallService.py, which also records the
    # InstallDir/PythonExe/DataDir options this service needs.
    win32serviceutil.HandleCommandLine(SHLaunchService)
