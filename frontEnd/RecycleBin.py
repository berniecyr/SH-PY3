#! /usr/local/bin/python

"""
## @file
Move a file to the Windows Recycle Bin rather than deleting it outright.

A photo deleted from the Images tab by a stray key press can be restored
from the Recycle Bin; os.remove would lose it for good.
"""

# Python imports...
import ctypes
import os
import sys


_kFoDelete = 0x0003
_kFofSilent = 0x0004
_kFofNoConfirmation = 0x0010
_kFofAllowUndo = 0x0040
_kFofNoErrorUi = 0x0400
_kFofWantNukeWarning = 0x4000


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


##############################################################################
def recycle(path, hwnd=None):
    """Send one file to the Recycle Bin.

    A file the Recycle Bin cannot take -- on a network share, some removable
    drives, or too large -- would otherwise be deleted for good without a
    word; Windows asks first instead, and a No leaves the file in place.

    @param  path  Absolute path of an existing file.
    @param  hwnd  Window that owns that question, if any.
    @raise  OSError if it is missing or the shell refuses.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if sys.platform != 'win32':
        raise OSError('The Recycle Bin is only available on Windows.')
    op = _SHFILEOPSTRUCTW()
    op.hwnd = hwnd
    op.wFunc = _kFoDelete
    # pFrom is a list of NUL-terminated names ending in an empty one.
    op.pFrom = path + '\0'
    op.fFlags = (_kFofAllowUndo | _kFofNoConfirmation | _kFofSilent
                 | _kFofNoErrorUi | _kFofWantNukeWarning)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError('Could not move %s to the Recycle Bin (error 0x%x).'
                      % (path, result))
    if os.path.exists(path):
        raise OSError('%s is still there after moving it to the Recycle Bin.'
                      % path)
