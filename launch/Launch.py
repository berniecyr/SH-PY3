#*****************************************************************************
#
# Launch.py
#
#
#
#*****************************************************************************
#
#
# Copyright 2013-2022 Sighthound, Inc.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
# Alternative licensing available from Sighthound, Inc.
# by emailing opensource@sighthound.com
#
# This file is part of the Sighthound Video project which can be found at
# https://github.com/sighthoundinc/SighthoundVideo
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; using version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02111, USA.
#
#
#*****************************************************************************

import copy
import getpass
import os
import random
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree

import json

from configparser import ConfigParser

# pywin32 drives the service. It is a hard requirement on Windows (see
# requirements.txt) but this module is imported on Mac too, and by tools that
# only want the config helpers, so an import failure must not be fatal.
try:
    import win32service
    import win32serviceutil
except Exception:
    win32service = None
    win32serviceutil = None


###############################################################################

# Name of the configuration file.
_kConfigFile = "shlaunch.cfg"

# Windows service identity. Deliberately NOT "shlaunch" so a Python 2 install of
# the original product can stay on the same machine without a name collision.
kServiceName = "SHLaunchPY3"

# The service publishes what it is doing here, in the data directory. It is the
# only status channel that crosses the session-0 boundary without needing any
# special rights (the SCM covers control, this covers state).
_kStateFile = "shlaunch.state"

# Custom SCM control codes the service understands (128-255 are app-defined).
kControlRestartBackend = 128
kControlStopBackend    = 129
kControlStartBackend   = 130

# Registry option names the installer records on the service key.
kOptionInstallDir = "InstallDir"
kOptionPythonExe  = "PythonExe"
kOptionDataDir    = "DataDir"

# Base name of the shlaunch executable w/o extension.
_kServiceExe = "shlaunch"

# The sudo application we use to run the service with administrative rights.
_kSudoExe = "SighthoundVideoLauncher"

# The (one and only) section in the configuration file.
_kConfigSectionMain = "Main"

# Configuration key: auto-start the backend when the service starts (boolean,
# default is "FALSE"). Mostly important at system boot time.
# NOTE: all keys must be lowercase, the INI reader normalizes like that
kConfigKeyAutoStart = "autostart"

# Configuration key: do start the backend via the service (boolean, default
# is "TRUE"). This is checked by the service and blocks the autostart, but is
# also used in the frontend to know whether to launch the backend ovr the
# service or all by itself.
kConfigKeyBackend = "backend"

# Configuration boolean values (strings).
kConfigValueTrue = "TRUE"
kConfigValueFalse = "FALSE"

# Enable autostart by default on Windows only (since OSX is still plagued by
# network drive issues, so we'd rather let user enable the service explicitly there)
# NOTE: this MUST be one of the kConfigValue* STRINGS, not a bool. Callers
# compare it with `kConfigValueTrue == cfg[key]`, and ConfigParser hands out
# strings for a config that exists -- a bool default made those comparisons
# silently False whenever shlaunch.cfg was missing.
kAutoStartDefault = kConfigValueTrue if sys.platform == 'win32' \
                    else kConfigValueFalse

_kDefaultSettings = { kConfigKeyAutoStart: kAutoStartDefault,
                      kConfigKeyBackend  : kConfigValueTrue }

# Where to create or expect the service launch registration.
_kDaemonPlistName = "com.sighthound.video.launch"
_kDaemonPlistFile = _kDaemonPlistName + ".plist"
_kDaemonPlistPath = os.path.join(os.path.sep, "Library", "LaunchDaemons",
                                 _kDaemonPlistFile)

# How long to wait for activation to be launched.
_kActivationLaunchTimeout = 20

# Decision about whether to run as a service not not. Made at runtime.
_kServiceAvailable = None

###############################################################################
def serviceAvailable():
    """ Checks whether the service is expected to be present and that we should
    use it, for at least determining the work directory. Whether it does the
    job of launching the back-end is a different decision.

    Py3 port: this used to require a frozen build, because the check was really
    "is the native launch.dll next to our exe". The service is now a normal
    Windows service (SHLaunchPY3, see SHLaunchService.py), so the honest test is
    "is that service installed" -- which also makes the whole path testable from
    a source checkout. `SV_NO_SERVICE=1` still forces the old in-process
    behaviour, which is handy when debugging without touching the service.

    @return  True if the service is available to us.
    """
    global _kServiceAvailable
    if _kServiceAvailable is None:
        try:
            if 0 != int(os.getenv("SV_NO_SERVICE", "0")):
                _kServiceAvailable = False
                return _kServiceAvailable
        except:
            pass
        if win32serviceutil is None or sys.platform != 'win32':
            _kServiceAvailable = False
        else:
            try:
                win32serviceutil.QueryServiceStatus(kServiceName)
                _kServiceAvailable = True
            except Exception:
                # Not installed, or we cannot see it; either way, don't use it.
                _kServiceAvailable = False
    return _kServiceAvailable


###############################################################################
def _serviceState():
    """ Query the service's current SCM state.

    @return  One of the win32service.SERVICE_* constants, or None when the
             service is not installed / not reachable.
    """
    if win32serviceutil is None:
        return None
    try:
        return win32serviceutil.QueryServiceStatus(kServiceName)[1]
    except Exception:
        return None


###############################################################################
def _serviceOption(name, default=None):
    """ Read one of the options the installer recorded on the service key.

    @param  name     Option name (InstallDir / PythonExe / DataDir).
    @param  default  Returned when the option is missing.
    @return          The option value.
    """
    if win32serviceutil is None:
        return default
    try:
        return win32serviceutil.GetServiceCustomOption(kServiceName, name,
                                                       default)
    except Exception:
        return default


###############################################################################
def _sendServiceControl(control):
    """ Send a custom control code to the service, from an UNELEVATED process.

    Deliberately not win32serviceutil.ControlService(): that helper opens the
    SCM with SC_MANAGER_ALL_ACCESS and the service with SERVICE_ALL_ACCESS, and
    an ordinary user is never granted either -- so every front end running
    without elevation got "Access is denied", Launch.do() returned None, and the
    app died with "The application could not be started.  Please wait a minute
    and try again."  No DACL grant can fix that; ALL_ACCESS is the problem.

    We ask for exactly one right instead, SERVICE_USER_DEFINED_CONTROL, which is
    all a custom control code needs and which the stock service DACL already
    gives to Interactive Users.

    @param  control  One of the kControl* codes.
    @raise           Whatever win32service raises if the user really cannot
                     signal the service.
    """
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(
            scm, kServiceName, win32service.SERVICE_USER_DEFINED_CONTROL)
        try:
            win32service.ControlService(handle, control)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)


###############################################################################
def getServiceDataDir():
    """ The data directory the installer registered for the service.

    Readable straight off the service key, so it answers even when the service
    is stopped -- which is precisely when a front end still needs to know where
    its databases are.

    @return  The data directory, or None when the service is not installed.
    """
    return _serviceOption(kOptionDataDir)


###############################################################################

# Extra flag to signal the service that the back-end(s) should be killed, before
# an actual launch. Kept for API compatibility with the old native service --
# the Python service always kills strays before a launch.
_kLaunchFlagKillFirst = 0x10000

###############################################################################
class Launch(object):
    """ To talk to the launch service, mainly to start the back-end and to get
    the data directory. And of course to determine if the service is up.
    """

    ###########################################################
    def __init__(self):
        """ Constructor. Does NOT connect to service.
        """
        super(Launch, self).__init__()
        self._handle = None


    ###########################################################
    def open(self):
        """ Connect to the service.

        @return True if service control is established. False if this did not
                work out, which in most of the cases means that the service is
                not running. There is no recovery for that, it either didn't
                get installed properly or it got shut down by an administrator.
        """
        if self._handle:
            return True
        if not serviceAvailable():
            return False
        # "Connected" now means the service exists and is running; there is no
        # session to hold open, so the handle is just a latch.
        if _serviceState() != win32service.SERVICE_RUNNING:
            return False
        self._handle = True
        return True


    ###########################################################
    def close(self):
        """ Detaches from the service.

        @return True if the operation succeeded or if we have been detached
                already. False if an error occurred, retrying is an option, but
                most likely not to succeed. Abandonment of the instance and
                possible restart of the owning process is recommended.
        """
        self._handle = None
        return True


    ###########################################################
    def do(self, signal=None, killFirst=False):
        """ Signals the service to launch the back-end. It is up to the service
        to detect the trigger, which happens asynchronously.

        @param  signal     The launch signal. Anything but zero will trigger a
                           launch operation. You may use a random or sequence
                           number to detect different launch triggers, in the
                           range of 1..65535 (16bit unsigned integer). If the
                           value is None a random number will be generated.
        @param  killFirst  Flag to just kill old back-ends and not launch.
        @return            (oldSignal, newSignal) The old value of the launch
                           signal. If it is zero it means that no trigger was
                           set before. If it is the same as the signal and such
                           has been chosen to be unique it means that the former
                           launch request has not been honored yet. The new one
                           is the signal passed in or the auto-generated value.
                           None if the client is not connected.
        """
        if not self._handle:
            return None
        if signal is None:  # create the signal value if none is given
            signal = random.randint(1,65535)
        signal &= 0xffff    # make sure the signal stays 16bit

        state = self.readState() or {}
        oldSignal = int(state.get("last_signal", 0))

        control = kControlRestartBackend if killFirst else kControlStartBackend
        try:
            _sendServiceControl(control)
        except Exception:
            # The service just stopped, or this user really cannot signal it.
            return None
        return oldSignal, signal


    ###########################################################
    def pid(self):
        """ The process identifier of the service. This allows detection of
        service restarts and re-issuing a trigger signal.

        @return Service PID. None if not connected to the service.
        """
        if not self._handle:
            return None
        state = self.readState()
        return None if state is None else int(state.get("service_pid", 0))


    ###########################################################
    def status(self):
        """ To determine if the last back-end process launch succeeded.

        @return  Zero if the launch failed. Last launch signal on success.
                 None if not connected to the service.
        """
        if not self._handle:
            return None
        state = self.readState()
        if state is None:
            return 0
        return int(state.get("last_signal", 0)) if state.get("backend_running") \
               else 0


    ###########################################################
    def shutdown(self):
        """ To check if the service got notified of a system shutdown.

        @return  Zero if no notification. 1 if shutdown got signaled.
                 None if not connected to the service.
        """
        if not self._handle:
            return None
        state = self.readState()
        if state is not None:
            return 1 if state.get("shutdown_pending") else 0

        # No readable state.  This is ONLY a shutdown if the file is genuinely
        # gone -- the service removes it on the way out.  A failure to READ it
        # must never be taken as one.
        #
        # readState() returns None for "could not read" as well as "not there",
        # and the back end polls this on every pass of its main loop while the
        # service rewrites the file every 2s with os.replace().  On Windows a
        # reader can momentarily fail to open a file being replaced (the same
        # collision the service logs from its own side as "cannot write state
        # file: PermissionError(13, 'Access is denied')").  Treating that
        # instant as a shutdown request made the back end quit ITSELF, cleanly
        # and for no reason, 50-90s after every start -- which meant no segment
        # ever lived long enough to be finalized, so thumbnails appeared and
        # .mp4 archive did not.  Diagnosed 2026-08-14.
        dataDir = _serviceOption(kOptionDataDir)
        if dataDir and os.path.isfile(os.path.join(dataDir, _kStateFile)):
            return 0        # it is there; we just could not read it this instant
        if _serviceState() == win32service.SERVICE_RUNNING:
            return 0        # service is up and simply has not written state yet
        return 1


    ###########################################################
    def build(self):
        """ Determines the build number of the service. This is only needed and
        currently works for OSX. Under Win32 we _always_ return None!

        @return  The build number, same format we use in the app itself. Or
                 None if not connected to the service.
        """
        # Was only ever meaningful on OSX, where the service shipped its own
        # build number; the Windows service has always returned None here.
        return None


    ###########################################################
    def dataDir(self):
        """ Lets the service tell us about the user data directory it chose.
        This directory is system-global and also needs to be prepared to be
        accessible by front-ends of any user, which can only be done by the
        service itself.

        @return  The data directory. Empty if it hasn't been created yet.
                 None if not connected to the service.
        """
        # The installer records the data directory on the service key, so this
        # answer is available whether or not the service is currently running.
        dataDir = _serviceOption(kOptionDataDir)
        if dataDir:
            return dataDir
        state = self.readState()
        return None if state is None else state.get("data_dir")


    ###########################################################
    def readState(self):
        """ Read the service's published state file.

        @return  The state dictionary, or None when the service has not written
                 one (not running, or never started).
        """
        dataDir = _serviceOption(kOptionDataDir)
        if not dataDir:
            return None
        try:
            with open(os.path.join(dataDir, _kStateFile), 'r') as f:
                return json.load(f)
        except Exception:
            return None


    ###########################################################
    def setConfig(self, cfg, dataDir=None):
        """ Writes a configuration file which the service picks up at start
        time. The key names are kConfig*. If a key is missing a default value
        will be written.

        @param cfg      Dictionary containing the configuration.
        @param dataDir  Data directory path, so configuration access can happen
                        even w/o a connection to the service, otherwise None.
        @return         True if written successfully, False on error.
        """
        if dataDir is None:
            dataDir = self.dataDir()
            if not dataDir:
                return False
        cfgFile = os.path.join(dataDir, _kConfigFile)
        try:
            h = open(cfgFile, 'w')
            h.write("[%s]\n%s=%s\n%s=%s\n" % (
                    _kConfigSectionMain,
                    kConfigKeyAutoStart,
                    cfg.get(kConfigKeyAutoStart, _kDefaultSettings[kConfigKeyAutoStart]),
                    kConfigKeyBackend,
                    cfg.get(kConfigKeyBackend  , _kDefaultSettings[kConfigKeyBackend])))
            return True
        except:
            return False
        finally:
            try:
                h.close()
            except:
                pass


    ###########################################################
    def getConfig(self, dataDir=None):
        """ Picks up the current configuration.

        @param dataDir  Data directory path, so configuration access can happen
                        even w/o a connection to the service, otherwise None.
        @return         Configuration dictionary. None if n/a.
        """
        if dataDir is None:
            dataDir = self.dataDir()
            if not dataDir:
                return None
        try:
            cfgFile = os.path.join(dataDir, _kConfigFile)
            cp = ConfigParser(_kDefaultSettings)
            cp.read([cfgFile])
            result = {}
            for item in cp.items(_kConfigSectionMain):
                result[item[0]] = item[1]
            return result
        except:
            return None


    ###########################################################
    def getConfigOrDefaults(self, dataDir=None):
        """Return the current config or defaults on error.

        @param dataDir  Data directory path, so configuration access can happen
                        even w/o a connection to the service, otherwise None.
        @return         Configuration dictionary.
        """
        ret = self.getConfig(dataDir)
        if ret is None:
            ret = _kDefaultSettings

        return ret


###############################################################
def launchLog(msg):
    """Simple log function which appends to shlaunch.log in the temp folder,
    since we won't have logging enabled at early/service installation time.

    @param msg  The message to log. Timestamp will be prefixed automatically.
    @return     True if logged successfully. False on error.
    """
    try:
        tstamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        msg = "%s [LAUNCH] - %s\n" % (tstamp, msg)
        f = open(os.path.join(tempfile.gettempdir(), "shlaunch_usr.log"), "a")
        f.write(msg)
        return True
    except:
        return False
    finally:
        try:
            f.close()
        except:
            pass


###############################################################################
def _loadLaunchPlist(plistFile=_kDaemonPlistPath):
    """ Loads the plist which declares the service for OSX.

    @param plistFile  Location of the file. Default value for release/frozen.
    @return           Tuple (id,exe,rel) with the service registration name,
                      the executable path and the release, as found in the
                      plist. None if the data could not be loaded, either
                      because of the file to be missing or its format not being
                      understood.
    """
    if not os.path.isfile(plistFile):
        launchLog("plist file not found (%s)" % plistFile)
        return None
    try:
        root = ElementTree.parse(plistFile).getroot()
        if "plist" != root.tag:
            launchLog("no <plist> found in '%s'" % plistFile)
            return None
        dct = root.findall("./dict")[0]
        nxt = False
        for c in dct:
            if nxt:
                if c.tag == "string":
                    label = c.text
                    break
                launchLog("unknown label tag <%s>" % c.tag)
                return None
            if c.tag == "key" and c.text == "Label":
                nxt = True
        else:
            launchLog("no label tag found in '%s'" % plistFile)
            return None
        progArgs = dct.findall("./array/string")
        launch  = progArgs[0].text
        release = progArgs[1].text
        return (label, launch, release)
    except:
        launchLog("_loadLaunchPlist - UNCAUGHT ERROR (%s)" % sys.exc_info()[1])
        return None


###############################################################################
def _checkServicePlist(build):
    """Checks if the service is properly registered.

    @param  build  The build number, same as what to expect in the service
                   plist file if installed properly and being recent.
    @return        True the plist looks fine.
    """
    exeDir = os.path.dirname(sys.executable)
    shlaunchPath = os.path.join(exeDir, _kServiceExe)
    lpl = _loadLaunchPlist()
    if lpl is None:
        return False
    if lpl[0] != _kDaemonPlistName:
        launchLog("daemon name mismatch (%s)" % lpl[0])
        return False
    if lpl[1] != shlaunchPath:
        launchLog("daemon path mismatch (%s)" % lpl[1])
        return False
    if lpl[2] != build:
        launchLog("build mismatch (%s)" % lpl[2])
        return False
    return True


###############################################################################
def _activateMac(build, localDataDir):
    """ Service activation call for OSX. Asks the user for credentials run the
    service executable with administrator rights, so it can install the plist
    for the service and create the global user data directory or a symlink to
    to local one if needed. Once done we launch the service one more time under
    the current user's account, same as the OSX service launcher would do, plus
    telling it not to kill this process.

    @param  build         The build number, to ensure the service is compatible.
    @param  localDataDir  Potential legacy local directory to move.
    @return               True if activation worked, False if it failed.
    """
    if type(localDataDir) == str:
        localDataDir = localDataDir.encode('utf-8')
    exeDir = os.path.dirname(sys.executable)
    shlaunchPath = os.path.join(exeDir, _kServiceExe)
    params = []
    params.append(os.path.join(exeDir, _kSudoExe))
    params.append("--wait")
    params.append(shlaunchPath)
    params.append(build)
    params.append("--activate")
    params.append(str(os.getpid()))
    params.append(localDataDir)
    params.append(str(os.getuid()))
    params.append(getpass.getuser())
    try:
        launchLog("activating launch %s ..." % str(params))
        p = subprocess.Popen(params,
            stdin =subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True)
        for s in [p.stdin, p.stdout, p.stderr]:
            s.close()
        # there is no other way, we need to wait for the activation to finish,
        # since the user has the right to idle on the admin prompt as long as
        # she wants ...
        exitCode = p.wait()
        if 0 != exitCode:
            launchLog("activation exit code %d" % exitCode)
            return False
        launchLog("activation successful")
    except:
        launchLog("activate error %s (%s)" % (str(params), sys.exc_info()[1]))
        return False

    lpl = _loadLaunchPlist()
    if lpl is None:
        launchLog("activation did not yield the plist?!")
        return False
    params = []
    params.append(lpl[1])
    params.append(lpl[2])
    # NOTE: since we are the parent process of shlaunch we're not in danger of
    #       getting killed by it during the old-process-cleanup stage.
    try:
        launchLog("launching service %s ..." % str(params))
        p = subprocess.Popen(params,
            stdin =subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True)
        for s in [p.stdin, p.stdout, p.stderr]:
            s.close()
        launchLog("launch call issued")
    except:
        launchLog("launch error %s (%s)" % (str(params), sys.exc_info()[1]))
        return False
    return True


###############################################################
def launchCheckWin():
    """Checks if the service is running, under Windows. Need to do much less
    diligence here, since everything is based on the installer to get things
    correctly set up.

    @return  True if we have contact, False if anything is not right.
    """
    try:
        l = Launch()
        return l.open()
    except:
        return False
    finally:
        try:
            l.close()
        except:
            pass


###############################################################
def launchCheckMac(build, localDataDir, timeout):
    """Checks if the service is running. If not it will try to activate it.
    Also takes care about moving the data directory from a legacy location to
    the global spot.

    @param  build         The current build number, so we can detect mismatched
                          service instance being present.
    @param  localDataDir  Legacy data directory, for possible migration needed.
    @param  timeout       Number of seconds to wait for service to be ready.
    @return               True if things are in order and the service is ready
                          for being accessed via the Launch API. False if not.
    """
    activated = False
    if not _checkServicePlist(build):
        launchLog("service plist conflict, activating...")
        if not _activateMac(build, localDataDir):
            return False
        activated = True
    end = time.time() + timeout
    while time.time() < end:
        l = Launch()
        # check and see if the service is available
        try:
            if l.open():
                # is it the right build?
                lbuild = l.build()
                if build == lbuild:
                    # all good, we're up and ready
                    launchLog("launch build %s verified" % build)
                    return True
                launchLog("build is %s, expected %s" % (lbuild, build))
        finally:
            try:
                l.close()
            except:
                pass
        # do one activation attempt, the service executable will run with
        # admin privileges and try to set things straight and be up and
        # ready for us
        if not activated:
            launchLog("service n/a, activating...")
            tm = time.time()
            if not _activateMac(build, localDataDir):
                return False
            end += time.time() - tm
            activated = True
        # don't poll too quickly, process re-launch etc does take some time
        time.sleep(.5)
    return False


###############################################################################

# testing, both exercising all functions as well as checking to see if polling
# for the shutdown flag is feasible ...

def _testLaunch():
    launch = Launch()
    if not launch.open():
        print("cannot open")
        sys.exit(1)
    print("opened")
    print("data directory is '%s'" % launch.dataDir())
    print("pid: %s" % launch.pid())
    print("status: %s" % launch.status())
    print("shutdown: %s" % launch.shutdown())
    print("launch result: %s" % str(launch.do(killFirst=True)))
    time.sleep(1)
    print("status: %s" % launch.status())
    result = launch.close()
    if not result:
        print("cannot close (%d)" % result)
        sys.exit(1)
    print("closed")

def _testLaunchPerf():
    now = startedAt = time.time()
    stopAt = now + 5
    polls = 0
    while now < stopAt:
        for _ in range(10):
            launch = Launch()
            if not launch.open():
                print("CANNOT OPEN!")
                sys.exit(1)
            if launch.shutdown():
                print("SHUTDOWN?!")
                sys.exit(1)
            if not launch.close():
                print("CANNOT CLOSE!")
                sys.exit(1)
            polls += 1
        now = time.time()
    print("%.3f poll(s) per second" % (polls / (now - startedAt), ))

def _testConfig():
    launch = Launch()
    if not launch.open():
        print("open error")
        sys.exit(1)
    cfg = launch.getConfig()
    print("config >>> %s" % (str(cfg)))
    if not launch.setConfig({ kConfigKeyAutoStart: kConfigValueTrue }):
        print("cannot set config")
    print("config >>> %s" % (str(launch.getConfig())))
    if not launch.close():
        print("close error")
        sys.exit(1)

if __name__ == '__main__':
    _testLaunch()
    _testLaunchPerf()  # measured 46K polls in a Windows 8.1 VM
    _testConfig()
