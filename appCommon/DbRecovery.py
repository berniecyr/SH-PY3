#*****************************************************************************
#
# DbRecovery.py
#     Database recovery module. Knows about the current databases, their tables
#     and the number of columns each one has. Hence it needs to be kept up to date in
#     case anything new gets introduced!
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


import sys, os, time, pickle, json
import sqlite3 as sqlite

from appCommon.CommonStrings import kObjDbFile
from appCommon.CommonStrings import kClipDbFile
from appCommon.CommonStrings import kResponseDbFile
from appCommon.CommonStrings import kSQLiteDatabases
from appCommon.CommonStrings import kCorruptDbFileName
from backEnd.ClipManager import ClipManager
from backEnd.DataManager import DataManager
from backEnd.ResponseDbManager import ResponseDbManager


""" Database recovery module. Knows about the current databases, their tables
and the number of columns each one has. Hence it needs to be kept up to date in
case anything new gets introduced!
"""

###############################################################################

_kPageSize = 512        # (smallest) SQLite page size
_kRowsBufferSize = 32   # number of rows to read before writing them all out
_kMaxCheckErrors = 1    # number of errors for the integrity check to report
_kRecoveryExtension = ".recovered"  # file extension for recovery files

###############################################################################

class TotalProgress(object):
    """ Progress reporting class. Abstract, needs implementation of _progress().
    """

    ###########################################################
    def __init__(self, path, dbFiles, interval):
        """ Constructor. The list of database files must match the ones passed
        to DbRecovery.recover() in count and the same order.

        @param path      The path where the database files are located.
        @param dbFiles   Database files, just the names.
        @param interval  How often to call the custom _progress method.
        """
        self._interval = interval
        self._path = path
        self._total = 0
        self._dbFileSizes = []
        # get the size for each database file
        for dbFile in dbFiles:
            try:
                dbFileSize = os.path.getsize(os.path.join(path, dbFile))
            except:
                dbFileSize = 0
            self._total += dbFileSize
            self._dbFileSizes.append(dbFileSize)
        self._copied = 0
        self._lastTime = 0
        self._lastDbFile = None


    ###########################################################
    def progress(self, dbFile, table, rows):
        """ General progress method known by the DbRecovery class.

        @param dbFile  The database file currently worked on.
        @param table   Current table being recovered.
        @param rows    Number of rows processed so far in the current table
        @return        True to continue, False to abort the whole recovery.
        """
        # time to report progress?
        now = time.time()
        if now - self._lastTime < self._interval:
            return True
        self._lastTime = now
        # get the size of the current recovery database file
        recoveryPath = os.path.join(self._path, dbFile + _kRecoveryExtension)
        try:
            recoverySize = os.path.getsize(recoveryPath)
        except:
            recoverySize = 0
        # if the database has changed move the base to the next slot, so we
        # stay as accurate as possible (jumping back in progress should not
        # happen because the assumption is that file sizes never get larger)
        if self._lastDbFile != dbFile:
            self._copied += 1
            self._lastDbFile = dbFile
        base = 0
        for i in range(0, self._copied - 1):
            base += self._dbFileSizes[i]
        percentage = ((base + recoverySize) * 100.0) / max(1, self._total)
        if percentage >= 100:
            percentage = 99.9
        return self._progress(dbFile, table, percentage)


    ###########################################################
    def _progress(self, dbFile, table, percentage):
        """ To be implemented in an inherited class.

        @param dbFile      The database file currently worked on.
        @param table       Current table being recovered.
        @param percentage  Total progress percentage. This is a guess, based on
                           the file sizes between original and currently written
                           recovery ones, with the tendency to jump from < 100%
                           to 100% rapidly due to some slack getting removed in
                           the recovered files.
        @return            True to continue, False to abort the whole recovery.
        """
        return True


###############################################################################
def _createDatabase(cls, path, logger):
    """ Creates a database leveraging the fact that all managers we have
    developed so far have the same interface for creation, opening (where the
    creation actually happens) and closing.

    @param cls     The manager class.
    @param path    Full path of the database file.
    @param logger  Logger for the manager to use.
    """
    if os.path.exists(path):
        os.remove(path)
    mng = cls(logger)
    mng.open(str(path))
    mng.close()

# descriptors to tell what database files are out there, what tables with how
# many rows they contain and what manager classes (luckily they all have the
# same interface) to use to create an empty database (with all of the right
# indices etc)
_kDescriptors = {
    kClipDbFile: (ClipManager,
        [('clips', 11)]),
    kObjDbFile: (DataManager,
        [("objects", 16), ('actions', 7), ('motion', 7)]),
    kResponseDbFile: (ResponseDbManager,
        [('clipsToSend', 12), ('lastSentInfo', 6), ('pushNotifications', 4)])
}

###############################################################################s

class DbRecovery(object):
    """ To check and recover SV databases. Recovery means going through each of
    a database's tables, reading out row after row until fully consumed or a
    failure (usually due to corruption) happens. The data is written into a new
    database file which will then replace the original one. The new files are
    usually smaller due to slack in the original ones.
    """

    ###########################################################
    """ Constructor.

    @param path    Path were the database files are located.
    @param logger  The logger to use.
    """
    def __init__(self, path, logger):
        self._logger = logger
        self._path = path

    ###########################################################
    """ To check all of the databases for their integrity.

    @param quick  True to do a quick check, False for a full check.
    @return       List of the database files (without path) found to be damaged.
    """
    def check(self, quick):
        result = []
        if quick:
            pragma = "PRAGMA quick_check(%d)" % _kMaxCheckErrors
        else:
            pragma = "PRAGMA integrity_check(%d)" % _kMaxCheckErrors
        for dbFile in _kDescriptors.keys():
            try:
                dbPath = os.path.join(self._path, dbFile)
                dbSize = os.path.getsize(dbPath)
                if _kPageSize > dbSize:
                    self._logger.error('DB size too small (%d)' % dbSize)
                    result.append(dbFile)
                    continue
                con = sqlite.connect(dbPath)
                cur = con.cursor()
                rows = cur.execute(pragma).fetchall()
                if 1 == len(rows) and ('ok', ) == rows[0]:
                    continue;
                self._logger.error('integrity check failed')
                for row in rows:
                    self._logger.error(str(row))
            except:
                self._logger.error("check error: %s" % sys.exc_info()[1])
            finally:
                try:
                    con.close()
                except:
                    pass
            result.append(dbFile)
        return result


    ###########################################################
    """ Recover a database file.

    @param dbFile     The name of the database file to recover.
    @param progress   Progress callback (dbFile, table name, rows so far).
    @param resetOnly  True if no rows should be copied.
    @return           True if recovery succeeded, False on error.
    """
    def recover(self, dbFile, progress, resetOnly=False):
        descriptor = _kDescriptors.get(dbFile, None)
        if descriptor is None:
            return False
        dbPath = os.path.join(self._path, dbFile)
        if not os.path.exists(dbPath):
            self._logger.info("database %s does not exist" % dbPath)
            return True # no database is a good database
        dbPathRecovery = os.path.join(self._path, dbFile + _kRecoveryExtension)
        wasError = False
        try:
            self._logger.info("creating recovery database %s ..." %
                              dbPathRecovery)
            _createDatabase(descriptor[0], dbPathRecovery, self._logger)
            if not resetOnly:
                self._logger.info(
                    "opening original database %s (%d bytes) ..." %
                    (dbPath, os.path.getsize(dbPath)))
                conIn = sqlite.connect(dbPath)
                curIn = conIn.cursor()
                self._logger.info("opening recovery database ...")
                conOut = sqlite.connect(dbPathRecovery)
                curOut = conOut.cursor()
                # NOTE: some throughput optimizing, but since things are mostly
                #       CPU-bound they don't seem to help very much ...
                curOut.execute("PRAGMA synchronous = OFF")
                curOut.execute("PRAGMA journal_mode = OFF")
                for tableInfo in descriptor[1]:
                    table = tableInfo[0]
                    self._logger.info("recovering table '%s' ..." % table)
                    try:
                        curIn.execute("SELECT * FROM %s" % table)
                    except:
                        self._logger.error("table %s select failed (%s)" %
                                           (table, sys.exc_info()[1]))
                        continue
                    # Shape the INSERT from the cursor, not from the
                    # descriptor.  The descriptor's column counts are
                    # hardcoded and drift: 'objects' grew from 16 to 20
                    # columns and _kDescriptors was never updated, so from
                    # then on EVERY objdb2 recovery died at statement
                    # preparation -- "table objects has 20 columns but 16
                    # values were supplied" -- 3ms in, before reading a row
                    # (2026-09-02).  cursor.description describes exactly the
                    # rows we are about to insert, so it cannot drift.
                    columns = [d[0] for d in curIn.description]
                    if len(columns) != tableInfo[1]:
                        self._logger.warning(
                            "table %s: descriptor claims %d columns, database "
                            "has %d -- using the database" %
                            (table, tableInfo[1], len(columns)))
                    # Insert by name rather than position, so a source table
                    # narrower than the freshly created recovery table still
                    # copies (the missing columns take their defaults) instead
                    # of failing the whole table.
                    collist = ",".join(['"%s"' % c for c in columns])
                    paramspec = ",".join(["?"] * len(columns))
                    execmany = "INSERT INTO %s (%s) VALUES(%s)" % (
                        table, collist, paramspec)
                    rowsCopied = 0
                    rowsTotal = 0
                    while True:
                        # collect a few rows first before writing them all at
                        # once out to the recovery database
                        rows = []
                        for i in range(0, _kRowsBufferSize):
                            try:
                                # read things row by row, maximizing our chances to
                                # recover as many as possible
                                row = curIn.fetchone()
                            except:
                                self._logger.error("table %s error at row %d" %
                                                   (table, i))
                                break
                            if row is None:
                                break
                            rows.append(row)
                            rowsTotal += 1
                        if rows:
                            curOut.executemany(execmany, rows)
                            if progress is not None and not \
                               progress(dbFile, table, rowsCopied):
                                raise Exception("progress interrupt")
                            rowsCopied += len(rows)
                        else:
                            break
                    conOut.commit()
                    self._logger.info("%d/%d rows copied" %
                                      (rowsCopied, rowsTotal))
        except:
            self._logger.error("repair failed: %s" % sys.exc_info()[1])
            wasError = True
        finally:
            if not resetOnly:
                try:
                    conIn.close()
                except:
                    pass
            try:
                if not resetOnly:
                    conOut.close()
                self._logger.info("removing corrupted database...")
                os.remove(dbPath)
                self._logger.info("renaming recovered database...")
                os.rename(dbPathRecovery, dbPath)
            except:
                self._logger.error("recovery finalization failed %s" %
                                   sys.exc_info()[1])
                wasError = True
        if wasError:
            try:
                os.remove(dbPathRecovery)
            except:
                pass
            return False
        return True


##############################################################################

# current application of the database recovery in the front-end and (beginning
# with the introduction of the service) later on the back-end ...

# Number of seconds to try to get rid of the database corruption status file.
_kDbCorruptionStatusRemovalTimeout = 1

# Values used in the corruption status file (first element of pickled array).
kStatusRecover = "recover"                 # request to recover databases
kStatusReset = "reset"                     # request to delete databases
kStatusDetected = ""                       # corruption detected state
kStatusRecoveryError = "recovery_error"    # recovery failed state
kStatusProgress = "progress"               # progress data during recovery

# Name of the "a database looks damaged RIGHT NOW" flag, in the data dir.
# Deliberately separate from the corruption STATUS file above: that one drives
# the recover/reset dialog at startup, whereas this one is a live signal other
# processes read to decide whether it is safe to delete anything.  Plain JSON
# so it can be read by eye -- when footage is at stake the operator should be
# able to see why deletions stopped without running a tool.
kCorruptionFlagFile = "db-corruption-detected"

# The 'source' recorded on the flag when clip registration stalls, as opposed
# to a source that means real, verified damage.  A stall raises the flag to
# stop the orphan sweep deleting unregistered footage, but the databases have
# passed their integrity check -- so anything reading the flag to TALK to the
# user has to tell the two apart.  Saying "a database is damaged" and pointing
# at repair_clipdb.py on the strength of a stall is simply wrong, and it cost
# a user an evening on 2026-08-23.
kStallFlagSource = "clip registration stalled"


###########################################################
def writeCorruptionFlag(dataDir, info, logger=None):
    """Raise the live corruption flag.  Safe to call repeatedly.

    Written whenever a database is seen to be damaged, or when clips stop
    reaching the database while cameras record.  Its ONLY job is to make other
    processes refuse to destroy data -- see DiskCleaner._removeOrphanFiles.
    """
    path = os.path.join(dataDir, kCorruptionFlagFile)
    try:
        payload = dict(info or {})
        payload.setdefault('firstSeen', time.time())
        payload['updated'] = time.time()
        existing = readCorruptionFlag(dataDir)
        if existing and existing.get('firstSeen'):
            payload['firstSeen'] = existing['firstSeen']
        with open(path, 'w') as f:
            json.dump(payload, f, indent=2, default=str)
        return True
    except Exception as e:
        if logger is not None:
            logger.error("could not write %s: %r" % (path, e))
        return False


###########################################################
def readCorruptionFlag(dataDir):
    """@return the flag's dict, or None when no database is flagged.

    A present-but-unreadable flag still counts as raised: the whole point is to
    fail safe, so a truncated file must not read as "all clear".
    """
    path = os.path.join(dataDir, kCorruptionFlagFile)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {'error': 'unreadable flag'}
    except Exception:
        return {'error': 'unreadable flag'}


###########################################################
def clearCorruptionFlag(dataDir, logger=None):
    """Lower the flag.  Only justified by EVIDENCE that the files are sound --
    a clean integrity check -- never by the symptom merely going away."""
    path = os.path.join(dataDir, kCorruptionFlagFile)
    try:
        if os.path.isfile(path):
            os.remove(path)
            if logger is not None:
                logger.info("database corruption flag cleared: %s" % path)
        return True
    except Exception as e:
        if logger is not None:
            logger.error("could not remove %s: %r" % (path, e))
        return False


###########################################################
class DbRecoveryProgress(TotalProgress):
    """ Tracker and reporter for database recovery. The current progress is
    written to the database recovery status file.
    """

    ###########################################################
    def __init__(self, dataDir, storageDir, logger):
        """ @see TotalProgress.__init__()
        """
        super(DbRecoveryProgress, self).__init__(
            storageDir, kSQLiteDatabases, 1)
        self._logger = logger
        self._dataDir = dataDir


    ###########################################################
    def _progress(self, dbFile, table, percentage):
        """ @see TotalProgress.progress()
        """
        self._logger.info("recovery progress %.1f%% (%s@%s)" %
                          (percentage, table, dbFile))
        status = [kStatusProgress, percentage, dbFile, table]
        setCorruptDatabaseStatus(status, self._dataDir, self._logger)
        return True


###########################################################
def setCorruptDatabaseStatus(status, dataDir, logger=None):
    """ Sets the database corruption status.

    @param status  The status message (array) or None if things are fine.
    @param dataDir The data directory holding the status file.
    @param logger  Optional logger.  Any failure is logged with its reason --
                   this write silently returning False (a text-mode pickle
                   TypeError under Py3) is exactly what masked the broken
                   corrupt-DB recovery for the life of the port, so callers
                   should pass a logger.
    @return        True on success, False if the update failed.
    """
    filePath = os.path.join(dataDir, kCorruptDbFileName)
    if status is None:
        # keep trying to get rid of that file in case the frontend has it
        # open for a short period of time for readout (Windows only)
        tmout = time.time() + _kDbCorruptionStatusRemovalTimeout
        while os.path.exists(filePath):
            try:
                os.remove(filePath)
                break
            except Exception as e:
                if time.time() >= tmout:
                    if logger is not None:
                        logger.error("Could not remove corruption status "
                                     "file %s: %r" % (filePath, e))
                    return False
                time.sleep(.1)
        return True
    f = None
    try:
        # Binary mode: pickle.dump emits BYTES.  Under Python 2 a text-mode
        # ("w") file accepted them, but under Python 3 it raises TypeError,
        # which the bare except below turned into a silent False -- leaving a
        # 0-byte marker that broke the whole corrupt-DB recovery flow (the
        # recover/reset request could never be persisted for the back end).
        f = open(filePath, "wb")
        pickle.dump(status, f)
        return True
    except Exception as e:
        if logger is not None:
            logger.error("Could not write corruption status file %s: %r" %
                         (filePath, e))
        return False
    finally:
        try:
            f.close()
        except:
            pass



###########################################################
def getCorruptDatabaseStatus(dataDir, logger=None):
    """ Checks if there's a database corruption file and if so reads its
    content. The recovery process in the back-end will update it and
    finally remove it upon completion.

    @param dataDir  The data directory to use.
    @param logger   Optional logger; a failed read is logged with its reason.
    @return         Content of the file (array), None if it does not exist.
    """
    corruptDbFile = os.path.join(dataDir, kCorruptDbFileName)
    if os.path.isfile(corruptDbFile):
        status = []
        f = None
        try:
            # Binary mode to match the pickle written by setCorruptDatabaseStatus
            # ("rb", not "r" -- pickle.load needs bytes under Python 3).
            f = open(corruptDbFile, "rb")
            status = pickle.load(f)
        except Exception as e:
            if logger is not None:
                logger.warning("Could not read corruption status file "
                               "%s: %r" % (corruptDbFile, e))
        finally:
            try:
                f.close()
            except:
                pass
        return status
    return None


###########################################################
def runDatabaseRecovery(dataDir, databasesDir, logger, resetOnly):
    """ Restores corrupted databases as best as possible. Recovery is
    triggered by the presence of a corruption file which gets emitted in
    case SQLite errors out with a distinct error message.

    @param dataDir       The data directory to use.
    @param databasesDir  Where the databases are located.
    @param logger        Logging instance to report the recovery to.
    @param resetOnly     Reset the databases, do not copy any old data over.
    @return              True if the databases got recovered successfully or
                         were not declared to be damaged at all. False if
                         recovery failed.
    """

    progress = DbRecoveryProgress(dataDir, databasesDir, logger)
    recovery = DbRecovery(databasesDir, logger)

    for dbFile in kSQLiteDatabases:
        logger.info("recovering '%s' (resetOnly=%s) ..." % (dbFile, resetOnly))
        if not recovery.recover(dbFile, progress.progress, resetOnly):
            logger.info("recovery failed, error status set")
            setCorruptDatabaseStatus([kStatusRecoveryError], dataDir, logger)
            return False

    return setCorruptDatabaseStatus(None, dataDir, logger)
