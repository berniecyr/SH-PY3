#! /usr/local/bin/python

#*****************************************************************************
#
# ImageThumbCache.py
#     The Image tab's on-disk thumbnail cache: layout, index and cleanup.
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

r"""
## @file

The Image tab's thumbnail cache, <dataDir>\usermedia\thumbs.

### Layout

Each thumbnail is named sha1(path | size-mtime | thumbSize).jpg and lives in
a subfolder named after the first two hex characters of that name:

    thumbs\3f\3fa9c0...e1.jpg

The hash is uniformly random, so the 256 subfolders fill evenly: about 60
files each at 15,000 thumbnails, about 4,000 each at a million.  That keeps
every folder small enough for Explorer, backup and antivirus tools, while the
app's own lookup is unchanged -- it computes the name and opens it, it never
lists a folder.

Before this the cache was one flat folder.  A flat file found on a lookup miss
is moved into its subfolder there and then, and the background pass below
moves the rest.

### Index

The name is a one-way hash, so the file alone cannot say which photo it came
from.  thumbs\index.db maps each name to its source path and the size/mtime
it was made from.  It is the cache's own file, deliberately NOT usermedia.db:
the grid writes a row per new thumbnail from its worker thread and that must
never contend with the analysis writes there.

### Cleanup

Only ever deletes a thumbnail whose original is gone:

  * the source file no longer exists, or
  * the source file still exists but has been changed since (size or mtime
    differ), so this thumbnail is of a version that no longer exists and its
    name can never be asked for again.

Never by age.  A source on a drive or share that is not reachable right now
(unplugged USB disk, NAS offline) is left alone -- missing volume is not a
missing file.  A thumbnail with no index row (made before the index existed
and not matched by the backfill) is also left alone: we cannot tell what it
belongs to, so we do not guess.

The pass runs once per session, on a daemon thread that waits
_kMaintenanceDelaySecs after the Image view is built, runs at Windows
background priority (low CPU *and* low I/O priority), and pauses between
batches.  It never touches the UI thread, and a lookup racing a deletion just
regenerates the thumbnail.
"""

# Python imports...
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
import traceback

# Common 3rd-party imports...

# Toolbox imports...

# Local imports...


# Constants...

# Wait this long after the Image view is built before touching the disk, so the
# pass never competes with front-end startup.
_kMaintenanceDelaySecs = 60

# Work in batches this big, sleeping _kBatchPauseSecs between them.
_kBatchSize = 200
_kBatchPauseSecs = 0.05

# A leftover .tmp from a write interrupted by a crash; anything older than
# this cannot belong to a write in progress.
_kStaleTmpSecs = 3600

_kIndexFile = "index.db"

_kThumbNameRe = re.compile(r"^[0-9a-f]{40}\.jpg$")

# Windows SetThreadPriority: lowers CPU *and* disk I/O priority for the
# calling thread.
_kThreadModeBackgroundBegin = 0x00010000


# Globals...

_cacheDir = None
_madeShards = set()
_shardLock = threading.Lock()

_local = threading.local()

_maintenanceThread = None
_maintenanceStop = threading.Event()
_maintenanceLock = threading.Lock()


##############################################################################
def getThumbCacheDir():
    """Where cached thumbnails live.

    Under the data directory, NOT under videos\\ -- DiskCleaner walks that tree
    and deletes anything in it with no clipdb row.

    @return  Absolute path; created if needed.
    """
    global _cacheDir
    if _cacheDir is None:
        from appCommon.InstallPaths import getUserDataDir
        path = os.path.join(getUserDataDir(), "usermedia", "thumbs")
        os.makedirs(path, exist_ok=True)
        _cacheDir = path
    return _cacheDir


##############################################################################
def _stamp(size, mtime):
    """The size/mtime part of a cache key."""
    return "%d-%d" % (size, mtime)


##############################################################################
def _keyFor(path, stamp, thumbSize):
    """The cache file name for a source path, stamp and thumbnail size."""
    digest = hashlib.sha1(
        ("%s|%s|%d" % (path, stamp, thumbSize)).encode("utf-8", "replace")
    ).hexdigest()
    return digest + ".jpg"


##############################################################################
def cacheKey(path, thumbSize):
    """A cache file name that changes when the source or the size does.

    @param  path       Absolute path of the source file.
    @param  thumbSize  Thumbnail size in pixels.
    @return name       The file name (no folder).
    @return stat       (size, mtime) it was made from, or None if the source
                       could not be read.
    """
    try:
        st = os.stat(path)
        stat = (st.st_size, int(st.st_mtime))
        stamp = _stamp(*stat)
    except OSError:
        stat = None
        stamp = "missing"
    return _keyFor(path, stamp, thumbSize), stat


##############################################################################
def shardPath(name, create=False):
    """Where the thumbnail with this file name lives.

    @param  name    A cache file name from cacheKey().
    @param  create  True to make sure its subfolder exists.
    @return         Absolute path.
    """
    shard = name[:2]
    folder = os.path.join(getThumbCacheDir(), shard)
    if create and shard not in _madeShards:
        os.makedirs(folder, exist_ok=True)
        with _shardLock:
            _madeShards.add(shard)
    return os.path.join(folder, name)


##############################################################################
def findThumb(name):
    """The cached thumbnail with this name, if there is one.

    Also adopts a thumbnail from the old flat layout by moving it into its
    subfolder, so the first lookup after upgrading finds it.

    @param  name  A cache file name from cacheKey().
    @return       Absolute path of an existing file, or None.
    """
    target = shardPath(name)
    if os.path.isfile(target):
        return target
    legacy = os.path.join(getThumbCacheDir(), name)
    if os.path.isfile(legacy):
        try:
            os.replace(legacy, shardPath(name, create=True))
            return target
        except OSError:
            # Being moved by the background pass right now, or locked.  Use it
            # where it is this time.
            if os.path.isfile(legacy):
                return legacy
            if os.path.isfile(target):
                return target
    return None


##############################################################################
def _openIndex():
    """This thread's connection to the index, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(os.path.join(getThumbCacheDir(), _kIndexFile),
                               timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute('''CREATE TABLE IF NOT EXISTS thumbs (
                            name TEXT PRIMARY KEY, path TEXT NOT NULL,
                            size INTEGER, mtime INTEGER,
                            thumbSize INTEGER NOT NULL)''')
        conn.commit()
        _local.conn = conn
    return conn


##############################################################################
def _closeIndex():
    """Close this thread's connection to the index, if it has one."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        _local.conn = None
        try:
            conn.close()
        except Exception:
            pass


##############################################################################
def recordThumb(name, path, stat, thumbSize, logger=None):
    """Note which source a new thumbnail belongs to, for the cleanup pass.

    Never raises: a thumbnail that is not indexed is merely never cleaned up.

    @param  name       Its cache file name.
    @param  path       The source file.
    @param  stat       (size, mtime) from cacheKey().
    @param  thumbSize  Thumbnail size in pixels.
    @param  logger     Optional logger for failures.
    """
    if stat is None:
        return
    try:
        conn = _openIndex()
        conn.execute("INSERT OR REPLACE INTO thumbs VALUES (?,?,?,?,?)",
                     (name, path, stat[0], stat[1], thumbSize))
        conn.commit()
    except Exception:
        if logger is not None:
            logger.info("ImageThumbCache: could not index %s: %s"
                        % (path, traceback.format_exc()))


##############################################################################
def startMaintenance(logger, thumbSize):
    """Start the once-per-session background pass, if it has not run yet.

    Returns at once; the thread sleeps _kMaintenanceDelaySecs before doing
    anything.

    @param  logger     A logger.
    @param  thumbSize  The grid's current thumbnail size, used to match old
                       flat thumbnails to usermedia.db rows.
    """
    global _maintenanceThread
    with _maintenanceLock:
        if _maintenanceThread is not None:
            return
        _maintenanceStop.clear()
        _maintenanceThread = threading.Thread(
            target=_maintenanceMain, args=(logger, thumbSize),
            name="ImageThumbCache", daemon=True)
        _maintenanceThread.start()


##############################################################################
def stopMaintenance():
    """Ask the background pass to stop.  Bounded wait; safe to call again."""
    _maintenanceStop.set()
    thread = _maintenanceThread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


##############################################################################
def _lowerPriority():
    """Run the calling thread at Windows background CPU and I/O priority."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(),
                                   _kThreadModeBackgroundBegin)
    except Exception:
        pass


##############################################################################
def _pause():
    """Between batches.  @return True if we have been asked to stop."""
    return _maintenanceStop.wait(_kBatchPauseSecs)


##############################################################################
def _maintenanceMain(logger, thumbSize):
    """The background pass: backfill, migrate, clean.  Own thread."""
    if _maintenanceStop.wait(_kMaintenanceDelaySecs):
        return
    _lowerPriority()
    start = time.time()
    try:
        indexed = _backfillIndex(thumbSize)
        moved = _migrateFlat() if not _maintenanceStop.is_set() else 0
        removed = _cleanOrphans() if not _maintenanceStop.is_set() else 0
        logger.info("ImageThumbCache: maintenance done in %.1fs -- %d indexed "
                    "from usermedia.db, %d moved into subfolders, %d removed "
                    "(original gone)%s"
                    % (time.time() - start, indexed, moved, removed,
                       ", stopped early" if _maintenanceStop.is_set() else ""))
    except Exception:
        logger.error("ImageThumbCache: maintenance failed: %s"
                     % traceback.format_exc())
    finally:
        _closeIndex()


##############################################################################
def _backfillIndex(thumbSize):
    """Index thumbnails made before the index existed, where we can.

    usermedia.db knows the path, size and mtime of every scanned file, which
    is everything the cache name was computed from.  For each, if a thumbnail
    under that name exists (flat or sharded) and is not indexed, index it.
    Thumbnails of files browsed but never scanned cannot be matched and stay
    unindexed, which means kept.

    Runs only until the index has had a backfill once.

    @param  thumbSize  Size to compute names for.
    @return            Rows added.
    """
    conn = _openIndex()
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    if conn.execute("SELECT 1 FROM meta WHERE key='backfilled'").fetchone():
        return 0

    try:
        from backEnd.UserMediaDb import getUserMediaDbPath
        dbPath = getUserMediaDbPath()
    except Exception:
        dbPath = None
    if not dbPath or not os.path.isfile(dbPath):
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('backfilled', '1')")
        conn.commit()
        return 0

    # Read-only, so this can never block or damage the analysis writer.
    from urllib.parse import quote
    src = sqlite3.connect("file:%s?mode=ro"
                          % quote(dbPath.replace("\\", "/"), safe="/:"),
                          uri=True, timeout=10)
    try:
        tables = set(r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        queries = []
        if "file_locations" in tables:
            queries.append("SELECT path, size, mtime FROM file_locations")
        if "files" in tables:
            queries.append("SELECT path, size, mtime FROM files")
        rows = set()
        for q in queries:
            rows.update(r for r in src.execute(q)
                        if r[0] and r[1] is not None and r[2] is not None)
    finally:
        src.close()

    added = 0
    batch = []
    for i, (path, size, mtime) in enumerate(rows):
        name = _keyFor(path, _stamp(size, mtime), thumbSize)
        if (os.path.isfile(shardPath(name))
                or os.path.isfile(os.path.join(getThumbCacheDir(), name))):
            batch.append((name, path, size, mtime, thumbSize))
        if i % _kBatchSize == _kBatchSize - 1:
            if batch:
                conn.executemany("INSERT OR IGNORE INTO thumbs VALUES (?,?,?,?,?)", batch)
                conn.commit()
                added += len(batch)
                batch = []
            if _pause():
                return added
    if batch:
        conn.executemany("INSERT OR IGNORE INTO thumbs VALUES (?,?,?,?,?)", batch)
        added += len(batch)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('backfilled', '1')")
    conn.commit()
    return added


##############################################################################
def _migrateFlat():
    """Move thumbnails from the old flat layout into their subfolders.

    Also removes .tmp files a crash left behind; those are half-written
    thumbnails, not thumbnails of anything.

    @return  Files moved.
    """
    root = getThumbCacheDir()
    moved = 0
    count = 0
    now = time.time()
    with os.scandir(root) as it:
        for entry in it:
            if not entry.is_file():
                continue
            name = entry.name
            try:
                if _kThumbNameRe.match(name):
                    os.replace(entry.path, shardPath(name, create=True))
                    moved += 1
                elif (".tmp" in name
                        and now - entry.stat().st_mtime > _kStaleTmpSecs):
                    os.remove(entry.path)
            except OSError:
                pass
            count += 1
            if count % _kBatchSize == 0 and _pause():
                break
    return moved


##############################################################################
def _volumeReachable(path):
    """Whether the drive or share holding path is there right now.

    @param  path  Absolute path of a source file.
    @return       True if its drive root (C:\\, \\\\server\\share\\) exists.
    """
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return os.path.isdir(os.sep)
    return os.path.isdir(drive + os.sep)


##############################################################################
def _originalGone(path, size, mtime):
    """Whether the file a thumbnail was made from no longer exists.

    @return  True only when we are sure: the file is missing from a reachable
             volume, or it is there but has changed since.  False on any doubt.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        # Missing file, or missing volume?  Only the first counts.
        return _volumeReachable(path)
    except OSError:
        # Access denied, share flaking, ... -- not proof of anything.
        return False
    return (st.st_size, int(st.st_mtime)) != (size, mtime)


##############################################################################
def _cleanOrphans():
    """Delete indexed thumbnails whose original is gone.

    @return  Thumbnails removed.
    """
    conn = _openIndex()
    removed = 0
    lastName = ""
    while not _maintenanceStop.is_set():
        rows = conn.execute("SELECT name, path, size, mtime FROM thumbs "
                            "WHERE name > ? ORDER BY name LIMIT ?",
                            (lastName, _kBatchSize)).fetchall()
        if not rows:
            break
        lastName = rows[-1][0]
        gone = []
        for name, path, size, mtime in rows:
            if _originalGone(path, size, mtime):
                gone.append(name)
        for name in gone:
            for candidate in (shardPath(name),
                              os.path.join(getThumbCacheDir(), name)):
                try:
                    os.remove(candidate)
                except FileNotFoundError:
                    pass
                except OSError:
                    # Open in the grid right now; try again next session.
                    break
            else:
                conn.execute("DELETE FROM thumbs WHERE name=?", (name,))
                removed += 1
        conn.commit()
        if _pause():
            break
    return removed
