#*****************************************************************************
#
# InstallService.py
#   Installs / removes / controls the SHLaunchPY3 Windows service.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" Install and control the SHLaunchPY3 service.

Run from an ELEVATED prompt (installing a service always needs administrator
rights). From an installed program:

    python\\SighthoundPy3c.exe -m launch.InstallService status

...and from a source checkout, with the venv's interpreter instead. The
installer itself runs:

    ... -m launch.InstallService install --local-system --data-dir <dir>
                                         --grant-user .\\Bernie

`install` records where the app lives (InstallDir), which interpreter to run it
with (PythonExe) and which data directory to use (DataDir) in the service's own
registry key, so the service does not have to guess any of it at start time.

Account: the service runs as LocalSystem, and is TOLD its data directory rather
than deriving one from a profile. That matters -- a service account's
%LOCALAPPDATA% is C:\\Windows\\System32\\config\\systemprofile\\..., so a back end
left to work it out for itself would build a second, empty set of databases,
enrollments and live-view files instead of using the user's. Everything that
resolves such a path now goes through appCommon.InstallPaths.getUserDataDir,
which reads the SV_DATA_DIR the service exports.

`--user`/`--password` still install the service under a real account, which is
what you want if video storage lives on a network share: LocalSystem cannot
authenticate to SMB as the user.
"""

import argparse
import getpass
import os
import subprocess
import sys

import ntsecuritycon
import win32security
import win32service
import win32serviceutil

from appCommon.InstallPaths import kServiceHostExe

from .SHLaunchService import SHLaunchService
from .SHLaunchService import kServiceName, kServiceDisplayName
from .SHLaunchService import kServiceDescription
from .SHLaunchService import kOptionInstallDir, kOptionPythonExe, kOptionDataDir
from .SHLaunchService import kControlRestartBackend, kControlStopBackend
from .SHLaunchService import kControlStartBackend
from .SHLaunchService import _defaultDataDir


###############################################################################

# Rights we grant the owning user so an UNELEVATED front end can start, stop and
# signal the service. In SDDL terms:
#   CC query-config   LC query-status   SW enumerate-dependents
#   RP start          WP stop           DT pause/continue
#   LO interrogate    CR user-defined-control (our custom codes)
#   RC read-control
_kUserServiceRights = "CCLCSWRPWPDTLOCRRC"


###############################################################################
def _repoRoot():
    """@return  The directory holding FrontEndLaunchpad.py (this file's parent)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


###############################################################################
def _defaultPythonExe():
    """The interpreter the service should run the back end with.

    An installed build has a private interpreter (python\\SighthoundPy3.exe);
    a checkout has the venv beside it. Either way the point is the same: the
    service must use the interpreter that has the CUDA torch build and every
    other pinned dependency, not whatever python happens to be on PATH.

    @return  Absolute path to an interpreter.
    """
    from appCommon.InstallPaths import getInterpreter, isInstalledBuild
    if isInstalledBuild():
        return getInterpreter(gui=True)

    venvPython = os.path.join(_repoRoot(), "venv", "Scripts", "python.exe")
    if os.path.isfile(venvPython):
        return venvPython
    return sys.executable


###############################################################################
def _pythonServiceExe():
    """Locate the executable that hosts the service.

    An installed build ships pywin32's pythonservice.exe copied next to the
    private interpreter as SighthoundPy3Service.exe and stamped as "Sighthound
    Py3" -- so the service is identifiable in Task Manager, and, more
    importantly, so it can find python312.dll. The host imports that DLL, and
    Windows searches the executable's directory first; left in
    site-packages\\win32 it only starts when some Python is on PATH.

    @return  Absolute path, or None when pywin32 is not installed properly.
    """
    try:
        import win32serviceutil as _wsu
        # win32serviceutil lives in site-packages\win32\lib, while the exe sits
        # one level up in site-packages\win32 -- check both, plus sys.prefix, so
        # this keeps working if pywin32's layout shifts.
        libDir = os.path.dirname(os.path.abspath(_wsu.__file__))
    except Exception:
        return None

    from appCommon.InstallPaths import getPrivatePythonDir
    directories = [
        getPrivatePythonDir() or sys.prefix,
        libDir,
        os.path.dirname(libDir),
        os.path.join(sys.prefix, "Lib", "site-packages", "win32"),
    ]
    for name in (kServiceHostExe, "pythonservice.exe"):
        for directory in directories:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


###############################################################################
def _serviceClassString(installDir):
    """The PythonClass value that lets the service host find our class.

    pywin32's service host does not import by name alone. It takes the
    service's PythonClass registry value, splits it at the LAST backslash,
    inserts that directory at the front of sys.path, and only then splits the
    remainder at its last dot into module and class (see
    LoadPythonServiceClass in pywin32's PythonService.cpp).

    That split is the only hook we have, and we need it: the service host is
    the private interpreter, whose sys.path covers python\\Lib and
    python\\Lib\\site-packages but NOT the install root the `launch` package
    lives in. A bare "launch.SHLaunchService.SHLaunchService" installs
    perfectly happily and then fails to start with "Python could not import the
    service's module", because nothing ever put the root on the path.

    So hand it the root as the directory part and the dotted module as the
    file part -- "C:\\...\\Sighthound Video Py3\\launch.SHLaunchService.SHLaunchService".
    The host imports "launch.SHLaunchService" as a package submodule, which is
    what SHLaunchService.py's relative imports need.

    @param  installDir  App root, the directory holding the launch package.
    @return             Value to store as the service's PythonClass.
    """
    return os.path.join(installDir,
                        "launch.SHLaunchService.SHLaunchService")


###############################################################################
def _checkPywin32Dlls():
    """Warn when pywin32's DLLs are not where a service can find them.

    The service host needs pythoncomXX.dll / pywintypesXX.dll. In a venv they
    sit in site-packages\\pywin32_system32, which is NOT on a service's DLL
    search path -- pywin32's own postinstall script copies them into System32.
    Without that the service installs fine and then fails to start with a bare
    "Error 1053", which is a miserable thing to debug.

    An installed build sidesteps the whole problem: make_payload.py copies
    those DLLs next to the service host, and a directory holding the executable
    is always searched. So check there first, and only fall back to demanding
    the System32 copy -- an installer has no business writing to System32, and
    doing it would break any other pywin32 on the machine.

    @return  True when the DLLs look reachable.
    """
    dllName = "pythoncom%d%d.dll" % (sys.version_info[0], sys.version_info[1])

    hostExe = _pythonServiceExe()
    if hostExe and os.path.isfile(os.path.join(os.path.dirname(hostExe),
                                               dllName)):
        return True

    sysDll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", dllName)
    if os.path.isfile(sysDll):
        return True

    print("WARNING: %s not found." % sysDll)
    print("         pythonservice.exe needs pywin32's DLLs on the system DLL")
    print("         path or the service fails to start with 'Error 1053'.")
    print("         Fix it once, from this same elevated prompt:")
    print("             %s %s -install" % (
        sys.executable,
        os.path.join(os.path.dirname(sys.executable),
                     "pywin32_postinstall.py")))
    return False


###############################################################################
def _grantUserServiceRights(account):
    """Let `account` start/stop/signal the service without elevation.

    The default service DACL only lets administrators control a service, so an
    ordinary front end could not ask for a back-end restart. We append one ACE
    for the owning user rather than replacing the descriptor, so the stock
    entries stay intact.

    @param  account  User account, e.g. ".\\Bernie" or "DOMAIN\\user".
    @return          True on success.
    """
    try:
        sid, _domain, _type = win32security.LookupAccountName(None,
                                                              _bareAccount(account))
        sidStr = win32security.ConvertSidToStringSid(sid)

        scm = win32service.OpenSCManager(None, None,
                                         win32service.SC_MANAGER_CONNECT)
        try:
            # READ_CONTROL/WRITE_DAC are generic securable-object rights, so
            # they live in ntsecuritycon -- win32service only defines the
            # service-specific ones (SERVICE_*, SC_MANAGER_*).
            handle = win32service.OpenService(
                scm, kServiceName,
                ntsecuritycon.READ_CONTROL | ntsecuritycon.WRITE_DAC)
            try:
                sd = win32service.QueryServiceObjectSecurity(
                    handle, win32security.DACL_SECURITY_INFORMATION)
                sddl = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    sd, win32security.SDDL_REVISION_1,
                    win32security.DACL_SECURITY_INFORMATION)

                ace = "(A;;%s;;;%s)" % (_kUserServiceRights, sidStr)
                if ace in sddl:
                    return True
                # Append our ACE at the end of the DACL portion.
                sddl = sddl + ace if sddl.startswith("D:") else "D:" + ace

                newSd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                    sddl, win32security.SDDL_REVISION_1)
                win32service.SetServiceObjectSecurity(
                    handle, win32security.DACL_SECURITY_INFORMATION, newSd)
                return True
            finally:
                win32service.CloseServiceHandle(handle)
        finally:
            win32service.CloseServiceHandle(scm)
    except Exception as e:
        print("WARNING: could not grant service rights to %s: %r" % (account, e))
        print("         The service will still work, but the front end will")
        print("         need elevation to start or restart the back end.")
        return False


###############################################################################
def _bareAccount(account):
    """Strip a leading '.\\' so LookupAccountName resolves a local user.

    @param  account  e.g. ".\\Bernie", "Bernie", "DOMAIN\\user".
    @return          Name suitable for LookupAccountName.
    """
    if account.startswith(".\\"):
        return account[2:]
    return account


###############################################################################
def installService(account, password, installDir=None, pythonExe=None,
                   dataDir=None, autoStart=True, grantUser=None):
    """Install (or reconfigure) the service.

    @param  account     Account to run as, e.g. ".\\Bernie", or None for
                        LocalSystem (which is what the installer uses, since it
                        needs no password and therefore no prompt).
    @param  password    That account's password; ignored for LocalSystem.
    @param  installDir  App root; defaults to this checkout.
    @param  pythonExe   Interpreter; defaults to the private python / venv.
    @param  dataDir     Data directory; defaults to the user's app data dir.
    @param  autoStart   Start the service at boot.
    @return             0 on success, non-zero on failure.
    """
    installDir = os.path.abspath(installDir or _repoRoot())
    pythonExe = os.path.abspath(pythonExe or _defaultPythonExe())
    dataDir = os.path.abspath(dataDir or _defaultDataDir())

    launchpad = os.path.join(installDir, "FrontEndLaunchpad.py")
    if not os.path.isfile(launchpad):
        print("ERROR: %s does not look like an install directory (no "
              "FrontEndLaunchpad.py)." % installDir)
        return 2
    if not os.path.isfile(pythonExe):
        print("ERROR: interpreter not found: %s" % pythonExe)
        return 2

    _checkPywin32Dlls()

    startType = (win32service.SERVICE_AUTO_START if autoStart
                 else win32service.SERVICE_DEMAND_START)

    print("Installing %s" % kServiceName)
    print("  account    : %s" % (account or "LocalSystem"))
    print("  installDir : %s" % installDir)
    print("  pythonExe  : %s" % pythonExe)
    print("  dataDir    : %s" % dataDir)

    exeName = _pythonServiceExe()
    classString = _serviceClassString(installDir)
    try:
        win32serviceutil.InstallService(
            pythonClassString=classString,
            serviceName=kServiceName,
            displayName=kServiceDisplayName,
            description=kServiceDescription,
            startType=startType,
            userName=account,
            password=password,
            exeName=exeName)
    except Exception as e:
        # Already installed: reconfigure in place rather than making the caller
        # remove/reinstall (which would lose the service's SID and rights).
        if "exists" not in str(e).lower():
            print("ERROR: install failed: %r" % (e,))
            return 3
        print("Service already exists; updating its configuration.")
        try:
            win32serviceutil.ChangeServiceConfig(
                pythonClassString=classString,
                serviceName=kServiceName,
                displayName=kServiceDisplayName,
                description=kServiceDescription,
                startType=startType,
                userName=account,
                password=password,
                exeName=exeName)
        except Exception as e2:
            print("ERROR: reconfigure failed: %r" % (e2,))
            return 3

    # Everything the service needs to know, recorded where it can read it
    # without importing the app.
    win32serviceutil.SetServiceCustomOption(kServiceName, kOptionInstallDir,
                                            installDir)
    win32serviceutil.SetServiceCustomOption(kServiceName, kOptionPythonExe,
                                            pythonExe)
    win32serviceutil.SetServiceCustomOption(kServiceName, kOptionDataDir,
                                            dataDir)

    # Under LocalSystem there is no owning user to grant rights to, so grant
    # them to whoever installed us -- otherwise the front end could not ask for
    # a back-end restart without elevation.
    _grantUserServiceRights(grantUser or account or
                            (".\\%s" % getpass.getuser()))

    print("Installed. Start it with:")
    print("    %s -m launch.InstallService start" % sys.executable)
    return 0


###############################################################################
def removeService(stopFirst=True):
    """Stop (optionally) and uninstall the service.

    @return  0 on success.
    """
    if stopFirst:
        try:
            win32serviceutil.StopService(kServiceName)
            print("Stopped %s." % kServiceName)
        except Exception:
            pass
    try:
        win32serviceutil.RemoveService(kServiceName)
        print("Removed %s." % kServiceName)
        return 0
    except Exception as e:
        print("ERROR: remove failed: %r" % (e,))
        return 3


###############################################################################
def serviceStatus():
    """Print the service's state and what it says about the back end.

    @return  0 when the service is installed, 1 when it is not.
    """
    try:
        status = win32serviceutil.QueryServiceStatus(kServiceName)
    except Exception as e:
        print("%s is NOT installed (%r)." % (kServiceName, e))
        return 1

    states = {
        win32service.SERVICE_STOPPED: "STOPPED",
        win32service.SERVICE_START_PENDING: "START_PENDING",
        win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
        win32service.SERVICE_RUNNING: "RUNNING",
        win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",
        win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
        win32service.SERVICE_PAUSED: "PAUSED",
    }
    print("%s: %s" % (kServiceName, states.get(status[1], status[1])))
    for option in (kOptionInstallDir, kOptionPythonExe, kOptionDataDir):
        print("  %-11s: %s" % (
            option, win32serviceutil.GetServiceCustomOption(kServiceName,
                                                            option, "<unset>")))

    # The state file is the service's own view of the back end.
    from .Launch import Launch
    state = Launch().readState()
    if state:
        print("  backend    : pid=%s running=%s (state written %s)" % (
            state.get("backend_pid"), state.get("backend_running"),
            state.get("updated_ms")))
    else:
        print("  backend    : no state file")
    return 0


###############################################################################
def main(argv=None):
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Install and control the %s service." % kServiceName)
    parser.add_argument("action",
                        choices=["install", "remove", "start", "stop",
                                 "restart", "status", "restart-backend",
                                 "stop-backend", "start-backend"])
    parser.add_argument("--user", default=None,
                        help="Account to run as, e.g. '.\\Bernie'. Needs that "
                             "account's password; use it when the back end "
                             "must reach network storage as that user.")
    parser.add_argument("--password", default=None,
                        help="Account password. Prompted for when omitted.")
    parser.add_argument("--local-system", action="store_true",
                        help="Run as LocalSystem (no password, no prompt). "
                             "This is what the installer does; pair it with "
                             "--data-dir so the back end uses the user's data "
                             "directory rather than the system profile.")
    parser.add_argument("--grant-user", default=None,
                        help="Account granted start/stop rights on the "
                             "service, so the app can restart the back end "
                             "without elevation. Defaults to the caller.")
    parser.add_argument("--install-dir", default=None)
    parser.add_argument("--python-exe", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--manual", action="store_true",
                        help="Install with manual start instead of automatic.")
    args = parser.parse_args(argv)

    if args.action == "install":
        if args.local_system:
            # userName=None is how the SCM spells LocalSystem.
            account, password = None, None
        else:
            account = args.user or (".\\%s" % getpass.getuser())
            password = args.password
            if password is None:
                password = getpass.getpass("Password for %s: " % account)
        return installService(account, password, args.install_dir,
                              args.python_exe, args.data_dir,
                              autoStart=not args.manual,
                              grantUser=args.grant_user)

    if args.action == "remove":
        return removeService()

    if args.action == "status":
        return serviceStatus()

    try:
        if args.action == "start":
            win32serviceutil.StartService(kServiceName)
            print("Started.")
        elif args.action == "stop":
            win32serviceutil.StopService(kServiceName)
            print("Stopped.")
        elif args.action == "restart":
            win32serviceutil.RestartService(kServiceName)
            print("Restarted.")
        elif args.action == "restart-backend":
            win32serviceutil.ControlService(kServiceName,
                                            kControlRestartBackend)
            print("Back-end restart signalled.")
        elif args.action == "stop-backend":
            win32serviceutil.ControlService(kServiceName, kControlStopBackend)
            print("Back-end stop signalled.")
        elif args.action == "start-backend":
            win32serviceutil.ControlService(kServiceName, kControlStartBackend)
            print("Back-end start signalled.")
    except Exception as e:
        print("ERROR: %s failed: %r" % (args.action, e))
        return 3
    return 0


###############################################################################
if __name__ == '__main__':
    sys.exit(main())
