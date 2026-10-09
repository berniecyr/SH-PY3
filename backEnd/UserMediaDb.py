#! /usr/local/bin/python

#*****************************************************************************
#
# UserMediaDb.py
#     Detections for the user's OWN photos and videos. Separate from objdb2.
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
The Image view's own database: <dataDir>\usermedia\usermedia.db.

Deliberately NOT objdb2, and deliberately not anywhere DiskCleaner walks.

Why its own file rather than the existing tables:

  * DataManager.tidyObjectTable() deletes `objects` rows that have no `motion`
    rows.  Every row this feature writes would be exactly that -- there is no
    motion track behind a photograph -- so they would disappear on the cleaner's
    next daily pass.
  * objdb2 stores boxes in a fixed 320x240 space, which is meaningless for a
    4032x3024 photo.  Here they are normalised 0..1, so a box can be drawn on
    the original at any size.
  * An `objects` row is shaped around a camera location and a wall-clock
    window.  A file has neither.

Why it is safe from cleanup, checked against backEnd/DiskCleaner.py: the cleaner
only ever DELETES inside videoDir (<dataStorage>\videos\archive), tmpDir and
remoteDir, plus rows in the two database paths it is handed at spawn.  It is
also given configDir -- the data-dir root, where this file lives -- but touches
it read-only and only by exact filename (disableOrphanScan, the corruption flag,
backEndPrefs).  Nothing walks the data-dir root.

The companion rule, which matters more: **no user file path may ever be written
into clipdb**.  DiskCleaner._deleteFile does os.path.join(self._videoDir, file),
and Python returns an absolute `file` unchanged -- so a row there pointing at
someone's photo is a delete of that photo.

Schema versioning follows the house style: no PRAGMA user_version (it is 0 in
every database in this tree), just column sniffing plus ALTER TABLE.
"""

# Python imports...
import os
import hashlib
import sqlite3
import time

# Common 3rd-party imports...

# Toolbox imports...

# Local imports...


# Constants...

# Bumped when a change to the ANALYSIS invalidates stored results, so a file
# analysed by an older build can be spotted and re-run.  Not the schema
# version -- the schema upgrades itself by sniffing columns.
kAnalyzerVersion = 1

# File name under the data directory.
kUserMediaDbFile = "usermedia.db"


def fingerprint(path):
    """Hash exact bytes, rejecting files modified while being read."""
    before = os.stat(path)
    with open(path, 'rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = os.stat(path)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError('File changed while hashing: %s' % path)
    return digest, after


def mergeTags(*values):
    """Keep distinct semicolon-separated tags, in first-seen order."""
    result, seen = [], set()
    for value in values:
        for tag in (value or '').split(';'):
            tag = tag.strip()
            if tag and tag.casefold() not in seen:
                result.append(tag)
                seen.add(tag.casefold())
    return '; '.join(result)

_kFileColumns = [
    ("path", "TEXT"), ("size", "INTEGER"), ("mtime", "INTEGER"),
    ("contentHash", "TEXT"), ("kind", "TEXT"),
    ("width", "INTEGER"), ("height", "INTEGER"), ("durationMs", "INTEGER"),
    ("captureMs", "INTEGER"), ("analyzedMs", "INTEGER"),
    ("analyzerVersion", "INTEGER"), ("modelSig", "TEXT"),
    ("thumbPath", "TEXT"), ("error", "TEXT"),
    ("description_tags", "TEXT"), ("description_ai", "TEXT"),
    ("faceModelRan", "INTEGER"), ("nudityModelRan", "INTEGER"),
    ("exifDate", "INTEGER"), ("exifTime", "INTEGER"),
]

# Optional-model flags: 1 when that model ran during the file's analysis, 0
# when it was skipped because it was off or not loaded, NULL if never analysed.
_kModelRanColumns = ("faceModelRan", "nudityModelRan")

# EXIF "date taken", split so either half can be searched alone: exifDate is
# YYYYMMDD (20260115) and exifTime is HHMMSS (193005), the camera's local
# clock.  NULL when the file has no EXIF date.  See backfillExifDates.

_kDetectionColumns = [
    ("fileUid", "INTEGER"), ("atMs", "INTEGER"),
    ("type", "TEXT"), ("subType", "TEXT"), ("conf", "REAL"),
    ("x1", "REAL"), ("y1", "REAL"), ("x2", "REAL"), ("y2", "REAL"),
    ("faceName", "TEXT"), ("faceConf", "REAL"), ("faceDetConf", "REAL"),
    ("gender", "TEXT"), ("age", "INTEGER"),
    ("nudity", "INTEGER"), ("nudityDetail", "TEXT"),
]


##############################################################################
def getUserMediaDbPath(dataDir=None):
    """Where the database lives.

    @return  Absolute path, under the data directory that every process agrees
             on -- never built from expanduser("~"), which under the service is
             the service account's profile rather than the user's.
    """
    if dataDir is None:
        from appCommon.InstallPaths import getUserDataDir
        dataDir = getUserDataDir()
    return os.path.join(dataDir, 'usermedia', kUserMediaDbFile)


##############################################################################
class UserMediaDb(object):
    """Detections for the user's own files."""

    ###########################################################
    def __init__(self, logger=None):
        """Initializer for UserMediaDb.

        @param  logger  Optional logger.
        """
        self._logger = logger
        self._conn = None
        self._path = None


    ###########################################################
    def open(self, filePath=None, timeout=15):
        """Open (creating if needed) and bring the schema up to date.

        @param  filePath  Path to the database; defaults to the standard one.
        @param  timeout   SQLite busy timeout, seconds.
        """
        if filePath is None:
            filePath = getUserMediaDbPath()
        self._path = filePath

        directory = os.path.dirname(filePath)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        self._conn = sqlite3.connect(filePath, timeout=timeout,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function('UM_CASEFOLD', 1, lambda value: str(value).casefold())
        self._conn.create_function('UM_BASENAME', 1, os.path.basename)
        from backEnd.UserMediaSearch import wholeWordMatch
        self._conn.create_function('UM_WORD', 2, wholeWordMatch)
        self._conn.create_function('UM_TAG', 2, lambda text, tag: int(
            str(tag).casefold() in [p.strip().casefold() for p in str(text or '').split(';')]))
        # WAL for the same reason the other three databases use it: a reader
        # and the writer can run at once, which is what keeps a scan from
        # freezing the browsing UI.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._createTables()
        self._upgradeTables()
        return self


    ###########################################################
    def close(self):
        """Close the connection, if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


    ###########################################################
    def _createTables(self):
        """Create the tables if this is a new database."""
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS files (
                uid INTEGER PRIMARY KEY,
                path TEXT UNIQUE,
                size INTEGER, mtime INTEGER, contentHash TEXT,
                kind TEXT,
                width INTEGER, height INTEGER, durationMs INTEGER,
                captureMs INTEGER,
                analyzedMs INTEGER, analyzerVersion INTEGER, modelSig TEXT,
                thumbPath TEXT, error TEXT,
                description_tags TEXT, description_ai TEXT,
                faceModelRan INTEGER, nudityModelRan INTEGER,
                exifDate INTEGER, exifTime INTEGER)""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                uid INTEGER PRIMARY KEY,
                fileUid INTEGER,
                atMs INTEGER,
                type TEXT, subType TEXT, conf REAL,
                x1 REAL, y1 REAL, x2 REAL, y2 REAL,
                faceName TEXT, faceConf REAL, faceDetConf REAL,
                gender TEXT, age INTEGER,
                nudity INTEGER, nudityDetail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS IDX_DET_FILE "
                  "ON detections(fileUid)")
        c.execute("CREATE INDEX IF NOT EXISTS IDX_DET_TYPE "
                  "ON detections(type)")
        c.execute("CREATE INDEX IF NOT EXISTS IDX_FILES_PATH "
                  "ON files(path)")
        c.commit()


    ###########################################################
    def _upgradeTables(self):
        """Add any column this build expects and the file does not have.

        Column sniffing plus ALTER, matching DataManager and ClipManager.
        PRAGMA user_version is 0 everywhere in this tree, so introducing a
        version number here would make this database the odd one out.
        """
        added = set()
        for table, columns in (("files", _kFileColumns),
                               ("detections", _kDetectionColumns)):
            have = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(%s)" % table)}
            for name, sqlType in columns:
                if name not in have:
                    added.add(name)
                    self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                       % (table, name, sqlType))
                    if self._logger is not None:
                        self._logger.info("UserMediaDb: added %s.%s"
                                          % (table, name))

        # One-time backfill when the model flags first appear: records
        # analysed before they existed are marked as done for both models.
        for name in _kModelRanColumns:
            if name in added:
                cur = self._conn.execute(
                    "UPDATE files SET %s = 1 "
                    "WHERE analyzedMs IS NOT NULL AND error IS NULL"
                    % name)
                if self._logger is not None:
                    self._logger.info("UserMediaDb: marked %d existing "
                                      "record(s) %s=1" % (cur.rowcount, name))
        self._conn.commit()

        # One content record, many physical locations. Existing records remain
        # valid; their content is verified lazily when analysis encounters them.
        self._conn.execute('''CREATE TABLE IF NOT EXISTS file_locations (
            path TEXT PRIMARY KEY COLLATE NOCASE, fileUid INTEGER NOT NULL,
            size INTEGER, mtime INTEGER, mtimeNs INTEGER, contentHash TEXT)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS IDX_LOCATION_FILE ON file_locations(fileUid)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS IDX_FILES_HASH ON files(contentHash)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS IDX_FILES_SIZE ON files(size)')
        self._conn.execute('''INSERT OR IGNORE INTO file_locations(path,fileUid,size,mtime)
                             SELECT path,uid,size,mtime FROM files''')
        self._conn.commit()


    ###########################################################
    def getFile(self, path):
        """Look up one file's row.

        @param  path  Absolute path.
        @return row   An sqlite3.Row, or None.
        """
        row = self._conn.execute('''SELECT f.*, l.path AS locationPath,
            l.size AS locationSize, l.mtime AS locationMtime, l.mtimeNs
            FROM files f JOIN file_locations l ON l.fileUid=f.uid
            WHERE l.path=?''', (path,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(path=row['locationPath'], size=row['locationSize'],
                      mtime=row['locationMtime'])
        return result


    def getLocations(self, path):
        row = self.getFile(path)
        if row is None:
            return []
        return [r[0] for r in self._conn.execute(
            'SELECT path FROM file_locations WHERE fileUid=? ORDER BY path', (row['uid'],))]


    def forgetFile(self, path):
        """Drop one path from the database after its file has been deleted.

        Other copies of the same content keep the record, its analysis and
        its descriptions; the last copy takes the record with it.

        @param  path  Absolute path that no longer exists.
        @return int   How many other indexed copies remain.
        """
        c = self._conn
        c.execute('BEGIN IMMEDIATE')
        try:
            row = self.getFile(path)
            if row is None:
                c.rollback()
                return 0
            uid = row['uid']
            c.execute('DELETE FROM file_locations WHERE path=?', (path,))
            others = [r[0] for r in c.execute(
                'SELECT path FROM file_locations WHERE fileUid=? ORDER BY path',
                (uid,))]
            if others:
                # files.path names one copy; hand it to a copy that still exists.
                c.execute('UPDATE files SET path=? WHERE uid=? '
                          'AND path=? COLLATE NOCASE', (others[0], uid, path))
            else:
                c.execute('DELETE FROM detections WHERE fileUid=?', (uid,))
                c.execute('DELETE FROM files WHERE uid=?', (uid,))
            c.commit()
            return len(others)
        except Exception:
            c.rollback()
            raise


    def renameFile(self, path, newName, allCopies=False):
        """Rename physical copies and their aliases, preserving content identity.

        Preflight every destination before moving anything. Hold the database
        write transaction throughout and undo filesystem moves on failure.
        A rename of one alias does not split a byte-identical content record.
        """
        if (not newName or newName in ('.', '..') or newName != newName.strip()
                or newName.endswith('.') or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in newName)
                or len(newName) > 255):
            raise ValueError('Enter a valid filename without a folder path or trailing spaces/dots.')
        reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {
            prefix + str(i) for prefix in ('COM', 'LPT') for i in range(1, 10)}
        if newName.split('.')[0].upper() in reserved:
            raise ValueError('This filename is reserved by Windows.')
        if os.path.splitext(newName)[1].lower() != os.path.splitext(path)[1].lower():
            raise ValueError('Keep the existing file extension when renaming.')
        c = self._conn
        moved = []
        c.execute('BEGIN IMMEDIATE')
        try:
            row = self.getFile(path)
            sources = self.getLocations(path) if allCopies and row else [path]
            changes = {source: os.path.join(os.path.dirname(source), newName)
                       for source in sources}
            targets = set()
            digest = row['contentHash'] if row else None
            for source, target in changes.items():
                if not os.path.isfile(source) or os.path.islink(source):
                    raise ValueError('File is missing or is a symbolic link: ' + source)
                targetKey = os.path.normcase(os.path.abspath(target))
                if targetKey in targets:
                    raise ValueError('Two copies would have the same destination: ' + target)
                targets.add(targetKey)
                caseOnly = targetKey == os.path.normcase(os.path.abspath(source))
                if not caseOnly and (os.path.lexists(target) or self.getFile(target)):
                    raise FileExistsError('The destination already exists or is indexed: ' + target)
                actual, _ = fingerprint(source)
                if digest and actual != digest:
                    raise ValueError('File contents changed. Analyze this file again before renaming: ' + source)
                digest = actual
            for source, target in changes.items():
                if source == target:
                    continue
                # On Windows os.rename refuses to overwrite an existing file.
                os.rename(source, target)
                moved.append((source, target))
                c.execute('UPDATE file_locations SET path=? WHERE path=?', (target, source))
                c.execute('UPDATE files SET path=? WHERE path=? COLLATE NOCASE', (target, source))
            c.commit()
            return changes
        except Exception as exc:
            rollbackErrors = []
            for source, target in reversed(moved):
                try:
                    os.rename(target, source)
                except OSError as rollbackError:
                    rollbackErrors.append('%s -> %s: %s' % (target, source, rollbackError))
            c.rollback()
            if rollbackErrors:
                raise RuntimeError('Rename failed (%s). Some files could not be restored: %s'
                                   % (exc, '; '.join(rollbackErrors))) from exc
            raise


    def _mergeContentRows(self, target, source):
        """Combine verified identities without losing existing user text."""
        c = self._conn
        a = c.execute('SELECT * FROM files WHERE uid=?', (target,)).fetchone()
        b = c.execute('SELECT * FROM files WHERE uid=?', (source,)).fetchone()
        paragraphs = list(dict.fromkeys(v for v in (a['description_ai'], b['description_ai']) if v))
        c.execute('UPDATE files SET description_tags=?, description_ai=? WHERE uid=?',
                  (mergeTags(a['description_tags'], b['description_tags']), '\n\n'.join(paragraphs), target))
        # Keep the newest analysis, never append duplicate detections.
        if (b['analyzedMs'] or 0) > (a['analyzedMs'] or 0):
            c.execute('DELETE FROM detections WHERE fileUid=?', (target,))
            c.execute('UPDATE detections SET fileUid=? WHERE fileUid=?', (target, source))
            columns = ('kind', 'width', 'height', 'durationMs', 'captureMs',
                       'analyzedMs', 'analyzerVersion', 'modelSig', 'error',
                       'faceModelRan', 'nudityModelRan', 'exifDate', 'exifTime')
            c.execute('UPDATE files SET '+','.join(name+'=?' for name in columns)+' WHERE uid=?',
                      tuple(b[name] for name in columns)+(target,))
        c.execute('DELETE FROM detections WHERE fileUid=?', (source,))
        c.execute('UPDATE file_locations SET fileUid=? WHERE fileUid=?', (target, source))
        c.execute('DELETE FROM files WHERE uid=?', (source,))


    def registerContent(self, path, _verifyCandidates=True):
        """Verify identity on analysis/import; names alone never cause a merge."""
        digest, stat = fingerprint(path)
        c = self._conn
        # Cached hashes describe the bytes at the previous analysis. Verify
        # prospective sibling locations so an externally replaced copy cannot
        # acquire unrelated detections merely through an outdated hash index.
        if _verifyCandidates:
            siblings = c.execute('''SELECT l.path FROM file_locations l
                JOIN files f ON f.uid=l.fileUid WHERE f.contentHash=?''', (digest,)).fetchall()
            for sibling in siblings:
                other = sibling['path']
                if os.path.normcase(other) == os.path.normcase(path):
                    continue
                try:
                    otherHash, unused = fingerprint(other)
                except OSError:
                    continue  # Keep historical locations, but never infer new ones.
                if otherHash != digest:
                    self.registerContent(other, _verifyCandidates=False)
        with c:
            current = self.getFile(path)
            # Index older same-size records before looking for a duplicate.
            legacy = c.execute('SELECT uid,path FROM files WHERE contentHash IS NULL AND size=?',
                               (stat.st_size,)).fetchall()
            for row in legacy:
                try:
                    oldHash, oldStat = fingerprint(row['path'])
                except OSError:
                    continue
                c.execute('UPDATE files SET contentHash=? WHERE uid=?', (oldHash, row['uid']))
                c.execute('UPDATE file_locations SET contentHash=?,size=?,mtime=?,mtimeNs=? WHERE path=?',
                          (oldHash, oldStat.st_size, int(oldStat.st_mtime), oldStat.st_mtime_ns, row['path']))
            current = self.getFile(path)
            if current and current['contentHash'] not in (None, digest):
                # A changed copy must no longer share another location's data.
                locations = self.getLocations(path)
                if len(locations) > 1:
                    replacement = next(p for p in locations if p.lower() != path.lower())
                    c.execute('UPDATE files SET path=? WHERE uid=?', (replacement, current['uid']))
                    c.execute('DELETE FROM file_locations WHERE path=?', (path,))
                    current = None
                else:
                    c.execute('DELETE FROM detections WHERE fileUid=?', (current['uid'],))
                    c.execute('UPDATE files SET contentHash=?, analyzedMs=NULL, '
                              'faceModelRan=NULL, nudityModelRan=NULL, '
                              'exifDate=NULL, exifTime=NULL WHERE uid=?',
                              (digest, current['uid']))
            candidates = c.execute('SELECT uid FROM files WHERE contentHash=? ORDER BY uid', (digest,)).fetchall()
            if current and current['contentHash'] is None:
                c.execute('UPDATE files SET contentHash=? WHERE uid=?', (digest, current['uid']))
                if current['uid'] not in [r['uid'] for r in candidates]:
                    candidates.append({'uid': current['uid']})
            if candidates:
                uid = candidates[0]['uid']
                for row in candidates[1:]:
                    self._mergeContentRows(uid, row['uid'])
                if current and current['uid'] != uid and current['uid'] not in [r['uid'] for r in candidates]:
                    self._mergeContentRows(uid, current['uid'])
            else:
                cur = c.execute('INSERT INTO files(path,contentHash,size,mtime) VALUES (?,?,?,?)',
                                (path, digest, stat.st_size, int(stat.st_mtime)))
                uid = cur.lastrowid
            c.execute('''INSERT INTO file_locations(path,fileUid,size,mtime,mtimeNs,contentHash)
                VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                fileUid=excluded.fileUid,size=excluded.size,mtime=excluded.mtime,
                mtimeNs=excluded.mtimeNs,contentHash=excluded.contentHash''',
                (path, uid, stat.st_size, int(stat.st_mtime), stat.st_mtime_ns, digest))
        return uid


    ###########################################################
    def getDescriptions(self, path):
        """Return editable descriptions, including for files not yet analysed."""
        row = self.getFile(path)
        return {name: (row[name] or "") if row is not None else ""
                for name in ("description_tags", "description_ai")}


    ###########################################################
    def saveDescriptions(self, path, tags, description):
        """Save user text without changing analysis state or detections.

        saveResult deliberately leaves these columns alone on re-analysis.
        Callers must serialize access to this connection, as for saveResult.
        """
        with self._conn:
            row = self.getFile(path)
            if row is not None:
                self._conn.execute('UPDATE files SET description_tags=?,description_ai=? WHERE uid=?',
                                   (tags, description, row['uid']))
                return
            self._conn.execute("""
                INSERT INTO files (path, description_tags, description_ai)
                VALUES (?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    description_tags=excluded.description_tags,
                    description_ai=excluded.description_ai""",
                (path, tags, description))
            self._conn.execute('INSERT OR IGNORE INTO file_locations(path,fileUid) SELECT path,uid FROM files WHERE path=?', (path,))


    ###########################################################
    def needsAnalysis(self, path, modelSig):
        """Should this file be (re-)analysed?

        True when we have never seen it, when it has changed on disk since we
        did, or when the models/thresholds behind the stored result are not the
        ones configured now.

        @param  path      Absolute path.
        @param  modelSig  Signature of the current analysis configuration.
        @return bool
        """
        row = self.getFile(path)
        if row is None or row["analyzedMs"] is None:
            return True
        if row["analyzerVersion"] != kAnalyzerVersion:
            return True
        if row["modelSig"] != modelSig:
            return True
        try:
            stat = os.stat(path)
        except OSError:
            return False
        # Size and mtime together: mtime alone misses a same-second edit, and
        # hashing every file in a folder to find that out costs more than the
        # case is worth.
        return (row["size"] != stat.st_size
                or row["mtime"] != int(stat.st_mtime)
                or (row.get('mtimeNs') is not None and row['mtimeNs'] != stat.st_mtime_ns))


    ###########################################################
    def saveResult(self, path, result):
        """Store one file's analysis, replacing anything previously stored.

        @param  path    Absolute path of the file.
        @param  result  Dict from UserMediaAnalysis: kind, width, height,
                        durationMs, captureMs, modelSig, error, detections,
                        faceModelRan, nudityModelRan, exifDate, exifTime.
        @return uid     The file's row id.
        """
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, int(stat.st_mtime)
        except OSError:
            size, mtime = None, None

        # Every normal Image-view analysis passes through this identity check.
        if os.path.isfile(path):
            self.registerContent(path)
        row = self.getFile(path)
        canonical = (self._conn.execute('SELECT path FROM files WHERE uid=?',
                     (row['uid'],)).fetchone()[0] if row else path)
        c = self._conn
        c.execute("""
            INSERT INTO files (path, size, mtime, kind, width, height,
                               durationMs, captureMs, analyzedMs,
                               analyzerVersion, modelSig, error,
                               faceModelRan, nudityModelRan,
                               exifDate, exifTime)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size, mtime=excluded.mtime,
                kind=excluded.kind, width=excluded.width,
                height=excluded.height, durationMs=excluded.durationMs,
                captureMs=excluded.captureMs,
                analyzedMs=excluded.analyzedMs,
                analyzerVersion=excluded.analyzerVersion,
                modelSig=excluded.modelSig, error=excluded.error,
                faceModelRan=excluded.faceModelRan,
                nudityModelRan=excluded.nudityModelRan,
                exifDate=excluded.exifDate, exifTime=excluded.exifTime""",
            (canonical, size, mtime, result.get("kind"), result.get("width"),
             result.get("height"), result.get("durationMs"),
             result.get("captureMs"), int(time.time() * 1000),
             kAnalyzerVersion, result.get("modelSig"), result.get("error"),
             1 if result.get("faceModelRan") else 0,
             1 if result.get("nudityModelRan") else 0,
             result.get("exifDate"), result.get("exifTime")))

        row = c.execute("SELECT uid FROM files WHERE path = ?",
                        (canonical,)).fetchone()
        uid = row["uid"]
        c.execute('INSERT OR IGNORE INTO file_locations(path,fileUid,size,mtime) VALUES (?,?,?,?)',
                  (path, uid, size, mtime))

        # Replace rather than append: a re-analysis supersedes, and appending
        # would double every detection each time the file was re-run.
        c.execute("DELETE FROM detections WHERE fileUid = ?", (uid,))
        for det in result.get("detections", []):
            c.execute("""
                INSERT INTO detections (fileUid, atMs, type, subType, conf,
                                        x1, y1, x2, y2,
                                        faceName, faceConf, faceDetConf,
                                        gender, age, nudity, nudityDetail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, det.get("atMs", 0), det.get("type"), det.get("subType"),
                 det.get("conf"), det.get("x1"), det.get("y1"),
                 det.get("x2"), det.get("y2"),
                 det.get("faceName"), det.get("faceConf"),
                 det.get("faceDetConf"), det.get("gender"), det.get("age"),
                 1 if det.get("nudity") else 0, det.get("nudityDetail")))
        c.commit()
        return uid


    ###########################################################
    def backfillExifDates(self, progressFn=None):
        """Fill exifDate/exifTime from each file's EXIF where still empty.

        Reads every copy of a record until one has an EXIF date, so a record
        whose first path has moved is still filled from another.  Safe to run
        again: records already filled, or without EXIF, are left as they are.

        @param  progressFn  Optional f(done, total).
        @return (checked, filled)
        """
        from backEnd.UserMediaAnalysis import exifDateTaken, exifDateTimeFields
        c = self._conn
        uids = [r['uid'] for r in c.execute(
            'SELECT uid FROM files WHERE exifDate IS NULL ORDER BY uid')]
        filled = 0
        for done, uid in enumerate(uids, 1):
            paths = [r['path'] for r in c.execute(
                'SELECT path FROM files WHERE uid=? UNION '
                'SELECT path FROM file_locations WHERE fileUid=?', (uid, uid))]
            for path in paths:
                fields = exifDateTimeFields(exifDateTaken(path))
                if fields['exifDate'] is not None:
                    c.execute('UPDATE files SET exifDate=?, exifTime=? '
                              'WHERE uid=?',
                              (fields['exifDate'], fields['exifTime'], uid))
                    filled += 1
                    break
            # Short write transactions, so the app is never locked out long.
            if done % 200 == 0:
                c.commit()
            if progressFn is not None:
                progressFn(done, len(uids))
        c.commit()
        return len(uids), filled


    ###########################################################
    def getDetections(self, path):
        """Every stored detection for one file, in time order.

        @param  path  Absolute path.
        @return list  List of sqlite3.Row.
        """
        row = self.getFile(path)
        if row is None:
            return []
        cur = self._conn.execute(
            "SELECT * FROM detections WHERE fileUid = ? "
            "ORDER BY atMs, uid", (row["uid"],))
        return cur.fetchall()


    ###########################################################
    def getFaceNames(self):
        """Distinct recognized names available in saved Image analysis results."""
        return [r['faceName'] for r in self._conn.execute(
            "SELECT DISTINCT faceName FROM detections "
            "WHERE faceName IS NOT NULL AND trim(faceName) != '' "
            "ORDER BY faceName COLLATE NOCASE")]


    ###########################################################
    def getSearchFields(self):
        """Describe every column in the three user-media tables, including IDs."""
        fields = []
        for table in ('files', 'detections', 'file_locations'):
            for row in self._conn.execute('PRAGMA table_info(%s)' % table):
                fields.append(dict(field=table + '.' + row['name'], type=row['type']))
        return fields

    def compileSearch(self, query):
        from backEnd.UserMediaSearch import compileQuery
        fields = self.getSearchFields()
        names = lambda table: [r['field'].split('.')[1] for r in fields
                               if r['field'].startswith(table + '.')]
        return compileQuery(query, names('files'), names('detections'), names('file_locations'),
                            {r['field']: r['type'] for r in fields})

    def searchFieldValues(self, path):
        """Field values for match explanations; does not modify saved records."""
        row = self.getFile(path)
        if row is None:
            return []
        values = []
        for table in ('files', 'detections', 'file_locations'):
            key = 'uid' if table == 'files' else 'fileUid'
            for record in self._conn.execute('SELECT * FROM %s WHERE %s=?' % (table, key), (row['uid'],)):
                values.extend((table + '.' + name, record[name]) for name in record.keys())
                if table == 'file_locations':
                    values.append(('filename', os.path.basename(record['path'])))
        return values

    def pathsMatching(self, folder, types=None, wantNudity=False,
                      wantFace=False, faceName=None, recursive=False,
                      query='', allFolders=False):
        """Which files in a folder have the detections asked for.

        @param  folder      Absolute folder path to search.
        @param  types       Object types to require (any of), e.g.
                            ["person", "vehicle"].  None means don't filter.
        @param  wantNudity  Require a nudity detection.
        @param  wantFace    Require a named or detected face.
        @param  faceName    Additionally require this recognized person anywhere
                            in the file, including another sampled video frame.
        @param  recursive   Include files in all subfolders when True.
        @return set         Absolute paths that match.
        """
        clauses = []
        args = []
        for t in (types or []):
            clauses.append("d.type = ?")
            args.append(t)
        if wantNudity:
            clauses.append("d.nudity = 1")
        if wantFace:
            clauses.append("d.faceDetConf IS NOT NULL")
        conditions = [("EXISTS (SELECT 1 FROM detections d WHERE d.fileUid=f.uid AND (%s))"
                       % " OR ".join(clauses))] if clauses else []
        if faceName:
            conditions.append("EXISTS (SELECT 1 FROM detections named "
                              "WHERE named.fileUid = f.uid AND named.faceName = ?)")
            args.append(faceName)
        if query.strip():
            searchSql, searchArgs = self.compileSearch(query)
            conditions.append(searchSql)
            args.extend(searchArgs)
        if not conditions:
            if not allFolders:
                return None
            conditions.append('1=1')

        sql = ("SELECT DISTINCT l.path FROM files f JOIN file_locations l ON l.fileUid=f.uid "
               "WHERE %s" % " AND ".join(conditions))
        if allFolders:
            return {row['path'] for row in self._conn.execute(sql, args)}
        if not folder:
            return set()
        root = os.path.normcase(os.path.abspath(folder))
        matches = set()
        for row in self._conn.execute(sql, args):
            path = row['path']
            parent = os.path.normcase(os.path.dirname(os.path.abspath(path)))
            try:
                inScope = (os.path.commonpath((root, parent)) == root
                           if recursive else parent == root)
            except ValueError:
                # Different drives cannot be inside the selected directory.
                inScope = False
            if inScope:
                matches.add(path)
        return matches


    ###########################################################
    def stats(self):
        """@return (fileCount, detectionCount) -- for diagnostics and tests."""
        f = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        d = self._conn.execute(
            "SELECT COUNT(*) AS n FROM detections").fetchone()
        return (f["n"], d["n"])
