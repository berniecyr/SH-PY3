#!/usr/bin/env python

#*****************************************************************************
#
# LegacyMigration.py
#   One-time migration of camera database, backend prefs, and rules from the
#   legacy Python 2 install (%LOCALAPPDATA%\Sighthound Video) into the Python 3
#   install (%LOCALAPPDATA%\Sighthound Video Py3) on first launch.
#
#   The legacy files are protocol-0 pickles written by Python 2 in Windows text
#   mode, so every '\n' opcode separator was stored as '\r\n'.  Python 3 reads
#   the files in binary and the stray '\r' bytes break unpickling.  Restoring
#   '\n' separators yields valid Python 3 pickles (protocol 0 escapes all string
#   data, so raw CRLF only ever appears as an opcode separator).
#
#   Old files are only ever READ; nothing in the legacy folder is modified.
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

import os
import pickle

from appCommon.CommonStrings import kLegacyAppName
from appCommon.CommonStrings import kPrefsFile, kCamDbFile
from appCommon.CommonStrings import kRuleDir, kRuleExt, kQueryExt


# Backend prefs whose values are machine-specific absolute paths from the old
# install (e.g. another user's profile, or an external drive).  We drop them so
# the fresh install falls back to its own default storage locations.
_kPathPrefsToReset = ('dataDir', 'videoDir')


##############################################################################
def _normalizePickleBytes(data):
    """Restore LF opcode separators in a legacy text-mode protocol-0 pickle.

    @param  data  Raw bytes read from a legacy pickle file.
    @return       Bytes with '\\r\\n' replaced by '\\n'.
    """
    return data.replace(b"\r\n", b"\n")


##############################################################################
def _hasExistingConfig(dataDir):
    """Return True if dataDir already holds a camera db, prefs, or any rules.

    Used to ensure we only migrate into a genuinely fresh install.
    """
    if os.path.isfile(os.path.join(dataDir, kCamDbFile)):
        return True
    if os.path.isfile(os.path.join(dataDir, kPrefsFile)):
        return True
    ruleDir = os.path.join(dataDir, kRuleDir)
    if os.path.isdir(ruleDir):
        for name in os.listdir(ruleDir):
            if name.endswith(kRuleExt) or name.endswith(kQueryExt):
                return True
    return False


##############################################################################
def _convertPickleFile(srcPath, dstPath):
    """Convert one legacy pickle file into the new folder.

    Normalizes line endings, verifies the result unpickles, then writes it.
    The validated bytes are written as-is (still a valid Python 3 pickle); the
    app rewrites them in native binary form the next time it saves.

    @param  srcPath  Legacy file (read only).
    @param  dstPath  Destination in the new data dir.
    """
    with open(srcPath, "rb") as f:
        data = _normalizePickleBytes(f.read())
    pickle.loads(data)                      # validate; raises on corruption
    with open(dstPath, "wb") as f:
        f.write(data)


##############################################################################
def _convertBackEndPrefs(srcPath, dstPath):
    """Convert backend prefs, dropping machine-specific storage paths.

    @param  srcPath  Legacy backEndPrefs (read only).
    @param  dstPath  Destination backEndPrefs.
    """
    with open(srcPath, "rb") as f:
        prefs = pickle.loads(_normalizePickleBytes(f.read()))
    if not isinstance(prefs, dict):
        raise ValueError("legacy backEndPrefs is not a dict")
    for key in _kPathPrefsToReset:
        prefs.pop(key, None)
    with open(dstPath, "wb") as f:
        pickle.dump(prefs, f)


##############################################################################
def _convertRules(oldRuleDir, newRuleDir, log):
    """Convert every .rule/.query file from the legacy rules folder.

    @param  oldRuleDir  Legacy rules dir (read only).
    @param  newRuleDir  Destination rules dir (created if needed).
    @param  log         A (level, msg, **kw) logging callable.
    @return             Number of files successfully converted.
    """
    if not os.path.isdir(newRuleDir):
        os.makedirs(newRuleDir, exist_ok=True)
    count = 0
    for name in sorted(os.listdir(oldRuleDir)):
        if not (name.endswith(kRuleExt) or name.endswith(kQueryExt)):
            continue
        src = os.path.join(oldRuleDir, name)
        if not os.path.isfile(src):
            continue
        try:
            _convertPickleFile(src, os.path.join(newRuleDir, name))
            count += 1
        except Exception:
            log('warning', "Failed to migrate rule file %s" % name,
                exc_info=True)
    return count


# Marker written once the user has been offered the legacy import (whichever
# choice they make), so we never prompt again on subsequent launches.
_kImportCheckedMarker = ".legacy_import_checked"


##############################################################################
def _markerPath(dataDir):
    return os.path.join(dataDir, _kImportCheckedMarker)


##############################################################################
def markLegacyImportChecked(newDataDir):
    """Record that the user has been offered the legacy import.

    Called after the user makes either choice (import or start fresh) so the
    prompt does not reappear on later launches.
    """
    try:
        if not os.path.isdir(newDataDir):
            os.makedirs(newDataDir, exist_ok=True)
        with open(_markerPath(newDataDir), "w") as f:
            f.write("1")
    except Exception:
        pass


##############################################################################
def getLegacyImportSource(newDataDir):
    """Return the legacy data dir to import from, or None if not applicable.

    A path is returned only when the user has not been asked before, the new
    install has no config yet, and a legacy folder containing importable files
    exists.  This is a pure check with no side effects.

    @param  newDataDir  The Python 3 user data directory.
    @return             The legacy data dir path, or None.
    """
    # Already offered the import on a previous launch.
    if os.path.isfile(_markerPath(newDataDir)):
        return None

    # Only offer for a genuinely fresh install.
    if _hasExistingConfig(newDataDir):
        return None

    oldDataDir = os.path.join(os.path.dirname(newDataDir), kLegacyAppName)

    # Safety: never treat our own folder as the legacy source.
    if os.path.normcase(os.path.abspath(oldDataDir)) == \
       os.path.normcase(os.path.abspath(newDataDir)):
        return None
    if not os.path.isdir(oldDataDir):
        return None

    oldCamDb = os.path.join(oldDataDir, kCamDbFile)
    oldPrefs = os.path.join(oldDataDir, kPrefsFile)
    oldRules = os.path.join(oldDataDir, kRuleDir)
    if not (os.path.isfile(oldCamDb) or os.path.isfile(oldPrefs) or
            os.path.isdir(oldRules)):
        return None

    return oldDataDir


##############################################################################
def importLegacyData(newDataDir, logger=None):
    """Convert legacy camera db, backend prefs, and rules into newDataDir.

    Imports the camera database, backend prefs (with machine-specific storage
    paths reset to this install's defaults), and rules/queries from the legacy
    Python 2 folder.  Legacy files are only ever READ, never altered.  Videos
    and recorded detections (objdb2/clipdb) are intentionally not imported.

    @param  newDataDir  The Python 3 user data directory.
    @param  logger      Optional logger with info()/warning() methods.
    @return             True if an import was performed, else False.
    """
    def log(level, msg, **kw):
        if logger is not None:
            getattr(logger, level)(msg, **kw)

    oldDataDir = getLegacyImportSource(newDataDir)
    if oldDataDir is None:
        return False

    log('info', "Importing legacy config from %s into %s"
        % (oldDataDir, newDataDir))
    if not os.path.isdir(newDataDir):
        os.makedirs(newDataDir, exist_ok=True)

    oldCamDb = os.path.join(oldDataDir, kCamDbFile)
    oldPrefs = os.path.join(oldDataDir, kPrefsFile)
    oldRules = os.path.join(oldDataDir, kRuleDir)

    # 1. Camera database.
    if os.path.isfile(oldCamDb):
        try:
            _convertPickleFile(oldCamDb, os.path.join(newDataDir, kCamDbFile))
            log('info', "Imported camera database (camdb)")
        except Exception:
            log('warning', "Failed to import camera database", exc_info=True)

    # 2. Backend prefs (storage paths reset to defaults per import policy).
    if os.path.isfile(oldPrefs):
        try:
            _convertBackEndPrefs(oldPrefs, os.path.join(newDataDir, kPrefsFile))
            log('info', "Imported backend prefs (storage paths reset to "
                "this install's defaults)")
        except Exception:
            log('warning', "Failed to import backend prefs", exc_info=True)

    # 3. Rules and queries.
    if os.path.isdir(oldRules):
        n = _convertRules(oldRules, os.path.join(newDataDir, kRuleDir), log)
        log('info', "Imported %d rule/query file(s)" % n)

    return True
