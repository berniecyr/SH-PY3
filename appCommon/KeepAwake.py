#*****************************************************************************
#
# KeepAwake.py
#   Hold Windows out of Modern Standby while the back end is running.
#
#*****************************************************************************
#
# This file is part of the Sighthound Video Python 3 port.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
#*****************************************************************************

""" A system-required power request for the lifetime of the back end.

On a Modern Standby PC ("Standby (S0 Low Power Idle)" in `powercfg /a`) the
display turning off IS standby entry -- the "Sleep: Never" setting does not
stop it.  Once standby reaches its Desktop Activity Moderator phase, Windows
suspends every desktop application and throttles services to about one second
of activity every 30 seconds.  Measured 2026-09-24 02:20-05:44 on this fleet:
every camera went 18-30 s between frames, detector requests took 11-20 s, the
recorder wrote a 13 s backlog under the wrong wall time, and a person at
080_FrontStep was stored as 'object'.  The back end never asked to stay awake.

Microsoft's "Prepare software for modern standby": power requests block the
first (NoCS) phase "indefinitely on AC power, and for up to 5 minutes on DC
power".  So a PowerRequestSystemRequired held by this process keeps the PC
working while the display still switches off.  Windows drops the request on a
user-initiated sleep (power button, lid, Start > Sleep), so the user can still
put the machine to sleep on purpose.

Deliberately NOT a display request: screens should still go dark.

Standard library only, and a no-op off Windows.
"""

import sys


###############################################################################

_kReason = "Sighthound Video is recording and analysing cameras"

# minwinbase.h / winnt.h
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_PowerRequestSystemRequired = 1
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_INVALID_HANDLE_VALUE = -1


###############################################################################
class KeepAwakeHandle(object):
    """What acquire() holds; pass it back to release().

    @ivar  method  'powerRequest', 'executionState', or None when nothing is
                   held (off Windows, or both mechanisms failed).
    @ivar  error   Why nothing is held, or None.
    """
    def __init__(self, method=None, handle=None, error=None):
        self.method = method
        self.error = error
        self._handle = handle

    def isHeld(self):
        """@return held  True if a request is currently keeping the PC awake."""
        return self.method is not None

    def describe(self):
        """@return text  One line for the log."""
        if self.method == 'powerRequest':
            return ("keep-awake: system power request held "
                    "(display may still turn off)")
        if self.method == 'executionState':
            return ("keep-awake: system execution state held "
                    "(display may still turn off)")
        return "keep-awake: not held (%s)" % (self.error or "not requested")


###############################################################################
def acquire(reason=_kReason):
    """Ask Windows to keep the system out of standby until release().

    Tries PowerCreateRequest/PowerSetRequest first, because it carries a reason
    string that `powercfg /requests` shows.  Falls back to
    SetThreadExecutionState, which belongs to the CALLING thread -- so call this
    from a thread that outlives the need (the back end's main thread).

    @param  reason  Shown by `powercfg /requests`.
    @return handle  A KeepAwakeHandle; check isHeld()/describe().  Never raises.
    """
    if sys.platform != 'win32':
        return KeepAwakeHandle(error="not Windows")

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:
        return KeepAwakeHandle(error="ctypes unavailable: %s" % e)

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    firstError = None

    try:
        class _ReasonContext(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG),
                        ("Flags", wintypes.DWORD),
                        ("SimpleReasonString", wintypes.LPWSTR)]

        create = kernel32.PowerCreateRequest
        create.argtypes = [ctypes.POINTER(_ReasonContext)]
        create.restype = wintypes.HANDLE
        setReq = kernel32.PowerSetRequest
        setReq.argtypes = [wintypes.HANDLE, ctypes.c_int]
        setReq.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        ctx = _ReasonContext(_POWER_REQUEST_CONTEXT_VERSION,
                             _POWER_REQUEST_CONTEXT_SIMPLE_STRING,
                             reason)
        h = create(ctypes.byref(ctx))
        if not h or h == ctypes.c_void_p(_INVALID_HANDLE_VALUE).value:
            raise OSError(ctypes.get_last_error(), "PowerCreateRequest failed")
        if not setReq(h, _PowerRequestSystemRequired):
            err = ctypes.get_last_error()
            close(h)
            raise OSError(err, "PowerSetRequest failed")
        return KeepAwakeHandle('powerRequest', h)
    except Exception as e:
        firstError = str(e)

    try:
        ste = kernel32.SetThreadExecutionState
        ste.argtypes = [wintypes.DWORD]
        ste.restype = wintypes.DWORD
        if ste(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) == 0:
            raise OSError(ctypes.get_last_error(),
                          "SetThreadExecutionState failed")
        return KeepAwakeHandle('executionState')
    except Exception as e:
        return KeepAwakeHandle(error="%s; fallback: %s" % (firstError, e))


###############################################################################
def release(handle):
    """Drop what acquire() took.  Safe to call more than once, or with None.

    @param  handle  A KeepAwakeHandle from acquire().
    """
    if handle is None or not handle.isHeld():
        return
    method, h = handle.method, handle._handle
    handle.method, handle._handle = None, None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        if method == 'powerRequest':
            clear = kernel32.PowerClearRequest
            clear.argtypes = [wintypes.HANDLE, ctypes.c_int]
            clear.restype = wintypes.BOOL
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            clear(h, _PowerRequestSystemRequired)
            close(h)
        elif method == 'executionState':
            ste = kernel32.SetThreadExecutionState
            ste.argtypes = [wintypes.DWORD]
            ste.restype = wintypes.DWORD
            ste(_ES_CONTINUOUS)
    except Exception:
        # Process exit releases both mechanisms anyway.
        pass
