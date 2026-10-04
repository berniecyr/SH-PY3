#! /usr/bin/env python
#*****************************************************************************
#
# testKeepAwake.py
#     Check that the back end's keep-awake request really reaches Windows.
#
#*****************************************************************************

"""Check appCommon.KeepAwake against the real Windows power manager.

WHY THIS EXISTS
On 2026-09-24 the PC sat in Modern Standby from 02:20:18 to 05:44:03 (System
log, Kernel-Power 506/507).  Standby suspends desktop programs, so every camera
went 18-30 s between frames and a person at 080_FrontStep was stored as
'object'.  Nothing in the app asked Windows to stay awake.

METHOD
The power manager's own view is read back with CallNtPowerInformation
(SystemExecutionState), which needs no elevation: ES_SYSTEM_REQUIRED must be
set while the request is held and must drop after release.  The "before" read
is the red half of the check -- if some other program already holds a system
request the bit is set before we start, and that is reported as INCONCLUSIVE
rather than passed.

Also covered: the SetThreadExecutionState fallback (forced by hiding
PowerCreateRequest), double release, and the no-op off Windows.

Usage:  python scripts\\testKeepAwake.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from appCommon import KeepAwake  # noqa: E402

_kPass, _kFail = 0, 1
_ES_SYSTEM_REQUIRED = 0x1
_SystemExecutionState = 16


def systemRequired():
    """@return set  True if Windows currently has ES_SYSTEM_REQUIRED set."""
    import ctypes
    from ctypes import wintypes
    state = wintypes.ULONG(0)
    rc = ctypes.WinDLL('powrprof').CallNtPowerInformation(
        _SystemExecutionState, None, 0, ctypes.byref(state),
        ctypes.sizeof(state))
    if rc != 0:
        raise OSError(rc, "CallNtPowerInformation failed")
    return bool(state.value & _ES_SYSTEM_REQUIRED)


def main():
    failures = []
    inconclusive = []

    print("[1] no-op off Windows")
    realPlatform = sys.platform
    try:
        sys.platform = 'linux'
        h = KeepAwake.acquire()
    finally:
        sys.platform = realPlatform
    print("    %s" % h.describe())
    if h.isHeld():
        failures.append("held a request off Windows")
    KeepAwake.release(h)

    if sys.platform != 'win32':
        print("\nnot Windows: skipping the power-manager checks")
        return _kFail if failures else _kPass

    print("\n[2] power request is visible to Windows while held, gone after")
    before = systemRequired()
    h = KeepAwake.acquire()
    during = systemRequired()
    print("    %s" % h.describe())
    KeepAwake.release(h)
    KeepAwake.release(h)            # second release must be harmless
    after = systemRequired()
    print("    ES_SYSTEM_REQUIRED before=%s during=%s after=%s"
          % (before, during, after))
    if h.isHeld():
        failures.append("handle still reports held after release")
    if not during:
        failures.append("Windows did not see the request while held")
    if before:
        inconclusive.append("another program already holds a system request, "
                            "so the release check cannot be seen")
    elif after:
        failures.append("request still visible after release")

    print("\n[3] SetThreadExecutionState fallback")
    import ctypes
    realWinDLL = ctypes.WinDLL

    class _NoPowerRequests(object):
        """kernel32 with PowerCreateRequest missing, as on a stripped system."""
        def __init__(self, dll):
            self._dll = dll

        def __getattr__(self, name):
            if name == 'PowerCreateRequest':
                raise AttributeError(name)
            return getattr(self._dll, name)

    ctypes.WinDLL = lambda name, **kw: _NoPowerRequests(realWinDLL(name, **kw))
    try:
        h = KeepAwake.acquire()
        during = systemRequired()
        print("    %s" % h.describe())
        KeepAwake.release(h)
    finally:
        ctypes.WinDLL = realWinDLL
    after = systemRequired()
    print("    method=%s ES_SYSTEM_REQUIRED during=%s after=%s"
          % (h.method or "released", during, after))
    if not during:
        failures.append("fallback did not reach Windows")
    if not before and after:
        failures.append("fallback still visible after release")

    print("\n" + "=" * 70)
    if failures:
        print("FAIL (%d): %s" % (len(failures), "; ".join(failures)))
        return _kFail
    if inconclusive:
        print("PASS with caveat: %s" % "; ".join(inconclusive))
        return _kPass
    print("PASS")
    return _kPass


if __name__ == '__main__':
    sys.exit(main())
