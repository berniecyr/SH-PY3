#!/usr/bin/env python

#*****************************************************************************
#
# DataManager.py
#     API for accessing and interacting with object and motion database (objDb2)
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


# Python imports...
import itertools
import operator
import os.path
import os
import sqlite3 as sql
import threading
import time
import traceback
import glob
import shutil
from bisect import bisect_left
from datetime import datetime, timezone

def _msToFolder(ms):
    """The day folder holding the thumbnail for `ms` -- LOCAL date.

    Must match QueuedDataManagerCloud._msToFolder, which writes them; see the
    note there.  Local matches the clip day folder, which has always been
    local -- thumbnails were the only part of the archive filed by UTC.
    Changed 2026-09-06 together with the writer and with
    DiskCleaner._enumDateFolders.

    Thumbnails written before that change are under UTC names, so anything
    recorded between 20:00 and 23:59 local is in the following day's folder
    and will not be found until it ages out.  The adjacent-folder probe in
    getThumbnailPath covers only a tolerance-sized gap, not a four-hour one.
    """
    return datetime.fromtimestamp(ms / 1000.0).strftime('%Y-%m-%d')

def _folderDiff(a, b):
    try:
        da = datetime.strptime(a, '%Y-%m-%d').date()
        db = datetime.strptime(b, '%Y-%m-%d').date()
        return abs((da - db).days)
    except ValueError:
        try:
            return abs(int(a) - int(b))
        except ValueError:
            return 0

# Common 3rd-party imports...
from PIL import ImageDraw, ImageColor, Image
import cv2            # already a transitive dep via ClipReader; used for fast
import numpy as np    # in-place overlay drawing during playback (_markFrame)

# Toolbox imports...
from vitaToolbox.sql.TimedConnection import TimedConnection
from vitaToolbox.sql.ShmHeal import healPoisonedWalIndex
from vitaToolbox.strUtils.EnsureUnicode import ensureUtf8
from vitaToolbox.sysUtils.TimeUtils import getTimeAsMs

# Local imports...
from .VideoMarkupModel import VideoMarkupModel
from videoLib2.python.ClipReader import ClipReader, frameMsAt

# Constants...
from appCommon.CommonStrings import kThumbsSubfolder
from appCommon.CommonStrings import kSqlAlertThreshold

# ...we always work in a coordinate system that is this big...
_kCoordWidth = 320
_kCoordHeight = 240

# Seed for minCx/minCy: larger than any centroid can be, so the first real frame
# always wins.  Paired with a maxCx/maxCy seed of -1, which doubles as the
# "this object has no frames" marker -- travel is only meaningful when maxCx >= 0.
_kNoCentroid = 1 << 20

# Never remove an entry from the objects table that has been updated in the
# past 10 minutes.  It could still have pending data that would be lost.
_kObjectSaveBuffer = 600000

# Flag to draw boxes and lines transparently. This was a proof-of-concept, but
# the lines drawn just appear weak and the transparency aspect really does not
# show very well. So put it on hold for now.
_kTransparentDraw = False

# When drawing playback bounding boxes, motion samples are sparse (a few Hz),
# so an exact frame-ms match usually finds nothing.  Match the nearest recorded
# box within this window (ms) so boxes track the object and vanish when it's
# gone.
_kBoxMatchToleranceMs = 750

# How much bigger than the detection its zoomed preview crop is.  Enough that
# the subject is not jammed against the edges; small enough that a person still
# fills most of a 64px preview.
_kZoomContextFactor = 1.6

# Two floats per file, so this could be far larger; it is a cap against a
# long-running back end walking the whole archive, not a memory concern.
_kMaxFrameTimingEntries = 4096

# drawBoxes - True to draw tracking boxes on the saved clip
# maxSize - Requested maximum size of created clip (may be smaller)
# max_bit_rate - Requested maximum bit rate
_kSaveClipDefaults = {
        "drawBoxes":False,
        "enableTimestamps":False,
        "maxSize":(0,0),
        "max_bit_rate":0,
}


###############################################################
class DataManager(object):
    """A class controlling the object database"""
    ###########################################################
    def __init__(self, logger, clipManager=None, videoStoragePath=None):
        """Initializer for the DataManager class

        @param  logger            An instance of a VitaLogger to use.
        @param  clipManager       A clip manager, required to retrieve frames
        @param  videoStoragePath  Path to the directory videos are stored,
                                  required to retrieve frames
        """
        self._logger = logger
        self._connection = None
        self._clipManager = clipManager

        self._filterStr = ''
        self._targetFilter = ''
        self._cameraFilter = ''
        self._attrFilter = ''

        self._sizeFilter = ''
        self._travelFilter = ''
        self._travelFilterPx = 0

        self._curDbPath = None

        # Keyed by UID
        self._targetRangeFilterDict = {}

        self._vidStoragePath = videoStoragePath
        self._objList = []
        self._curVidPath = None
        self._curVidSize = None
        self._clipReader = None
        self._curMsIndex = -1
        self._curFrameMs = 0
        self._fileStart = 0
        self._fileStop = 0
        self._bboxCache = {}
        self._cacheKeys = {}
        self._thumbCache = {}
        self._videoDebugLines = []
        self._firstFile = None
        self._lastFile = None
        self._pendingNextFrame = None
        self._audioEnabled = False
        self._asyncReadEnabled = True
        self._curFileAudioEnabled = False

        self._muted = False

        # Init markupModel to defaults, though we expect it to
        # be changed later with setMarkupModel...
        self._markupModel = VideoMarkupModel()

        self._liveMs = 0

        self._procSizeCache = {}

        # (fps, frameCount) per recorded file, keyed on (path, size, mtime);
        # see _getFrameTiming.
        self._frameTimingCache = {}


    ###########################################################
    def _createTables(self):
        """Create the necessary tables in the database

        Table objects:
            uid        - int, primary key
            fileName   - text, filename of the associated video file (DEPRECATED - TODO: remove)
            camLoc     - text, name of the camera location the obj was captured
            timeStart  - int, first time the object was seen
            timeStop   - int, final time the object was seen
            rX1, rY1   - int, (DEPRECATED - TODO: remove)
            rX2, rY2   - int, (DEPRECATED - TODO: remove)
            type       - text, a lable for the object's classification
            confidence - real, (DEPRECATED - TODO: remove)
            thumbnail  - blob, (DEPRECATED - TODO: remove)
            minWidth   - int, the minimum height of the object
            maxWidth   - int, the maximum height of the object
            minHeight  - int, the minimum height of the object
            maxHeight  - int, the maximum height of the object
            minCx, maxCx - int, the extremes of the object's bbox CENTRE x
            minCy, maxCy - int, the extremes of the object's bbox CENTRE y
                         Together these give "travel": how far the object moved
                         over its life, as (maxCx-minCx) + (maxCy-minCy).  See
                         getTravelExpr() for why this stat is worth keeping and
                         how it is filtered on.  Unlike the four size columns
                         these are seeded so that an object with NO frames is
                         detectable (maxCx = -1), rather than silently keeping
                         a plausible-looking wrong value.

        Table motion:
            objUid - int, the object table uid of the motion object
            frame  - int, the frame number of this bbox
            time   - int, the time corresponding to this frame
            x1, y1 - int, upper left coordinates of the object's bbox
            x2, y2 - int, bottom right coordinates of the object's bbox

        Table actions:
            objUid     - int, the object table uid of the action object
            type       - text, the category, like "person"; this is a duplicate
                         of info in the objects table, but makes searching easy
            action     - text, the action string, like "walking"
            frameStart - int, the start frame number of the action
            timeStart  - int, the milliseconds associated with frameStart
            frameStop  - int, the stop frame number of the action; inclusive
                         (in other words, the action _includes_ this frame)
            timeStop   - int, the milliseconds associated with frameStop

        """
        # Use a page size of 4096.  The thought (from google gears API docs),
        # is that: "Desktop operating systems mostly have default virtual
        # memory and disk block sizes of 4k and higher."
        self._cur.execute('''PRAGMA page_size = 4096''')

        try:
            self._cur.disableExecuteLogForNext()
            self._cur.execute(
                '''CREATE TABLE objects (uid INTEGER PRIMARY KEY, '''
                '''fileName TEXT, camLoc TEXT, timeStart INTEGER, '''
                '''timeStop INTEGER, rX1 INTEGER, rY1 INTEGER, rX2 INTEGER, '''
                '''rY2 INTEGER, type TEXT, confidence REAL, thumbnail BLOB, '''
                '''minWidth INTEGER, maxWidth INTEGER, '''
                '''minHeight INTEGER, maxHeight INTEGER, '''
                '''minCx INTEGER, maxCx INTEGER, '''
                '''minCy INTEGER, maxCy INTEGER)''')

            self._cur.disableExecuteLogForNext()
            self._cur.execute(
                '''CREATE TABLE motion (objUid INTEGER, frame INTEGER, '''
                '''time INTEGER, x1 INTEGER, y1 INTEGER, x2 INTEGER, '''
                '''y2 INTEGER, '''
                '''PRIMARY KEY (objUid, time))''')

            self._cur.disableExecuteLogForNext()
            self._cur.execute(
                '''CREATE TABLE actions '''
                '''(objUid INTEGER, type TEXT, action TEXT, '''
                '''frameStart INTEGER, timeStart INTEGER, '''
                '''frameStop INTEGER, timeStop INTEGER)'''
            )
        except sql.OperationalError:
            # Happens if two processes try at same time...
            pass


    ###########################################################
    def _upgradeOldTablesIfNeeded(self):
        """Upgrade from older versions of tables."""

        # From 4901 and earlier
        # ---------------------

        # Get the SQL that was used to create the object table...
        ((objectSql,),) = self._cur.execute(
            '''SELECT sql FROM sqlite_master'''
            ''' WHERE type="table" AND name="objects"''')

        if 'minWidth' not in objectSql:
            # Expect that the first statement might fail; that can happen if
            # another process is running at nearly the same time and also
            # decided to upgrade the tables.  If the first statement succeeds,
            # we expect the rest to succeed.
            try:
                self._cur.disableExecuteLogForNext()
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN minWidth INTEGER'''
                )

                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN maxWidth INTEGER'''
                )
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN minHeight INTEGER'''
                )
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN maxHeight INTEGER'''
                )
                self._cur.execute(
                    '''UPDATE objects SET minWidth='''
                    '''(SELECT MIN(x2-x1) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET maxWidth='''
                    '''(SELECT MAX(x2-x1) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET minHeight='''
                    '''(SELECT MIN(y2-y1) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET maxHeight='''
                    '''(SELECT MAX(y2-y1) FROM motion WHERE uid=objUid)'''
                )
            except sql.OperationalError:
                # Happens if two processes try at same time...
                pass

        # Centroid extremes, for the "travel" stat -- how far an object moved
        # over its life.  Same shape as the block above: add the columns, then
        # back-fill them from the motion rows that already exist, so the stat is
        # available for everything ever recorded and not just from here on.
        #
        # Measured 2026-08-26 on a 16,215-object database: the whole back-fill
        # is one pass taking ~3 s.  It is gated by the sniff, so it runs once.
        if 'maxCx' not in objectSql:
            try:
                self._cur.disableExecuteLogForNext()
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN minCx INTEGER DEFAULT 0'''
                )
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN maxCx INTEGER DEFAULT -1'''
                )
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN minCy INTEGER DEFAULT 0'''
                )
                self._cur.execute(
                    '''ALTER TABLE objects ADD COLUMN maxCy INTEGER DEFAULT -1'''
                )
                # As above, 'uid' here resolves to the OUTER objects.uid and
                # objUid to motion.objUid -- motion has no uid column.  Reads
                # oddly, but it is a correct correlated backfill.
                self._cur.execute(
                    '''UPDATE objects SET minCx='''
                    '''(SELECT MIN((x1+x2)/2) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET maxCx='''
                    '''(SELECT MAX((x1+x2)/2) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET minCy='''
                    '''(SELECT MIN((y1+y2)/2) FROM motion WHERE uid=objUid)'''
                )
                self._cur.execute(
                    '''UPDATE objects SET maxCy='''
                    '''(SELECT MAX((y1+y2)/2) FROM motion WHERE uid=objUid)'''
                )
                # An object with no motion rows gets NULL from the subqueries,
                # which would compare as unknown in the filter rather than as
                # "no data".  Pin it to the no-data sentinel instead.
                self._cur.execute(
                    '''UPDATE objects SET minCx=0, maxCx=-1, minCy=0, maxCy=-1 '''
                    '''WHERE maxCx IS NULL'''
                )
            except sql.OperationalError:
                # Happens if two processes try at same time...
                pass


    ###########################################################
    def _addIndices(self):
        """Add some indices to the database.

        This will auto-add any indices that are needed...
        """
        # As far as I can tell this index was actually slowing things down.
        # Indexes should typically be avoided for columns with low cardinality,
        # and we never have very many locations.
        self._cur.execute('''DROP INDEX IF EXISTS IDX_OBJECTS_CAMLOC''')

        # This index made getBboxAtFrame() faster, but at the expense of
        # inserts.  We insert more often than we call getBboxAtFrame(), so
        # we'll drop it...
        self._cur.execute('''DROP INDEX IF EXISTS IDX_MOTION_OBJUID_FRAME''')

        # Our most common database operation is searches between two times so
        # indexing times really helps us.  Specifically real time searches are
        # performed most often where timeStop >= ~1 second ago and
        # timeStart <= now.  Because the timeStop search is going to be much
        # smaller/quicker (nearly no values vs likely tens of thousands or
        # more in the timeStart) we want timeStop to be the index.
        # Runtimes: IDX (stop) < no index < IDX (start).
        self._cur.execute('''CREATE INDEX IF NOT EXISTS '''
                          '''IDX_OBJECTS_STOP on objects (timeStop)''')


    ###########################################################
    def open(self, filePath, timeout=45, readOnly=False):
        """Open the database, self-healing a poisoned WAL-index once.

        Same rationale as ClipManager.open (incident 2026-07-26): a poisoned
        -shm makes every open fail although the database is healthy; it is a
        rebuildable index, so delete it and retry once on that signature.
        @see _openImpl for parameters.
        """
        try:
            return self._openImpl(filePath, timeout, readOnly)
        except sql.DatabaseError as e:
            try:
                self.close()
            except Exception:
                pass
            if not healPoisonedWalIndex(filePath, self._logger, e):
                raise
            return self._openImpl(filePath, timeout, readOnly)


    ###########################################################
    def _openImpl(self, filePath, timeout=45, readOnly=False):
        """Open the database, creating tables if necessary

        @param  filePath  Path of the database file to open
        @param  timeout   The time in seconds connections will wait for locks
                          to free without throwing an exception.
        @param  readOnly  If True, open a read-only connection and skip all
                          schema setup.  Used by searches: the back end has
                          already created/migrated the DB, so a search only
                          needs to read.  A read-only ('mode=ro') connection
                          takes only shared locks and never writes a journal,
                          so it cannot contend with the back end's concurrent
                          writes -- which was crashing sqlite during searches.
        """
        assert type(filePath) == str

        self._curDbPath = filePath

        if self._connection:
            self.close()

        if readOnly:
            from pathlib import Path as _Path
            uri = _Path(filePath).as_uri() + '?mode=ro'
            self._connection = sql.connect(uri, timeout,
                    factory=TimedConnection, check_same_thread=False, uri=True)
            self._connection.setParameters(self._logger,
                    float(kSqlAlertThreshold), os.path.exists(self._curDbPath+".debug"))
            self._cur = self._connection.cursor()
            return

        # Open the database file and retrieve a list of the tables
        self._connection = sql.connect(filePath.encode('utf-8'), timeout,
                factory=TimedConnection, check_same_thread=False)
        self._connection.setParameters(self._logger, float(kSqlAlertThreshold), os.path.exists(self._curDbPath+".debug"))
        self._cur = self._connection.cursor()

        # WAL journal mode lets search reads and detection/object writes run
        # concurrently without "database is locked" contention (which had been
        # freezing the UI and dropping writes under load).
        _jm = self._cur.execute("PRAGMA journal_mode=WAL").fetchone()
        if not _jm or str(_jm[0]).lower() != 'wal':
            self._logger.warning("objdb: journal_mode is %r, expected 'wal'"
                                 % (_jm[0] if _jm else None))

        tables = list(map(operator.itemgetter(0),
                         self._cur.execute('''SELECT name FROM sqlite_master '''
                                           '''WHERE type="table"''')))

        if 'objects' not in tables:
            # Set up tables if they don't exist
            self._createTables()

            # Brand-new database: persist the global baseline AI-detection
            # thresholds so a fresh install starts configured and the Options
            # UI and the detector agree from the very first run.  No-op if a
            # config already exists; never raises.
            try:
                from backEnd.ImageCheckConfig import ensureDefaults
                ensureDefaults()
            except Exception:
                pass

            # An EXISTING config may still be sitting on a default that has
            # since changed (e.g. the YOLO model).  ensureDefaults() cannot do
            # this -- it is a no-op once a config file exists.
            try:
                from backEnd.ImageCheckConfig import migrateConfig
                migrateConfig()
            except Exception:
                pass

            try:
                from backEnd.IHostConfig import ensureDefaults as ensureIHostDefaults
                ensureIHostDefaults()
            except Exception:
                pass

        self._upgradeOldTablesIfNeeded()

        # Always call addIndices to update old versions of databases...
        self._addIndices()

        # Ensure the detection-attributes table exists (face name / nudity).
        # Uses IF NOT EXISTS so it works for both fresh and pre-existing DBs.
        self._ensureAttributesTable()

        # Do a big commit now...
        self.save()


    ###########################################################
    def setVideoStoragePath(self, videoStoragePath):
        """Update the video storage path.

        @param  videoStoragePath  Path to the directory videos are stored,
                                  required to retrieve frames.
        """
        self._vidStoragePath = videoStoragePath


    ###########################################################
    def setMarkupModel(self, markupModel):
        """Sets the data model that tells us how to markup video.

        @param  markUpModel  A VideoMarkupModel object.  We'll consult this
                             whenever asked for marked video.  Note that we
                             don't listen for changes--it's the client's job
                             to re-ask us for video if the markup changed.
        """
        self._markupModel = markupModel


    ###########################################################
    def getPaths(self):
        """Retrieve paths important to the data manager.

        @return objDbPath         Path to the object database or None
        @return clipDbPath        Path to the clip manager, or None.
        @return videoStoragePath  Path to the directory videos are stored or
                                  None.
        """
        clipDbPath = None
        if self._clipManager:
            clipDbPath = self._clipManager.getPath()
        return (self._curDbPath, clipDbPath, self._vidStoragePath)


    ###########################################################
    def close(self):
        """Close the database"""
        if self._connection:
            self._connection.close()

        self._connection = None
        self._curDbPath = None


    ###########################################################
    def save(self):
        """Save all changes to the database"""
        assert self._connection is not None

        self._connection.commit()


    ###########################################################
    def reset(self):
        """Reset the database"""
        assert self._connection is not None

        self._connection.execute('''DROP TABLE objects''')
        self._connection.execute('''DROP TABLE motion''')
        # These two are keyed on objUid and describe rows we just dropped.
        # Uids restart at 1 after this, so leaving them means a reissued uid
        # inherits a stale face name / nudity flag.  removeCameraLocation
        # already wipes all four together; match it.  Dropping `actions` also
        # un-breaks _createTables, whose single try/except was swallowing the
        # CREATE that failed because the table still existed.
        self._connection.execute('''DROP TABLE IF EXISTS actions''')
        self._connection.execute('''DROP TABLE IF EXISTS objectAttributes''')

        self._createTables()
        self._addIndices()
        self._ensureAttributesTable()

        # In-memory state describing the database we just destroyed.  Nothing
        # else clears these: the orphan sweep in _ensureAttributesTable and the
        # cache rebuilds only happen at open(), and reset() does not reopen.
        self._procSizeCache = {}
        self._thumbCache = {}
        self._bboxCache = {}
        self._cacheKeys = {}
        self._frameTimingCache = {}
        self._objList = []

        self.save()


    ###########################################################
    #def deleteObject(self, id):
    #    """Remove an object from the database
    #
    #    @param  id  The id of the object to remove from the database
    #    """
    #    assert self._connection is not None
    #
    #    self._cur.execute('''DELETE FROM objects WHERE uid=?''', (id,))
    #    self._cur.execute('''DELETE FROM motion WHERE objUid=?''', (id,))
    #    # TODO: Don't leave the database hanging.  DO A SAVE (!!!)


    ###########################################################
    def addObject(self, timeStart, objType="object", cameraLocation=''):
        """Insert an object into the database

        @param  timeStart       The time the object first came into view
        @param  objType         The type of this object, like 'person' or
                                'object'.
        @param  cameraLocation  The camera location for the object.
        @return dbId            The id for this object assigned by the database
        """
        assert self._connection is not None

        # Always use 'object' for 'unknown' and 'nonperson'
        if objType.lower() in ('unknown', 'nonperson'):
            objType = 'object'

        # minCx/minCy seed HIGH and maxCx/maxCy seed to -1, so the first real
        # frame wins on both sides and an object that never gets a frame stays
        # detectable as "no data" (maxCx < 0).  Deliberately not copying the
        # size columns' sentinels: those seed min from _kCoordWidth/_kCoordHeight
        # (320x240) while real coordinates reach 639x359, so an object wider than
        # 320 keeps a wrong minWidth forever.  See ClipRecordDialog.py.
        self._cur.execute(
            '''INSERT INTO objects '''
            '''(camLoc, timeStart, timeStop, type, '''
            '''minWidth, maxWidth, minHeight, maxHeight, '''
            '''minCx, maxCx, minCy, maxCy) Values '''
            '''(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (cameraLocation, int(timeStart), int(timeStart),
             objType, _kCoordWidth, 0, _kCoordHeight, 0,
             _kNoCentroid, -1, _kNoCentroid, -1))

        newId = self._cur.execute('''SELECT last_insert_rowid()''')
        newId = newId.fetchone()[0]

        # Object uids are reused by SQLite after deletions (the uid column is an
        # INTEGER PRIMARY KEY, not AUTOINCREMENT).  A previous, now-deleted
        # object with this same uid may have left a stale row behind in the
        # metadata side table (objectAttributes), because that table is keyed by
        # objUid and is not part of the objects row.  If we don't clear it, this
        # brand-new, unrelated object inherits the old object's face name /
        # nudity flag — surfacing a phantom "Bernie"/nudity tag on an otherwise
        # 'unknown' object.  Guarantee a fresh object starts with no attributes.
        self._cur.execute('''DELETE FROM objectAttributes WHERE objUid=?''',
                          (newId,))

        # Do a save right away so that we don't block out other processes.
        # TODO: Does that hit our speed at all?
        self.save()

        return newId


    ###########################################################
    def addFrame(self, objId, frame, time, bbox, objType, action):
        """Add a new frame of data

        @param  objId    The id of the object in the database
        @param  frame    The number of the frame to add
        @param  time     The time in ms that matches frame
        @param  bbox     A bounding box for the object at the given frame
        @param  objType  The object's type; passed here for speed--
                         this should match the type used for addObject().
        @param  action   The action to add to the database; or None if none.
        """
        assert self._connection is not None

        # Make time an int...
        time = int(time)

        try:
            self._cur.execute('''INSERT INTO motion'''
                              ''' Values (?, ?, ?, ?, ?, ?, ?)''',
                    (objId, frame, time, bbox[0], bbox[1], bbox[2], bbox[3]))
        except sql.IntegrityError:
            # We get this when we violate the uniqueness requirement of the
            # primary key.  In other words: when we try to add a second entry
            # with the same objId and time...  My guess is that this happens
            # due to a tracker bug (?).  In any case, we'll just warn and
            # ignore, but we should get to the bottom of it.
            self._logger.warning("Skipping duplicate data: " +
                str((objId, frame, time, bbox[0], bbox[1], bbox[2], bbox[3])))
            return

        # Update the objects table with some summary info; note that we'll have
        # to update this summary info (if we care) if we ever delete stuff from
        # the motion table.
        # TODO - fix time here, always sets stop time
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        # Centroid extremes ride along in the same UPDATE.  Travel is measured as
        # the SPAN of the centroid cloud, which is order-independent, so it folds
        # into MIN()/MAX() exactly like the size columns and needs no memory of
        # the previous frame.  (Cumulative path length would have needed per-object
        # state -- and would have been the wrong stat anyway, since it grows with
        # the frame-to-frame jitter of a blob that is not going anywhere.)
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        self._cur.execute(
            '''UPDATE objects SET timeStop=?, '''
            '''minWidth=MIN(?, minWidth), maxWidth=MAX(?, maxWidth), '''
            '''minHeight=MIN(?, minHeight), maxHeight=MAX(?, maxHeight), '''
            '''minCx=MIN(?, minCx), maxCx=MAX(?, maxCx), '''
            '''minCy=MIN(?, minCy), maxCy=MAX(?, maxCy) '''
            '''WHERE uid=?''',
            (time, width, width, height, height, cx, cx, cy, cy, objId))

        # Try to extend an action if it already exists; otherwise create a new
        # one.  If frames are always added in order, this is perfect.  If frames
        # are added out of order, there might be cases where we'll end up
        # creating a whole bunch of table rows for parts of the same action
        # sequence, like (10 - 12), (13 - 13), (14 - 20), etc.  If this happens
        # in reality, we should add some logic to condense these sequences.
        if action is not None:
            # First try to extend...
            self._cur.execute(
                '''UPDATE actions SET frameStop=?, timeStop=? '''
                '''WHERE objUID=? AND frameStop=? AND action=?''',
                (frame, time, objId, frame-1, action))

            # If the extend failed, do the insert...
            if self._cur.rowcount != 1:
                assert self._cur.rowcount == 0
                self._cur.execute(
                    '''INSERT INTO actions Values (?, ?, ?, ?, ?, ?, ?)''',
                    (objId, objType, action, frame, time, frame, time))


    ###########################################################
    #def updateFrame(self, objId, frame, bbox):
    #    """Change bbox data for an existing frame
    #
    #    @param  objId    The id of the object in the database
    #    @param  frame    The number of the frame to edit
    #    @param  bbox     A bounding box for the object at the given frame
    #    """
    #    assert self._connection is not None
    #
    #    # Right now, we never end up calling this function; if we ever do
    #    # again, we'll need to update to adjust the actions table...
    #    assert False, "May need to update actions table too!"
    #
    #    self._cur.execute(
    #        '''UPDATE motion SET x1=?, y1=?, x2=?, y2=? WHERE objUid=? AND frame=?''',
    #        (bbox[0], bbox[1], bbox[2], bbox[3], objId, frame))


    ###########################################################
    #def deleteFrame(self, objId, frame):
    #    """Remove a frame from the database
    #
    #    @param  objId  The id of the object in the database
    #    @param  frame  The frame number to delete
    #    """
    #    assert self._connection is not None
    #
    #    # Right now, we never end up calling this function; if we ever do
    #    # again, we'll need to update to adjust the actions table...
    #    assert False, "May need to update actions table too!"
    #
    #    self._cur.execute('''DELETE FROM motion WHERE objUid=? AND frame=?''',
    #                      (objId, frame))


    ###########################################################
    #def updateType(self, objId, objType):
    #    """Update the recognition information for an object
    #
    #    @param  objId       The id of the object in the database
    #    @param  objType     A label for the new type
    #    """
    #    assert self._connection is not None
    #
    #    # Right now, we never end up calling this function; if we ever do
    #    # again, we'll need to update to adjust the actions table...
    #    assert False, "May need to update actions table too!"
    #
    #    if objType.lower() in ('unknown', 'nonperson'):
    #        objType = 'object'
    #
    #    self._cur.execute(
    #        '''UPDATE objects SET type=? WHERE uid=?''',
    #        (objType, objId))


    ###########################################################
    def getObjectType(self, objId):
        """Return the type of a given object

        @param  objId  The id of the object to identify
        @return type   The type of the object
        """
        assert self._connection is not None

        result = self._cur.execute('''SELECT type FROM objects WHERE uid=?''',
                                   (objId,))
        result = result.fetchone()
        if result is not None:
            return result[0]
        else:
            # TODO: not sure why this happens, but be robust...
            return "unknown"


    ###########################################################
    def getObjectTypes(self, objIds):
        """Return a dict mapping object ID to type string for a list of IDs.

        More efficient than calling getObjectType() in a loop.

        @param  objIds  A list/sequence of object IDs.
        @return types   A dict {objId: typeStr}.  Missing IDs map to 'unknown'.
        """
        assert self._connection is not None
        if not objIds:
            return {}
        placeholders = ','.join('?' * len(objIds))
        rows = self._cur.execute(
            'SELECT uid, type FROM objects WHERE uid IN (%s)' % placeholders,
            list(objIds)
        ).fetchall()
        result = {row[0]: row[1] for row in rows}
        for objId in objIds:
            if objId not in result:
                result[objId] = 'unknown'
        return result


    ###########################################################
    def _ensureAttributesTable(self):
        """Create the objectAttributes table if it doesn't exist.

        This table stores per-object detection attributes that don't fit the
        single 'type' column on the objects table: the recognized face name
        (from ArcFace) and a nudity flag (from NudeNet).  These are kept
        separate so the object-type voting engine continues to operate on a
        clean categorical type (person/vehicle/animal).
        """
        try:
            self._cur.execute(
                '''CREATE TABLE IF NOT EXISTS objectAttributes '''
                '''(objUid INTEGER PRIMARY KEY, faceName TEXT, '''
                '''faceConf REAL, faceDetConf REAL, gender TEXT, age INTEGER, '''
                '''subType TEXT, detConf REAL, '''
                '''nudity INTEGER, nudityDetail TEXT)'''
            )
        except sql.OperationalError:
            # Happens if two processes try at same time...
            pass

        # Upgrade path: add any columns missing from tables created by an
        # earlier version of this schema.
        try:
            cols = [r[1] for r in
                    self._cur.execute('''PRAGMA table_info(objectAttributes)''')]
            for colName, colType in (('nudityDetail', 'TEXT'),
                                     ('faceConf', 'REAL'),
                                     ('gender', 'TEXT'),
                                     ('age', 'INTEGER'),
                                     ('faceDetConf', 'REAL'),
                                     ('subType', 'TEXT'),
                                     ('detConf', 'REAL')):
                if colName not in cols:
                    self._cur.execute(
                        '''ALTER TABLE objectAttributes ADD COLUMN %s %s'''
                        % (colName, colType))
        except sql.OperationalError:
            pass

        # Self-heal: drop attribute rows whose object no longer exists.  Earlier
        # versions did not clean objectAttributes when objects were deleted, so
        # existing databases accumulate orphans; because object uids are reused,
        # those orphans re-attach to new objects and surface phantom face /
        # nudity tags on 'unknown' objects.  Runs once per DB open; the table is
        # small (one row per face/nudity detection) so this is cheap.
        try:
            self._cur.execute(
                '''DELETE FROM objectAttributes '''
                '''WHERE objUid NOT IN (SELECT uid FROM objects)''')
        except sql.OperationalError:
            pass


    ###########################################################
    def setObjectAttributes(self, objId, faceName=None, faceConf=None,
                            faceDetConf=None, gender=None, age=None,
                            subType=None, detConf=None, nudity=False,
                            nudityDetail=None):
        """Store detection attributes for an object.

        @param  objId         The object's database uid.
        @param  faceName      Recognized face name, or None if unrecognized.
        @param  faceConf      Recognition confidence (cosine sim 0-1), or None.
        @param  faceDetConf   Face detection score (how sure it's a face), None.
        @param  gender        'M' / 'F' from the genderage model, or None.
        @param  age           Estimated age (int), or None.
        @param  subType       Specific YOLO class (dog/cat/car/...), or None.
        @param  detConf       YOLO object detection confidence (0-1), or None.
        @param  nudity        True if nudity was detected on this object.
        @param  nudityDetail  Encoded "CLASS=score,..." breakdown, or None.
        """
        assert self._connection is not None
        if not (faceName or nudity or gender or subType or
                age is not None or detConf is not None or
                faceDetConf is not None):
            return
        self._cur.execute(
            '''INSERT OR REPLACE INTO objectAttributes '''
            '''(objUid, faceName, faceConf, faceDetConf, gender, age, '''
            '''subType, detConf, nudity, nudityDetail) '''
            '''VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (objId,
             faceName if faceName else None,
             faceConf,
             faceDetConf,
             gender if gender else None,
             age,
             subType if subType else None,
             detConf,
             1 if nudity else 0,
             nudityDetail if nudityDetail else None))
        self.save()


    ###########################################################
    def getObjectAttributes(self, objIds):
        """Return detection attributes for a list of object IDs.

        @param  objIds  A list/sequence of object IDs.
        @return attrs   A dict {objId: attrDict} for objects that have
                        attributes.  Objects with no attributes are omitted.
                        Each attrDict has keys: faceName (str|None),
                        faceConf (float|None), faceDetConf (float|None),
                        gender (str|None), age (int|None), subType (str|None),
                        detConf (float|None), nudity (bool),
                        nudityDetail (str|None).
        """
        assert self._connection is not None
        if not objIds:
            return {}
        placeholders = ','.join('?' * len(objIds))
        rows = self._cur.execute(
            'SELECT objUid, faceName, faceConf, faceDetConf, gender, age, '
            'subType, detConf, nudity, nudityDetail FROM objectAttributes '
            'WHERE objUid IN (%s)' % placeholders,
            list(objIds)
        ).fetchall()
        return {
            row[0]: {
                'faceName':     row[1],
                'faceConf':     row[2],
                'faceDetConf':  row[3],
                'gender':       row[4],
                'age':          row[5],
                'subType':      row[6],
                'detConf':      row[7],
                'nudity':       bool(row[8]),
                'nudityDetail': row[9],
            }
            for row in rows
        }


    ###########################################################
    def getAttributeSchema(self):
        """Return the column schema of objectAttributes, excluding the PK.

        @return schema  List of (colName, colType) tuples, e.g.
                        [('faceName', 'TEXT'), ('age', 'INTEGER'), ...]
                        Auto-reflects future columns added via ALTER TABLE.
        """
        assert self._connection is not None
        rows = self._cur.execute(
            "PRAGMA table_info(objectAttributes)"
        ).fetchall()
        # row format: (cid, name, type, notnull, dflt_value, pk)
        return [(row[1], row[2].upper()) for row in rows if row[1] != 'objUid']


    ###########################################################
    def searchByAttributes(self, filters,
                           timeStart=None, timeStop=None, cameras=None):
        """Find objects whose attributes match the given filters.

        @param  filters    Ordered list of filter dicts, each with keys:
                             'col'  — column name in objectAttributes
                             'op'   — one of: =, !=, >, >=, <, <=,
                                      contains, starts_with, not_contains,
                                      is_true, is_false
                             'val'  — the comparison value (None for bool ops)
                             'conn' — how this condition joins the previous one:
                                      'AND', 'OR', or 'EXCEPT'.  Ignored on the
                                      first filter; defaults to 'AND' otherwise.
                           Text comparisons are case-insensitive.  EXCEPT means
                           "AND NOT" (exclude objects matching the condition),
                           and is NULL-safe.
        @param  timeStart  Optional absolute-ms lower bound; objects whose
                           timeStop is before this are excluded (overlap test).
        @param  timeStop   Optional absolute-ms upper bound; objects whose
                           timeStart is after this are excluded (overlap test).
        @param  cameras    Optional list of camera locations to restrict to.
        @return rows       List of (uid, camLoc, timeStart, timeStop) tuples
                           ordered by timeStart ascending.
        """
        assert self._connection is not None
        if not filters:
            return []

        # Reflect the live schema: column names (injection guard) + types so we
        # can apply case-insensitive collation to text equality.
        try:
            info = self._cur.execute(
                "PRAGMA table_info(objectAttributes)").fetchall()
            known    = {r[1] for r in info}
            colTypes = {r[1]: (r[2] or '').upper() for r in info}
        except Exception:
            return []

        def _positiveClause(col, op, val):
            """Return (sqlFragment, paramsList) for a single positive match."""
            if op == 'contains':
                return ("a.%s LIKE ?" % col, ['%' + str(val) + '%'])
            if op == 'starts_with':
                return ("a.%s LIKE ?" % col, [str(val) + '%'])
            if op == 'not_contains':
                return ("(a.%s IS NULL OR a.%s NOT LIKE ?)" % (col, col),
                        ['%' + str(val) + '%'])
            if op == 'is_true':
                return ("a.%s = 1" % col, [])
            if op == 'is_false':
                return ("(a.%s IS NULL OR a.%s = 0)" % (col, col), [])
            if op in ('=', '!='):
                # Case-insensitive for text columns.
                if colTypes.get(col) == 'TEXT':
                    return ("a.%s %s ? COLLATE NOCASE" % (col, op), [val])
                return ("a.%s %s ?" % (col, op), [val])
            if op in ('>', '<', '>=', '<='):
                return ("a.%s %s ?" % (col, op), [val])
            return (None, [])

        where  = ''
        params = []
        for i, f in enumerate(filters):
            col = f.get('col', '')
            op  = f.get('op',  '')
            val = f.get('val')
            if col not in known:
                continue

            frag, fragParams = _positiveClause(col, op, val)
            if frag is None:
                continue

            conn = (f.get('conn') or 'AND').upper() if i > 0 else None

            if not where:
                # First effective condition.
                where = frag
                params.extend(fragParams)
            elif conn == 'OR':
                where += ' OR ' + frag
                params.extend(fragParams)
            elif conn == 'EXCEPT':
                # AND NOT, NULL-safe: keep rows where the condition isn't true.
                where += ' AND ((' + frag + ') IS NOT 1)'
                params.extend(fragParams)
            else:  # AND (default)
                where += ' AND ' + frag
                params.extend(fragParams)

        if not where:
            return []

        where = '(' + where + ')'

        # Overlap test: keep objects whose lifespan intersects [timeStart, timeStop]
        if timeStart is not None:
            where += ' AND o.timeStop >= ?'
            params.append(int(timeStart))
        if timeStop is not None:
            where += ' AND o.timeStart <= ?'
            params.append(int(timeStop))

        if cameras:
            cam_ph = ','.join('?' * len(cameras))
            where += ' AND o.camLoc IN (%s)' % cam_ph
            params.extend(cameras)

        sql = (
            'SELECT DISTINCT o.uid, o.camLoc, o.timeStart, o.timeStop '
            'FROM objects o '
            'INNER JOIN objectAttributes a ON o.uid = a.objUid '
            'WHERE %s '
            'ORDER BY o.timeStart ASC'
        ) % where

        try:
            return self._cur.execute(sql, params).fetchall()
        except Exception:
            self._logger.warning("searchByAttributes failed", exc_info=True)
            return []


    ###########################################################
    def getObjectsBetweenTimes(self, startTime=None, endTime=None, includeAllFields=False):
        """Retrieve objects seen between the given times

        @param  startTime  The time to begin the search, None for the beginning
        @param  endTime    The time to stop the search, None for most recent
        @param  includeAllFields Whether to include more than just IDs
        @return idList     A list of ids (or tuples, if includeAllFields=True) of active objects
        """
        assert self._connection is not None

        # Construct a search criteria based on the requested times
        searchStr = ''
        if startTime:
            searchStr += 'timeStop >= %i' % int(startTime)
            if endTime:
                searchStr += ' AND '
        if endTime:
            searchStr += 'timeStart <= %i' % int(endTime)

        if searchStr and self._filterStr:
            searchStr = ' AND '.join([searchStr, self._filterStr])
        elif self._filterStr:
            searchStr = self._filterStr

        if searchStr:
            searchStr = "WHERE " + searchStr

        # Do the search...
        selection = "uid, timeStart, timeStop, type" if includeAllFields else "uid"
        objs = self._cur.execute('''SELECT %s FROM objects %s''' %
                                (selection, searchStr,)).fetchall()

        if includeAllFields:
            objList = objs
        else:
            # Parse the rows into a list of ids
            objList = [row[0] for row in objs]

        return objList


    ###########################################################
    def getActiveObjectsBetweenTimes(self, startTime=None, endTime=None):
        """Retrieve active objects seen between the given times.

        Similar to getObjectsBetweenTimes but additionally verifies that
        those objects actually have entries in the motion table in the
        specified time ranges.

        @param  startTime  The time to begin the search, None for the beginning
        @param  endTime    The time to stop the search, None for most recent
        @return idList     A list of ids of active objects
        """
        assert self._connection is not None

        objIds = self.getObjectsBetweenTimes(startTime, endTime)
        if not startTime and not endTime:
            return objIds

        searchStr = ''
        if startTime:
            searchStr += 'time >= %i' % int(startTime)
            if endTime:
                searchStr += ' AND '
        if endTime:
            searchStr += 'time <= %i' % int(endTime)
        if searchStr:
            searchStr = "WHERE " + searchStr

        motionIds = self._cur.execute('''SELECT DISTINCT objUid FROM motion '''
                                      '''%s''' % (searchStr,)).fetchall()
        motionIds = [row[0] for row in motionIds]

        return list(set(objIds).intersection(motionIds))


    ###########################################################
    def deleteCameraLocationDataBetween(self, camLoc, startMs, stopMs):
        """Delete data associated with a camera locaiton.

        Automatically does a save() for you.

        @param  camLoc     The camera location to delete data at.
        @param  startMs    The ms at which to start deleting data.
        @param  lastMs     The last ms at which to delete data.
        """
        assert self._connection is not None
        if stopMs < startMs:
            assert False, "stopMs must be >= startMs"
            return

        # Get any affected object ids
        objInfo = self._cur.execute('''SELECT uid, timeStart, timeStop FROM '''
                '''objects WHERE camLoc=? AND timeStart<=? AND timeStop>=?''',
                (camLoc, int(stopMs), int(startMs))).fetchall()

        idList = [info[0] for info in objInfo]

        # Delete non-existant times from the motion table
        if idList:
            timeStr = " AND time >=%i AND time <=%i" % (startMs, stopMs)

            if len(idList) == 1:
                searchStr = "DELETE FROM motion WHERE objUid=%i" % idList[0]
            else:
                searchStr = "DELETE FROM motion WHERE objUid in %s" % \
                            (str(tuple(idList)))

            self._cur.execute(searchStr + timeStr)

            # Remove objects that no longer have any motion data
            orphanedUids = []
            for uid in idList:
                isUidInMotion = self._cur.execute(
                    '''SELECT objUid FROM motion WHERE objUid=?'''
                    ''' LIMIT 1''', (uid,)).fetchone() is not None
                if not isUidInMotion:
                    orphanedUids.append(uid)

            if orphanedUids:
                self._deleteObjects(orphanedUids)

            for objId, start, stop in objInfo:
                if objId in orphanedUids:
                    continue

                if start < startMs:
                    if stop > stopMs:
                        # Add a new object
                        objType, minW, maxW, minH, maxH, = \
                            self._cur.execute('''SELECT type, '''
                                '''minWidth, maxWidth, minHeight, maxHeight '''
                                '''FROM objects WHERE '''
                                '''uid=?''', (objId,)).fetchone()
                        self._cur.execute('''INSERT INTO objects (camLoc, '''
                                '''timeStart, timeStop, type, '''
                                '''minWidth, maxWidth, minHeight, maxHeight) '''
                                '''Values '''
                                '''(?, ?, ?, ?, ?, ?, ?, ?)''',
                                (camLoc, start, stop, objType, minW, maxW,
                                 minH, maxH))
                        newObjId = self._cur.execute(
                                '''SELECT last_insert_rowid()''').fetchone()[0]
                        # newObjId may be a REUSED uid, so strip whatever the
                        # previous holder left in the attributes side table --
                        # the same guard addObject already has.  Without it a
                        # split half can surface a phantom face name or nudity
                        # flag belonging to an unrelated, deleted object.
                        self._cur.execute(
                            '''DELETE FROM objectAttributes WHERE objUid=?''',
                            (newObjId,))
                        # Update the motion table with the new object id.
                        self._cur.execute('''UPDATE motion SET objUid=? WHERE'''
                                          ''' objUid=? AND time>?''',
                                          (newObjId, objId, stopMs))
                        # Set the min and max times on the new/old object.
                        self._cur.execute('''UPDATE objects SET timeStart='''
                            '''(SELECT MIN(time) FROM motion WHERE objUid=?) '''
                            '''WHERE uid=?''', (newObjId, newObjId))

                        # TODO: Probably should recalculate minWidth, maxWidth,
                        #       minHeight, maxHeight
                        # ...but DO recalculate the centroid extremes.  Copying
                        # them from the original would give both halves the whole
                        # track's travel, and travel is far more sensitive to
                        # truncation than a min/max is: half a crossing that is
                        # cut here would still claim the full distance and sail
                        # through any filter.  DiskCleaner splits routinely.
                        self._recomputeCentroidExtremes(newObjId)
                    # Set the new stop time
                    self._cur.execute('''UPDATE objects SET timeStop='''
                        '''(SELECT MAX(time) FROM motion WHERE objUid=?) '''
                        '''WHERE uid=?''', (objId, objId))
                    self._recomputeCentroidExtremes(objId)

                elif stop > stopMs:
                    # Adjust the startMs to be the new minimum
                    self._cur.execute('''UPDATE objects SET timeStart='''
                            '''(SELECT MIN(time) FROM motion WHERE objUid=?) '''
                            '''WHERE uid=?''', (objId, objId))
                    self._recomputeCentroidExtremes(objId)
                else:
                    assert False, "Should have been orphaned... %s" % \
                                   str((start, startMs, stop, stopMs, objId))

            # Save right away--don't leave it up to the client...
            self.save()


    ###########################################################
    def tidyObjectTable(self):
        """Tidy up the object table, removing orphaned objects.

        This could get slow with large numbers of objects.  Ideally, don't run
        it too often...
        """
        # Find orphaned UIDs.
        #
        # This is equivalent to the following SQL:
        #   oldObjects = [row[0] for row in self._cur.execute(
        #       '''SELECT uid FROM objects WHERE uid NOT IN'''
        #       ''' (SELECT DISTINCT objUid FROM motion)'''
        #   ).fetchall()]
        # ...but the above SQL slows down quite a bit with large databases.
        #
        # Actually: just the SELECT DISTINCT bit above is pretty slow in a DB
        # with 333 unique objUids and 463545 rows.
        #
        # Our code is faster because (apparently) detecting the presence of
        # an object is faster than finding all unique object IDs...


        # Get the timeStart of the last added object.  We won't look for
        # orphans that are younger than 15 minutes before that time.
        # ...we do this because there may be a delay between adding an object
        # and adding the first bit of motion data about it.  This shouldn't
        # count as an orphaned object...
        startTime = time.time()
        lastTime = self._cur.execute(
            '''SELECT timeStart FROM objects ORDER BY uid DESC LIMIT 1'''
        ).fetchone()
        if not lastTime:
            return
        (lastTime,) = lastTime

        # NOTE: No clue how this can happen but it was in case 17707 and dying
        #       below with "unsupported operand type(s) for -: 'NoneType' and
        #       'int'". DB corruption of some sort? Regardless, if this happens
        #       we'll log the error and pick a "safe" time of 24 hours ago.
        if lastTime is None:
            self._logger.error("Last added time retrieved as None")
            lastTime = int(time.time()*1000) - (1000*60*60*24)

        minStartTime = lastTime - (1000 * 60 * 15)

        orphanedUids = []
        prevUid = 0
        while True:
            # Get the next N uids in the object list.  We work with smaller
            # groups to keep from ever having a super-long database access.
            idList = [row[0] for row in self._cur.execute(
                '''SELECT uid FROM objects WHERE uid > ? AND timeStart < ?'''
                ''' ORDER BY uid LIMIT 1000''', (prevUid, minStartTime)
            ).fetchall()]

            # If no more UIDs, we're done looking for orphans!
            if not idList:
                break

            # Do this relatively quick query on motion
            for uid in idList:
                isUidInMotion = self._cur.execute(
                    '''SELECT objUid FROM motion WHERE objUid=?'''
                    ''' LIMIT 1''', (uid,)).fetchone() is not None
                if not isUidInMotion:
                    orphanedUids.append(uid)
            prevUid = idList[-1]

        if orphanedUids:
            self._logger.warning("Detected " + str(len(orphanedUids)) + " orphaned objects:" + str(orphanedUids))
            self._deleteObjects(orphanedUids)

            # Save right away--don't leave it up to the client...
            self.save()
        self._logger.info("tidyObjectTable took %.02fsec" % (time.time()-startTime))


    ###########################################################
    def getObjectBboxesBetweenTimes(self, objIds, startTime=None,
                                        endTime=None):
        """Retrieve bounding boxes for an object between the given times

        If objIds contains more than one object ID, the results will be
        ordered by the object ID.  For a given object ID, results will be
        ordered by time.

        @param  objIds     A list or set of object IDs in the database.
        @param  startTime  The time to begin the search, None for the beginning
        @param  endTime    The time to stop the search, None for most recent
        @return bboxes     A list of (x1, y1, x2, y2, frame, time, objId) tuples
                           for each bbox within the given times.
        """
        # No need to hit the database if no objects...
        if not objIds:
            return []

        # Create all the different pieces of our search string, which will
        # be combined with AND.
        if len(objIds) == 1:
            filterPieces = ['objUid = %i' % list(objIds)[0]]
        else:
            filterPieces = ['objUid in %s' % str(tuple(objIds))]
        if startTime:
            filterPieces.append('time >= %i' % int(startTime))
        if endTime:
            filterPieces.append('time <= %i' % int(endTime))

        searchStr = ' AND '.join(filterPieces)

        bboxes = self._cur.execute('''SELECT x1, y1, x2, y2, frame, time, '''
                                   '''objUid FROM motion WHERE %s '''
                                   '''ORDER BY objUid ASC, time ASC'''
                                   % searchStr)

        return bboxes.fetchall()


    ###########################################################
    def getObjectRangesBetweenTimes(self, startTime=None, endTime=None):
        """Retrieve time ranges for an object between the given times.

        Takes the current filter string into account.

        @param  startTime    The time to begin the search, None for the beginning
        @param  endTime      The time to stop the search, None for most recent
        @return resultItems  An iterable of tuples, like this: [
                               (objId, ((firstMs, firstFrame),
                                        (lastMs, lastFrame)), cameraLocation)
                               ...
                             ]
        """
        assert self._connection is not None

        # Construct a search criteria based on the requested times
        # Swiped from getObjectsBetweenTimes.
        objSearchStr = ''
        if startTime:
            objSearchStr += 'timeStop >= %i' % int(startTime)
            if endTime:
                objSearchStr += ' AND '
        if endTime:
            objSearchStr += 'timeStart <= %i' % int(endTime)

        if objSearchStr and self._filterStr:
            objSearchStr = ' AND '.join([objSearchStr, self._filterStr])
        elif self._filterStr:
            objSearchStr = self._filterStr

        if objSearchStr:
            objSearchStr = "WHERE " + objSearchStr

        # Create all the different pieces of our motion search string, which
        # will be combined with AND.
        # TODO: Use SQL's "between"!
        filterPieces = []
        if startTime:
            filterPieces.append('m.time >= %i' % int(startTime))
        if endTime:
            filterPieces.append('m.time <= %i' % int(endTime))
        searchStr = ' AND '.join(filterPieces)
        if searchStr:
            searchStr = "WHERE " + searchStr

        # Run the super-crazy execute to get all this stuff.  It seems pretty
        # quick for the most part, considering everything it's doing...
        results = self._cur.execute('''
          SELECT x.camLoc, x.objUid, y.time, y.frame, z.time, z.frame FROM (
            SELECT m.camLoc,m.objUid,min(m.time) as minTime,max(m.time) as maxTime FROM (
              SELECT * FROM (
                SELECT camLoc,uid as objUid FROM objects %s
              ) NATURAL JOIN (
                motion
              )
            ) m
            %s GROUP BY m.objUid
          ) x
          JOIN motion y ON y.objUid = x.objUid AND y.time = x.minTime
          JOIN motion z ON z.objUid = x.objUid AND z.time = x.maxTime
        ''' % (objSearchStr, searchStr)).fetchall()

        # Return in the right format
        # TODO: Change to just return results, then change callers.  That
        # should be slightly faster...
        return ((c[1], ((c[2], c[3]), (c[4], c[5])), c[0])
                for c in results                    )


    ###########################################################
    def getObjectStartTime(self, objId):
        """Retrieve the time an object first appeared

        @param  objId      The id of the object in the database
        @return startTime  The time of the object's appearance, or -1 if the
                           object no longer exists.
        """
        result = self._cur.execute(
            '''SELECT timeStart FROM objects WHERE uid=?''', (objId,))
        row = result.fetchone()
        # The object can be deleted between a caller collecting its id and this
        # lookup -- DiskCleaner runs in another process against the same file.
        # -1 is the sentinel getMostRecentObjectTime already uses.
        return row[0] if row is not None else -1


    ###########################################################
    def getFrameAtTime(self, objId, time):
        """Retrieve the frame for the given time

        For this function to return a value there must be an entry in the
        motion table within 10 ms of the requested time

        @param  objId     An object that was tracked at the given time
        @param  time      The requested time
        @return frame     The frame closest to the requested time, or -1
        @return distance  The abs ms distance from the requested time, or -1
        """
        variability = 10
        # Find the closest time in the database
        results = self._cur.execute('''SELECT time FROM motion WHERE '''
                                    '''objUID=? AND time>? AND time<?''',
                                    (objId, int(time)-variability,
                                     int(time)+variability))
        bestTime = -1
        for row in results:
            if bestTime == -1:
                bestTime = row[0]
            else:
                distance = abs(time-row[0])
                if distance < abs(time-bestTime):
                    bestTime = row[0]
                else:
                    break

        if bestTime == -1:
            return -1, -1

        # Find the frame number of the closest time
        result = self._cur.execute(
            '''SELECT frame FROM motion WHERE time=? AND objUid=?''',
            (int(bestTime), objId))

        row = result.fetchone()

        if row:
            return row[0], abs(time-bestTime)

        return -1, -1


    ###########################################################
    def getBboxAtFrame(self, objId, frame):
        """Retrieve a bbox for an object at a given frame

        @param  objId  The id of the object in the database
        @param  frame  The requested frame
        @return bbox   The bbox, or None if it could not be found
        """
        result = self._cur.execute(
            '''SELECT x1, y1, x2, y2 FROM motion WHERE objUID=? AND frame=?''',
            (objId, frame))
        return result.fetchone()


    ###########################################################
    def getObjectStartFrame(self, objId):
        """Retrieve the frame an object first appeared

        @param  objId       The id of the object in the database
        @return startFrame  The frame of the object's appearance
        """
        startTime = self.getObjectStartTime(objId)
        return self.getFrameAtTime(objId, startTime)


    ###########################################################
    def getFirstObjectBbox(self, objId, startTime=None):
        """Retrieve the first bbox on or after a given time

        @param  objId       The id of the object in the database
        @param  startTime  The minimum time to search from, or None for all
        @return bbox       The first bbox found within the time specification
        @return frame      The frame number corresponding to the bbox
        @return time       The time corresponding to bbox, -1 on error
        """
        if startTime:
            timeQuery = '''AND time>= %i''' % int(startTime)
        else:
            timeQuery = ''

        searchStr = (
            '''SELECT x1, y1, x2, y2, frame, time FROM motion '''
            '''WHERE objUid=? %s ORDER BY time LIMIT 1'''
        ) % timeQuery
        result = self._cur.execute(searchStr, (objId,)).fetchone()
        if not result:
            return (-1, -1, -1, -1), -1, -1

        x1, y1, x2, y2, frame, objTime = result
        return (x1, y1, x2, y2), frame, objTime


    ###########################################################
    def getObjectFinalTime(self, objId):
        """Retrieve the last frame and time an object was tracked

        @param  objId  The id of the object in the database
        @return frame  The final frame the object was tracked, or -1
        @return time   The final time the object was tracked, or -1
        """
        result = self._cur.execute(
            '''SELECT timeStop FROM objects WHERE uid=?''', (objId,))

        row = result.fetchone()
        if row is None:
            # Deleted under us -- see getObjectStartTime.
            return -1, -1
        time = row[0]
        frame, _ = self.getFrameAtTime(objId, time)

        return frame, time


    ###########################################################
    def doCustomSearch(self, searchStr):
        """Perform a custom database query

        @param  searchStr   A search query to present to the database
        @return resultRows  The rows returned by the query
        """
        # This should be a select query...no adding or deleting and whatnot...
        assert searchStr.startswith('SELECT')

        resultRows = self._cur.execute(searchStr).fetchall()

        return resultRows


    ###########################################################
    def setMinSizeFilter(self, minHeight):
        """Restrict future searches to objects that were at least minSize big.

        @param  minHeight  The number of pixels to restrict future searches
                           to.  If None, disables the restriction.
        """
        if minHeight:
            # Check against maxHeight.  We want to know about objects that
            # were bigger than minHeight at some point in time...
            self._sizeFilter = 'maxHeight >= %d' % minHeight
        else:
            self._sizeFilter = ''

        self._setFilterStr()


    ###########################################################
    def _recomputeCentroidExtremes(self, objId):
        """Rebuild one object's centroid extremes from its current motion rows.

        Used wherever an object's set of frames changes after the fact (a track
        split by a partial delete, or rows moved between objects), because those
        paths copy summary stats forward rather than recomputing them.
        """
        try:
            self._cur.execute(
                '''UPDATE objects SET '''
                '''minCx=COALESCE((SELECT MIN((x1+x2)/2) FROM motion '''
                '''WHERE objUid=?), ?), '''
                '''maxCx=COALESCE((SELECT MAX((x1+x2)/2) FROM motion '''
                '''WHERE objUid=?), -1), '''
                '''minCy=COALESCE((SELECT MIN((y1+y2)/2) FROM motion '''
                '''WHERE objUid=?), ?), '''
                '''maxCy=COALESCE((SELECT MAX((y1+y2)/2) FROM motion '''
                '''WHERE objUid=?), -1) '''
                '''WHERE uid=?''',
                (objId, _kNoCentroid, objId, objId, _kNoCentroid, objId, objId))
        except sql.OperationalError:
            # Pre-migration database (read-only opens skip the upgrade); the
            # columns simply aren't there yet.
            pass


    ###########################################################
    @staticmethod
    def getTravelExpr():
        """-> the SQL expression for an object's travel, in analysis pixels.

        "Travel" is the SPAN of the object's centroid over its life: how far it
        got from where it started, summed across both axes.  It is the single
        best separator this fleet has between a real subject and camera noise --
        measured over 16,215 recorded objects, median 407 for person/animal
        against 102 for unknowns, where blob AREA only separates them 3.8x.
        Something that flickers in place at the edge of frame is the same SIZE
        as a real subject at distance; only movement tells them apart.

        Callers must pair this with a `maxCx >= 0` guard -- see _kNoCentroid.
        """
        return '((maxCx - minCx) + (maxCy - minCy))'


    ###########################################################
    def hasTravelColumns(self):
        """-> True if this database has the centroid columns.

        A read-only open skips _upgradeOldTablesIfNeeded entirely (see open()),
        so a search process can be looking at a database the back end has not
        migrated yet.  Filtering on a column that is not there raises
        "no such column" and fails the whole search; callers check this first and
        simply do not filter, which shows more than asked for rather than
        nothing at all.
        """
        try:
            cols = [r[1] for r in
                    self._cur.execute('''PRAGMA table_info(objects)''')]
            return 'maxCx' in cols
        except sql.OperationalError:
            return False


    ###########################################################
    def setMinTravelFilter(self, minTravel):
        """Restrict future searches to objects that moved at least this far.

        @param  minTravel  Travel in ANALYSIS pixels (already scaled for this
                           camera's processing size by the caller).  0 or None
                           disables the restriction.
        """
        if minTravel and self.hasTravelColumns():
            # maxCx >= 0 excludes objects that never got a frame; without it
            # their sentinel values produce a large bogus travel and they would
            # survive every threshold.
            self._travelFilter = '(maxCx >= 0 AND %s >= %d)' \
                                 % (self.getTravelExpr(), minTravel)
            self._travelFilterPx = int(minTravel)
        else:
            self._travelFilter = ''
            self._travelFilterPx = 0

        self._setFilterStr()


    ###########################################################
    def getMinTravelFilter(self):
        """-> the travel threshold currently in force, in analysis pixels.

        Lets a nested MinTravelTrigger combine with an outer one instead of
        overwriting it.  Two can legitimately be in play at once -- the Search
        screen's live slider wraps a query that may already carry the editor's
        own travel filter -- and without this the inner one's reset would clear
        the outer one, leaving the slider looking connected but doing nothing.
        """
        return getattr(self, '_travelFilterPx', 0)


    ###########################################################
    def countObjectsByTravel(self, minTravel, camLocs=None):
        """-> (matching, total) object counts for a travel threshold.

        Backs the live "showing N of M" readout next to the filter control: the
        raw pixel number means nothing on its own, so the UI shows what it
        actually does to the result set.  Deliberately ignores the current
        filter state so the denominator is always every object.

        @param  minTravel  Threshold in ANALYSIS pixels, same units as
                           setMinTravelFilter -- NOT the 1280x720 reference units
                           the UI and camera settings are quoted in.  Callers
                           holding a reference value must scale it first (halve
                           it for the usual 640x360 camera).
        @param  camLocs    Restrict to these cameras, or None for all.
        @return counts     (matching, total), or (None, None) if this database
                           predates the travel columns.
        """
        if not self.hasTravelColumns():
            return (None, None)
        where = ''
        args = []
        if camLocs:
            where = ' WHERE camLoc IN (%s)' % ','.join('?' * len(camLocs))
            args = list(camLocs)
        total = self._cur.execute(
            '''SELECT COUNT(*) FROM objects%s''' % where, args).fetchone()[0]

        # Mirror setMinTravelFilter: a threshold of 0 installs no filter at all,
        # so nothing is hidden -- including the handful of objects that have no
        # motion rows and therefore no travel to judge.  Counting those out here
        # would report "16,215 of 16,217" for a setting that actually shows all
        # of them.
        if not minTravel:
            return (total, total)

        cond = 'maxCx >= 0 AND %s >= ?' % self.getTravelExpr()
        joiner = ' AND ' if where else ' WHERE '
        matching = self._cur.execute(
            '''SELECT COUNT(*) FROM objects%s%s%s''' % (where, joiner, cond),
            args + [int(minTravel)]).fetchone()[0]
        return (matching, total)


    ###########################################################
    def setTargetFilter(self, targetAndActionList,
                        timeStart=None, timeStop=None):
        """Restrict future searches to objects of certain types

        NOTE: Currently unused

        @param  targetAndActionList  An iterable of object types and actions to
                                     include in searches, or the empty list to
                                     search on all.  Looks like: [
                                       ('person', 'walking'),
                                       ('person', 'running'),
                                       ('vehicle', 'any'),
                                       ...
                                     ]
        @param  timeStart   The time to start searching from, None for beginning
                            DOESN'T FULLY FILTER USING THIS; it's just for
                            optimizing our searches--you must filter yourself
                            later.
        @param  timeStop    The time to stop searching at, None for present
                            DOESN'T FULLY FILTER USING THIS; it's just for
                            optimizing our searches--you must filter yourself
                            later.
        """
        # Initially, start target box filter as nothing...
        self._targetRangeFilterDict = {}

        if not targetAndActionList:
            self._targetFilter = ''
        else:
            # We'll do a global OR over all of the filters...
            filters = []

            # Make a simple search for things that have the 'any' action...
            # SECURITY WARNING: The following is dangerous because of potential
            # SQL injection.  Can we avoid?
            anyTargets = set([target
                              for (target, action) in targetAndActionList
                              if action == 'any'])
            if anyTargets:
                filters.append(
                    '(type in ("' + '", "'.join(anyTargets) + '"))'
                )

            # If we need a specific action, we need to look up in the 'actions'
            # table to figure out what times are appropriate for each individual
            # object.  We'll build up a (potentially large) filter string to
            # find these cases...  First, find all the actionTargets; note that
            # if you are looking for ('person', 'walking') OR ('person', 'any')
            # that's the same as just looking for ('person', 'any'), so we filter
            # out any targets that are in the "anyTargets" set.
            actionTargets = [(target, action)
                             for (target, action) in targetAndActionList
                             if (action != 'any') and (target not in anyTargets)]
            if actionTargets:
                # Make a string to narrow down the entries we'll be getting back
                # from the 'actions' table so it's not _too_ huge (it still may
                # end up being pretty big).  If necessary, we can try to do other
                # types of filters too?
                timeStr = ''
                if timeStart is not None:
                    timeStr += (' timeStop>=%d AND ' % timeStart)
                if timeStop is not None:
                    timeStr += (' timeStart<=%d AND ' % timeStop)

                # Make the string to handle all of the target/actions.  We go
                # through a little extra work (using itertools) to still use the
                # '?' syntax here to avoid SQL injection.
                actionQuery = ' OR '.join(
                    ['(type=? AND action=?)'] * len(actionTargets)
                )

                # Get all the places between timeStart and timeStop where the
                # right types of objects are performing the right types of
                # actions.
                objRanges = self._cur.execute(
                    '''SELECT objUid, timeStart, timeStop FROM actions '''
                    '''WHERE ''' + timeStr + actionQuery,
                    list(itertools.chain(*actionTargets))
                ).fetchall()
                objUids = map(str, map(operator.itemgetter(0), objRanges))

                # We know that these objects were performing the right actions
                # at some point during the time period, so add them in.
                # Note: Objects may not have been performing the actions the
                #       whole time during the range.  We do add the limits to
                #       the _targetRangeFilterDict
                # TODO: OK that this could be large?
                filters.append('(uid in (%s))' % ', '.join(objUids))

                # Any objects that needed a certain action to be present no
                # longer match the whole time--they only match during certain
                # ranges.  Other functions will need to take that into account.
                #
                # NOTE: Any objects not referenced in this dict that still match
                # the target filter should be assumed to match for all times.
                #
                # Right now, only getObjectBboxesBetweenTimes() uses this.  ...but
                # maybe we should think more about whether getObjectStartTime and
                # getObjectFinalTime should too, for enter/exit trigger?
                for (objUid, timeStart, timeEnd) in objRanges:
                    thisFilter = '(time>=%d AND time <=%d)' % \
                                 (timeStart, timeEnd)

                    if objUid not in self._targetRangeFilterDict:
                        self._targetRangeFilterDict[objUid] = thisFilter
                    else:
                        self._targetRangeFilterDict[objUid] += \
                            (' OR ' + thisFilter)

            self._targetFilter = ' OR '.join(filters)

        self._setFilterStr()


    ###########################################################
    def setAttributeFilter(self, attrSpec, timeStart=None, timeStop=None):
        """Restrict future searches to objects with certain detection attributes.

        Follows the same set/clear lifecycle as setTargetFilter: callers must
        push the filter, run their queries, then clear with None.  The filter
        is an EXISTS probe against the objectAttributes side table (PK objUid),
        so it composes with the camera/target/size filters in _setFilterStr.

        @param  attrSpec   None to clear, else a dict:
                             {'nudity': True}
                               ...objects flagged with nudity.
                             {'face': True, 'faceNames': [names]}
                               ...objects with a detected face.  faceNames
                               empty = any face; the reserved name "Unknown"
                               matches faces that were detected but not
                               recognized; other names match faceName
                               case-insensitively.
        @param  timeStart  Unused (parity with setTargetFilter's signature).
        @param  timeStop   Unused (parity with setTargetFilter's signature).
        """
        _ = timeStart, timeStop

        if not attrSpec:
            self._attrFilter = ''
        else:
            conds = []
            if attrSpec.get('nudity'):
                conds.append('objectAttributes.nudity = 1')
            if attrSpec.get('face'):
                # A face was detected at all (recognized or not)...
                conds.append('objectAttributes.faceDetConf IS NOT NULL')

                names = [n for n in (attrSpec.get('faceNames') or []) if n]
                if names:
                    nameConds = []
                    wantUnknown = any(n.lower() == 'unknown' for n in names)
                    realNames = [n for n in names if n.lower() != 'unknown']
                    if realNames:
                        quoted = ", ".join(
                            "'%s'" % n.replace("'", "''") for n in realNames)
                        nameConds.append(
                            'objectAttributes.faceName COLLATE NOCASE '
                            'IN (%s)' % quoted)
                    if wantUnknown:
                        nameConds.append(
                            "(objectAttributes.faceName IS NULL OR "
                            "objectAttributes.faceName = '')")
                    conds.append('(' + ' OR '.join(nameConds) + ')')

            self._attrFilter = (
                '(EXISTS (SELECT 1 FROM objectAttributes WHERE '
                'objectAttributes.objUid = objects.uid AND %s))'
                % ' AND '.join(conds)
            )

        self._setFilterStr()


    ###########################################################
    def setCameraFilter(self, cameraList):
        """Restrict future searches to objects seen at certain camera locations

        @param  cameraList  A list of camera locations to include in searches,
                            or an empty list to search on all
        """
        if not cameraList:
            self._cameraFilter = ''
        else:
            # Camera names are user-entered and land in SQL text here (this is
            # a fragment, assembled by _setFilterStr, so there is nothing to
            # bind to).  Quote them as proper string literals and double any
            # embedded quote rather than relying on the UI's name validator.
            escaped = [str(name).replace("'", "''") for name in cameraList]
            self._cameraFilter = "camLoc in ('" + "', '".join(escaped) + "')"

        self._setFilterStr()


    ###########################################################
    def _setFilterStr(self):
        """Construct a search string from the given filters"""
        filters = []
        if self._cameraFilter:
            filters.append(self._cameraFilter)
        if self._targetFilter:
            filters.append(self._targetFilter)
        if self._attrFilter:
            filters.append(self._attrFilter)
        if self._sizeFilter:
            filters.append(self._sizeFilter)
        if self._travelFilter:
            filters.append(self._travelFilter)

        self._filterStr = " AND ".join(filters)


    ###########################################################
    def getCameraLocations(self, forceUseOfObjdb=False):
        """Retrieve a list of all camera locations in the database

        NOTE: In most cases you should try to use getCameraLocations from
              ClipManager, as it will be faster.  This will attempt to do
              that if possible, unless you force it not to.

        @param  forceUseOfObjdb  Prevent the optimization of using the clipdb.
        @return locNames         A list of all camera locations in the database.
        """
        if self._clipManager and not forceUseOfObjdb:
            return self._clipManager.getCameraLocations()

        results = self._cur.execute('''SELECT DISTINCT camLoc FROM objects''')
        return [result[0] for result in results]


    ###########################################################
    def getObjectsInfoForRange(self, minId, maxId):
        """ Return information for objects within ID range
        """
        assert self._connection is not None

        # Get any associated object ids
        objects = self._cur.execute('''SELECT uid,camLoc,type FROM objects WHERE uid>=? AND uid<=?''',
                                (minId,maxId)).fetchall()
        objMap = {}
        for obj in objects:
            objMap[obj[0]] = obj[2]
        return objMap

    ###########################################################
    def removeCameraLocation(self, location):
        """Remove all data associated with a given camera location.

        @param  location  The name of the location to remove.
        """
        assert self._connection is not None

        # Get any associated object ids
        ids = self._cur.execute('''SELECT uid FROM objects WHERE camLoc=?''',
                                (location,)).fetchall()

        idList = [str(row[0]) for row in ids]
        if idList:
            idListStr = ','.join(idList)
            self._cur.execute('''DELETE FROM objects WHERE uid IN (%s)''' %
                              idListStr)
            self._cur.execute('''DELETE FROM motion WHERE objUid IN (%s)''' %
                              idListStr)
            self._cur.execute('''DELETE FROM actions WHERE objUid IN (%s)''' %
                              idListStr)
            self._cur.execute('''DELETE FROM objectAttributes WHERE objUid IN (%s)'''
                              % idListStr)
            self.save()


    ###########################################################
    def getCameraLocation(self, objId):
        """Retrieve the camera location for a given object

        @param  objId     The object to search for
        @return location  The name of the camera location, or None if the
                          object no longer exists.
        """
        results = self._cur.execute('''SELECT camLoc FROM objects WHERE uid=?''', (objId,))
        row = results.fetchone()
        # Deleted under us -- see getObjectStartTime.
        return row[0] if row is not None else None


    ###########################################################
    def getObjectInfo(self, objId):
        """Retrieve camera + time span for a single object.

        @param  objId  The object to look up.
        @return        (camLoc, timeStart, timeStop), or None if not found.
        """
        row = self._cur.execute(
            '''SELECT camLoc, timeStart, timeStop FROM objects WHERE uid=?''',
            (objId,)).fetchone()
        if not row:
            return None
        return (row[0], row[1], row[2])


    ###########################################################
    def getSearchResults(self, query, timeStart=None, timeStop=None, procSizesMsRange=None):
        """Compute file and timepoints of interest for the given query

        @param  query             The trigger on which to perform the search
        @param  timeStart         The time to start searching from, None for all time
        @param  timeStop          The time to stop searching at, None for present
        @param  procSizesMsRange  A list of sizes the camera was processed at for
                                  certain ranges of time. Contains a list of 4-tuples
                                  of (procWidth, procHeight, firstMs, lastMs).
                                  Note: if the list contains only one 4-tuple, then
                                  procWidth and procHeight is unique, and firstMs and
                                  lastMs should be ignored; they may hold None values.
                                  If the list contains more than one 4-tuple, then
                                  procWidth and procHeight are not unique, and you must
                                  use the firstMs and lastMs to determine which
                                  procSize was used for a specified period of time.
        @return resultDict        A dict of [objId] = (triggerMsList)
        """
        # Perform the search
        results = query.search(timeStart, timeStop, 'single', procSizesMsRange)

        # key = objID, val = list of times triggered
        resultDict = {}
        for objId, frame, ms in results:
            if objId not in resultDict:
                resultDict[objId] = []
            resultDict[objId].append((ms, frame))

        return resultDict


    ###########################################################
    def getSearchResultsRanges(self, query, timeStart=None, timeStop=None, procSizesMsRange=None):
        """Like getSearchResults(), but returns a firstMs and lastMs per object.

        @param  query             The trigger on which to perform the search
        @param  timeStart         The time to start searching from, None for all time
        @param  timeStop          The time to stop searching at, None for present
        @param  procSizesMsRange  A list of sizes the camera was processed at for
                                  certain ranges of time. Contains a list of 4-tuples
                                  of (procWidth, procHeight, firstMs, lastMs).
                                  Note: if the list contains only one 4-tuple, then
                                  procWidth and procHeight is unique, and firstMs and
                                  lastMs should be ignored; they may hold None values.
                                  If the list contains more than one 4-tuple, then
                                  procWidth and procHeight are not unique, and you must
                                  use the firstMs and lastMs to determine which
                                  procSize was used for a specified period of time.
        @return resultItems       An iterable of tuples, like this: [
                                    (objId, ((firstMs, firstFrame),
                                             (lastMs, lastFrame)), camLoc)
                                    ...
                                  ]
        """
        # Perform the search
        return query.searchForRanges(timeStart, timeStop, procSizesMsRange)


    # The following is commented out since we don't use fileName
    # field in objdb2 any more.
    # Use the clipDb to re-implement this if we need it
    ###########################################################
    #def isFileInDb(self, fileName):
    #    """Check if data for a given file is in the database
    #
    #    @param  fileName  The file to look for
    #    @return exists    True if the file exists in the database
    #    """
    #    results = self._cur.execute(
    #        '''SELECT * FROM objects WHERE fileName=?''', (fileName,))
    #
    #    if results.fetchone():
    #        return True
    #    return False


    ###########################################################
    def updateLocationName(self, oldName, newName, changeMs):
        """Change the name of a camera location.

        @param  oldName   The name of the camera location to change.
        @param  newName   The new name for the camera location.
        @param  changeMs  The absolute ms at which the change took place.
        """
        # We need to split objects that occur across the time change.
        objs = self._cur.execute(
            '''SELECT uid, camLoc, timeStop, type, '''
            '''minWidth, maxWidth, minHeight, maxHeight '''
            '''FROM objects WHERE camLoc=? AND timeStop>=? AND timeStart<?''',
            (oldName, changeMs, changeMs)
        ).fetchall()
        for oldId, cam, stop, objType, minW, maxW, minH, maxH in objs:
            # Add a new object starting at changeMs.
            self._cur.execute(
                '''INSERT INTO objects (camLoc, timeStart, timeStop, type, '''
                '''minWidth, maxWidth, minHeight, maxHeight) Values '''
                '''(?, ?, ?, ?, ?, ?, ?, ?)''', (cam, changeMs,
                        stop, objType, minW, maxW, minH, maxH))
            newId = self._cur.execute('''SELECT last_insert_rowid()''')
            newId = newId.fetchone()[0]

            # Reused uid -- see the same guard in addObject.
            self._cur.execute(
                '''DELETE FROM objectAttributes WHERE objUid=?''', (newId,))

            # Update the related entries in the motion table.
            self._cur.execute('''UPDATE motion SET objUid=? WHERE objUid=? '''
                              '''AND time>=?''', (newId, oldId, changeMs))

            # Update the stop time of the old object.
            self._cur.execute('''UPDATE objects SET timeStop=? WHERE uid=?''',
                              (changeMs-1, oldId))

            # Both halves now own a subset of the frames, so their centroid
            # extremes have to be rebuilt rather than inherited whole.
            self._recomputeCentroidExtremes(newId)
            self._recomputeCentroidExtremes(oldId)

        # Update all objects that start after the time change.
        self._cur.execute('''UPDATE objects SET camLoc=? WHERE camLoc=? AND '''
                          '''timeStart>=?''', (newName, oldName, changeMs))


    ###########################################################
    def getMostRecentObjectTime(self, cameraLocation):
        """Find the most recent ms an object was seen at a given location.

        @param  cameraLocation  The camera to search.
        @return recentMs        The most recent ms seen or -1.
        """
        recentMs = self._cur.execute('''SELECT MAX(timeStop) from objects '''
                                     '''WHERE camLoc=?''', (cameraLocation,)
                                     ).fetchone()
        if not recentMs or recentMs[0] is None:
            return -1
        return recentMs[0]


    ###########################################################
    def hasAudio(self):
        """Does the currently selected clip contain audio?

        @return  hasAudio   True if the current clip has audio, and False
                            otherwise.
        """
        if self._clipReader:

            return self._clipReader.hasAudio()

        return False


    ###########################################################
    def _deleteObjects(self, objUidList):
        """Remove entries from the objects table.

        NOTE: This will not remove any objects that have the highest
              id in the table. If we do this it messes up our algorithm for
              assigning new ids.  Assuming all associated motion data was
              removed this will be taken care of eventually by the disk cleaner.

        @param  objUidList  A list of object uid strings to remove.
        """
        now = time.time()*1000

        # We need to make a new copy of so we don't change it under our caller.
        objUidList = objUidList[:] #PYCHECKER OK: This does have an effect, it makes a copy

        maxId = self._cur.execute('''SELECT MAX(uid) FROM objects''').fetchone()
        if maxId and maxId[0] in objUidList:
            objUidList.remove(maxId[0])

        stopTimeList = self._cur.execute('''SELECT uid, timeStop FROM objects'''
                                         ''' WHERE uid IN (%s)''' % ','.join(
                                         str(objId) for objId in objUidList)
                                         ).fetchall()
        for uid, timeStop in stopTimeList:
            # Select timeStop
            if timeStop > (now-_kObjectSaveBuffer):
                objUidList.remove(uid)

        if objUidList:
            idsStr = ','.join(str(objId) for objId in objUidList)
            self._cur.execute('''DELETE FROM objects WHERE uid IN (%s)''' %
                              idsStr) #PYCHECKER OK: This isn't redefining the genexpr from above since we have removed objects
            # objectAttributes is keyed by objUid and is not covered by the
            # DELETE above.  Object uids get reused, so an orphaned attribute
            # row would later re-attach to an unrelated new object (phantom face
            # name / nudity on an 'unknown' object).  Remove it with the object.
            self._cur.execute('''DELETE FROM objectAttributes WHERE objUid IN (%s)'''
                              % idsStr)


###############################################################################
#                File manipulation functions below this point                 #
###############################################################################


    ###########################################################
    def setupMarkedVideo(self, cameraLoc, firstMs, lastMs, playMs, objList=[],
                        displaySize=(320, 240), enableAudio=False, asyncRead=True):
        """Open a video with optional object borders

        @param  cameraLoc    The name of the camera location to view
        @param  firstMs      The absolute ms of the first frame to play
        @param  lastMs       The absolute ms of the last frame to play
        @param  playMs       The absolute ms of the start play time
        @param  objList      A list of object id's to draw bounding boxes for
        @param  displaySize  A (w,h) tuple of the desired frame size
        @return realFirstMs  The first requestable ms if firstMs didn't exist,
                             -1 on error
        @return realLastMs   The last requestable ms if lastMs didn't exist,
                             -1 on error
        """
        if not self._clipManager:
            return

        self._curVidCameraLoc = cameraLoc
        self._objList = objList
        self._displaySize = displaySize
        self._curVidPath = None
        self._curFrameMs = -1
        self._audioEnabled = enableAudio
        self._asyncReadEnabled = asyncRead

        # Find files we can access, starting from the play point and going
        # ahead and back.
        self._firstFile = self._clipManager.getFileAt(cameraLoc, playMs,
                                                      lastMs-playMs, 'after')
        self._lastFile  = self._firstFile
        if not self._firstFile:
            # If we couldn't find a file in the range of the play start to the
            # end, something is terribly wrong.
            return -1, -1

        firstFileStart, lastFileStop = \
                    self._clipManager.getFileTimeInformation(self._firstFile)

        # Walk forward until we reach our stop time or we break the file chain.
        while lastFileStop < lastMs:
            nextFile = self._clipManager.getNextFile(self._lastFile)
            if not nextFile:
                break

            tempStart, tempStop = self._clipManager.getFileTimeInformation(nextFile)
            if tempStart > lastMs:
                # clip's last frame's timestamp falls between the previous and current file ... trim to the last frame of the previous
                break
            lastFileStop = tempStop
            self._lastFile = nextFile

        # Walk back until we reach our start time or we break the file chain.
        while firstFileStart > firstMs:
            prevFile = self._clipManager.getPrevFile(self._firstFile)
            if not prevFile:
                break

            tempStart, tempStop = self._clipManager.getFileTimeInformation(prevFile)
            if tempStop < firstMs:
                break
            firstFileStart = tempStart
            self._firstFile = prevFile

        # A cache of bounding boxes for objects active in the video
        self._bboxCache = {}

        # Ensure we aren't requesting times that aren't recorded
        self._firstMs = max(firstFileStart, firstMs)
        self._lastMs = min(lastFileStop, lastMs)

        return self._firstMs, self._lastMs

    ###########################################################
    def _getFrameTiming(self, filePath):
        """(fps, frameCount) for a recorded file, as ClipReader reads them.

        Cached on (path, size, mtime), so a segment the recorder re-stamps in
        place is re-read.  A miss costs a cv2 container open -- ~0.02s, and no
        decode: the reader is opened in 'singleFrame' mode so it never spawns
        the NVDEC pipe.  The same trick _getClipSize uses.

        @param  filePath  Path relative to the video storage directory.
        @return timing    (fps, frameCount), or None if the file can't be read.
        """
        fullPath = os.path.join(self._vidStoragePath, filePath)
        try:
            st = os.stat(fullPath)
            key = (os.path.normcase(fullPath), st.st_size, st.st_mtime)
        except OSError:
            return None

        if key in self._frameTimingCache:
            return self._frameTimingCache[key]

        reader = ClipReader(self._logger.info)
        if reader.open(fullPath, 0, 0, 0, {'singleFrame': 1}):
            timing = reader.getFrameTiming()
        else:
            timing = None
        reader.close()

        if timing is not None and not timing[0]:
            timing = None               # no frame rate, no arithmetic

        if len(self._frameTimingCache) >= _kMaxFrameTimingEntries:
            self._frameTimingCache.clear()
        self._frameTimingCache[key] = timing
        return timing


    ###########################################################
    def _snapBoundToFrame(self, absMs, filePath, direction):
        """Move a clip bound onto a frame that exists, without decoding.

        The bounds openMarkedVideo hands back have to land on real frames, or
        the timeline and any duration derived from them are wrong.  That used to
        mean decoding a frame at each bound, which on an NVDEC camera respawned
        the ffmpeg pipe twice per clip open -- several seconds, on the UI thread,
        for two numbers that are pure arithmetic on the frame rate.

        Deliberately uses ClipReader's own frameMsAt() rather than the exact
        presentation times in the MP4 sample table.  The sample table is more
        accurate, but seek() and _ClipFrame.ms run on the uniform fps ladder, and
        getCurFrameOffset() subtracts _firstMs from a frame's ms -- so a bound
        from a different time base makes a clip's offset non-zero, occasionally
        negative, at its own start point.

        @param  absMs      The bound, in absolute ms.
        @param  filePath   The file the bound falls in.
        @param  direction  'after' for a start bound, 'before' for an end one.
        @return snappedMs  The adjusted bound, or None if we have no usable
                           answer (caller should probe instead).
        """
        timing = self._getFrameTiming(filePath)
        if timing is None:
            return None

        fileStart, _ = self._clipManager.getFileTimeInformation(filePath)
        snapped = fileStart + frameMsAt(absMs - fileStart, timing[0], timing[1])

        # A bound may only ever move INWARD.  frameMsAt clamps to the file's
        # frame range, which is inward at one end and OUTWARD at the other: a
        # start time past the last frame clamps back onto it, which would pull
        # the clip open earlier than asked.  That happens for real -- clipdb
        # records a file's true last frame time, while the frame count and rate
        # cv2 reports can describe a shorter file (measured 4.1s short on a
        # stalled segment).  Hand those to the probe, which can cross into the
        # neighbouring file; here we would just be guessing.
        if (direction == 'after' and snapped < absMs) or            (direction == 'before' and snapped > absMs):
            return None

        return snapped


    ###########################################################
    def openMarkedVideo(self, cameraLoc, firstMs, lastMs, playMs, objList=[],
                        displaySize=(320, 240), enableAudio=False, asyncRead=True):
        self._firstMs, self._lastMs = \
            self.setupMarkedVideo(cameraLoc, firstMs, lastMs, playMs, objList, displaySize, enableAudio, asyncRead)
        if self._firstMs == -1:
            return -1, -1

        # Sanitize firstMs and lastMs onto times that really exist -- computed
        # from each file's frame rate, not decoded out of it.  These are only
        # bounds, and decoding a frame for each meant a full ClipReader teardown
        # and rebuild (cv2 open, ffmpeg audio probe, NVDEC process spawn)
        # whenever a bound fell in the other file, plus an NVDEC respawn for the
        # long seek even when it did not.
        snappedLast = self._snapBoundToFrame(self._lastMs, self._lastFile,
                                             'before')
        snappedFirst = self._snapBoundToFrame(self._firstMs, self._firstFile,
                                              'after')

        # Open the file playback actually starts in -- and only that one.
        # Nothing downstream depends on the LAST file being the open one:
        # saveCurrentClip re-derives everything from _firstFile/_lastMs, and
        # _loadClip's next move is getFrameAt(playMs).
        if not self._openMarkedFile(self._firstFile, self._objList):
            return -1, -1

        # Fall back to probing for a bound whose frame times we could not read
        # (not an MP4, truncated, or the bound is in a recording gap).  Only
        # trust a probe that actually landed on a frame: on a gap getFrameAt()
        # returns None and leaves _curFrameMs at -1, which would place the bound
        # one ms BEFORE the file start -- and with that on lastMs the returned
        # range comes back inverted (stop < start).
        needReopen = False
        if snappedLast is not None:
            self._lastMs = snappedLast
        else:
            needReopen = True
            if self.getFrameAt(self._lastMs, 'before') is not None:
                self._lastMs = self._curFrameMs + self._fileStart

        if snappedFirst is not None:
            self._firstMs = snappedFirst
        else:
            needReopen = True
            if self.getFrameAt(self._firstMs, 'after') is not None:
                self._firstMs = self._curFrameMs + self._fileStart

        # A probe may have wandered into a neighbouring file; put the reader
        # back where playback starts.  No-op when the file is already open.
        if needReopen and not self._openMarkedFile(self._firstFile,
                                                   self._objList):
            return -1, -1
        self._curFrameMs = -1

        # Never hand back an inverted range.
        self._lastMs = max(self._lastMs, self._firstMs)

        return self._firstMs, self._lastMs


    ###########################################################
    def updateVideoSize(self, resolution):
        """Update the size of retrieved frames in the currently opened video.

        @param  resolution  The desired resolution to begin receiving frames in.
        """
        if self._clipReader:
            self._clipReader.setOutputSize(resolution)
            self._displaySize = resolution


    ###########################################################
    def forceCloseVideo(self):
        """Close any open video files.

        NOTE: This can leave things in an odd state.  Don't call functions
              like getNextFrame before reopening the video.
        """
        if self._clipReader:
            self._clipReader.close()


    ###########################################################
    def getVideoState(self):
        """Return an object describing the current video state.

        @return videoState  An opaque object describing the current state.
        """
        return (self._curVidPath, self._curFrameMs+self._fileStart)


    ###########################################################
    def restoreVideoState(self, videoState):
        """Restore the video state.

        NOTE: If any videos were opened between a call to getVideoState
              and a call to this function, it will not work.

        @param videoState  An object describing the state to restore.
        """
        self._curVidPath = None
        if self._openMarkedFile(videoState[0], self._objList):
            self.getFrameAt(videoState[1])


    ###########################################################
    def saveCurrentClip(self, filePath, desiredFirstMs, desiredLastMs,
            configDir, extras={}):
        """Save the clip definied by the last call to openMarkedVideo.

        @param  filePath       The path where the clip should be stored.
        @param  desiredFirstMs The absolute ms where we'd like to start the
                               video.  May be before the start of the current
                               clip.  If so, we'll try to rewind a bit as long
                               as there is continuous video.
        @param  desiredLastMs  The absolute ms where we'd like to stop; if
                               None, assumes that we'd like to stop at the end
                               of the clip.
        @param  configDir      Directory to search for config files.
        @param  extras         Dict of extras, see _kSaveClipDefaults
        @return success        True if the clip was saved successfully.
        """
        from videoLib2.python.ClipUtils import remuxClip  # Lazy--loaded on first need

        if not self._firstFile or not self._lastFile:
            return False

        drawBoxes = extras.get("drawBoxes", _kSaveClipDefaults["drawBoxes"])
        overlayTimestamp = extras.get("enableTimestamps", _kSaveClipDefaults["enableTimestamps"])
        firstFileStart, _ = self._clipManager.getFileTimeInformation(
                                                            self._firstFile)

        if desiredLastMs is None:
            desiredLastMs = self._lastMs

        if desiredFirstMs < firstFileStart:
            # Move back one file at a time, breaking out of the loop if we run
            # out of continuous clips (in which case we'll just start from
            # the earliest)...
            curFile = self._firstFile
            while True:
                # Move to previous if it's there...
                prevFile = self._clipManager.getPrevFile(curFile)
                if not prevFile:
                    break
                curFile = prevFile

                # If we've found our answer, break out too
                start, stop = self._clipManager.getFileTimeInformation(curFile)
                if desiredFirstMs >= start:
                    assert desiredFirstMs <= stop
                    break
        else:
            if desiredFirstMs > self._lastMs:
                # Requested clip start is past the last recorded frame -- e.g.
                # the triggering event fell in a recording gap (09_Jungle has
                # had these).  There's no video to export.  Fail gracefully: an
                # assert here crashes into ResponseRunner._processClip's bare
                # `except`, which flags the export for retry and then loops on it
                # every ~60s forever, flooding Response.log.
                self._logger.warning(
                    "saveCurrentClip: requested start %d is past last recorded "
                    "ms %d (recording gap?); nothing to save" %
                    (desiredFirstMs, self._lastMs))
                return False
            curFile = self._clipManager.getFileAt(self._curVidCameraLoc,
                                                  desiredFirstMs,
                                                  self._lastMs - desiredFirstMs,
                                                  'after')
            if not curFile:
                # Same rationale as above -- give up cleanly, don't assert-crash
                # into an infinite retry.
                self._logger.warning(
                    "saveCurrentClip: no clip file found for start time %d" %
                    desiredFirstMs)
                return False

        boxOverlay = []
        if drawBoxes:
            boxOverlay = self._getBoundingBoxes(curFile, self._objList, desiredFirstMs-10, desiredLastMs+10)
        extras['boxList'] = boxOverlay
        extras['enableTimestamps'] = overlayTimestamp


        # Build the list of filenames and gaps from the first
        # file to the last.
        start, stop = self._clipManager.getFileTimeInformation(curFile)
        fileList = [(os.path.join(self._vidStoragePath, curFile), start)]
        while stop < desiredLastMs:
            curFile = self._clipManager.getNextFile(curFile)
            if not curFile:
                break
            start, stop = self._clipManager.getFileTimeInformation(curFile)
            fileList.append((os.path.join(self._vidStoragePath, curFile),
                             start))

        return remuxClip(fileList, filePath, desiredFirstMs, desiredLastMs,
                           configDir, extras, self._logger.getCLogFn())>=0

    ###########################################################
    def getBoundingBoxesBetweenTimes(self, camLoc, firstMs, lastMs, procSize, format="videoLib"):
        ''' Get all bounding boxes between times.
        '''
        backupFilter = self._cameraFilter
        try:
            self.setCameraFilter([camLoc])
            objs = self.getObjectsBetweenTimes(firstMs, lastMs, True)
            if format == "json":
                filename = self._clipManager.getFileAt(camLoc, firstMs, lastMs-firstMs, 'after' )
                result = self._getBoundingBoxesJSON(filename, objs, firstMs, lastMs)
            else:
                result = self._getBoundingBoxes(None, objs, firstMs, lastMs, procSize)
            return result
        finally:
            # Restore the FRAGMENT, don't re-derive it.  setCameraFilter takes
            # a list of camera names; handing it the saved fragment made it
            # join that string character by character, leaving
            # camLoc in ("c", "a", "m", ...) -- which matches nothing, for
            # every later search on this instance.
            self._cameraFilter = backupFilter
            self._setFilterStr()


    ###########################################################
    def _getBoundingBoxes(self, filename, objList, firstMs, lastMs,
                        procSize=None):
        procW, procH = self._figureOutProcSize2(filename) if procSize is None else procSize
        if procW == 0 or procH == 0:
            self._logger.warning( "Couldn't get bounding boxes: procW=" + str(procW) + " procH=" + str(procH) )
            return []


        if not objList:
            return []

        # Resolve every colour first, then fetch every box in ONE query.  This
        # used to issue two queries per object (a type lookup plus a per-object
        # motion query) on every clip open, which is on the UI thread.
        bareIds = [obj for obj in objList if not isinstance(obj, tuple)]
        typeById = self.getObjectTypes(bareIds) if bareIds else {}

        colorById = {}
        objIds = []
        for obj in objList:
            if isinstance(obj, tuple):
                objId = obj[0]
                colorById[objId] = self._getLabelColorForType(obj[3])
            else:
                objId = obj
                colorById[objId] = self._getLabelColorForType(typeById[objId])
            objIds.append(objId)

        boxesById = {}
        for row in self.getObjectBboxesBetweenTimes(objIds, firstMs, lastMs):
            boxesById.setdefault(row[6], []).append(row)

        boxOverlay = []
        for objId in objIds:
            labelColor = colorById[objId]
            for x1, y1, x2, y2, _, frameTime, uid in boxesById.get(objId, ()):
                boxOverlay.append([frameTime, "drawbox=%d:%d:%d:%d:%d:%d:%d:%s:t=0" %
                        (x1, y1, x2-x1, y2-y1, procW, procH, uid, labelColor)])
        boxOverlay.sort()
        return boxOverlay

    ###########################################################
    def _getBoundingBoxesJSON(self, filename, objList, firstMs, lastMs):
        """ Return list of objects between times as json of shape
            { "uid": uid,label": label,
              "boxes": [ { "time":time, "x":x, "y":y, "w":w, "h":h },
                         ...
                       ]
                      },
              ...
            }
        """
        inW, inH = self._getClipSize(filename)
        procW, procH = self._figureOutProcSize(filename, (inW, inH))
        if procW == 0 or procH == 0:
            self._logger.warning( "Couldn't get bounding boxes: procW=" + str(procW) + " procH=" + str(procH) )
            return []

        objects = []
        for obj in objList:
            object = {}
            id = obj[0]
            object["id"] = id
            object["label"] = obj[3]
            boxList = []

            bboxes = self.getObjectBboxesBetweenTimes([id], firstMs, lastMs)

            if bboxes:
                wRatio = inW/float(procW)
                hRatio = inH/float(procH)
                for x1, y1, x2, y2, _, frameTime, uid in bboxes:
                    box = {}
                    box["time"] = frameTime
                    box["x"] = int(x1*wRatio)
                    box["y"] = int(y1*hRatio)
                    box["h"] = int((x2-x1)*wRatio)
                    box["w"] = int((y2-y1)*hRatio)
                    boxList.append(box)
                object["boxes"] = boxList
            objects.append( object )
        return objects

    ###########################################################
    def _getRegionZones(self, filename):
        procW, procH = self._figureOutProcSize2(filename)
        if procW == 0 or procH == 0:
            print("Couldn't get region boxes: procW=" + str(procW) + " procH=" + str(procH))
            return []

        zonesOverlay = []
        for triggerObj in self._videoDebugLines:

            points = triggerObj.getPoints((procW, procH))

            numPts = len(points)
            for i in range(0, numPts):
                (x1, y1), (x2, y2) = points[i], points[(i+1)%numPts]
                # format the lines as we would boxes ... they are still defined by two points
                zonesOverlay.append([-1, "drawbox=%d:%d:%d:%d:%d:%d:%d:%s:t=0" %
                        (x1, y1, x2, y2, procW, procH, 0, "red")])
        self._logger.debug("Overlay zones: " + str(zonesOverlay))
        return zonesOverlay


    ###########################################################
    def _openMarkedFile(self, filePath, objList):
        """Open a file with optional object borders

        @param  filePath  The path of the file to open.
        @return success   True if the open was successful.
        """
        if not filePath:
            return False
        # Open this file if it isn't already open
        if filePath != self._curVidPath or self._curFileAudioEnabled != self._audioEnabled:
            fullPath = os.path.join(self._vidStoragePath, filePath)
            if not os.path.exists(fullPath):
                self._logger.error("Failed to open %s" % (ensureUtf8(fullPath)))
                return False

            self._curFrameMs = -1
            self._curVidPath = filePath
            self._curFileAudioEnabled = self._audioEnabled
            # Release the previous reader (stops its audio, frees the capture).
            if self._clipReader is not None:
                self._clipReader.close()
            self._clipReader = ClipReader(self._logger.info)

            self._fileStart, self._fileStop = \
                self._clipManager.getFileTimeInformation(filePath)

            outW = self._displaySize[ 0 ]
            outH = self._displaySize[ 1 ]

            extras = {}
            if self._markupModel.getShowBoxesAroundObjects():
                boxOverlay = self._getBoundingBoxes(self._curVidPath, objList, self._fileStart-10, self._fileStop+10)
                extras ['boxList'] = boxOverlay
            if self._markupModel.getShowRegionZones():
                zonesOverlay = self._getRegionZones(self._curVidPath)
                if zonesOverlay is not None:
                    extras ['zonesList'] = zonesOverlay

            if self._markupModel.getPlayAudio():
                extras ['enableAudio'] = self._audioEnabled
                extras ['audioMute'] = self._muted
            extras ['asyncRead'] = 1 if self._asyncReadEnabled else 0
            extras ['enableTimestamps'] = self._markupModel.getOverlayTimestamp()
            extras ['useUSDate'] = self._markupModel.getUSDate()
            extras ['use12HrTime'] = self._markupModel.get12HrTime()
            extras ['enableDebug'] = 1 if self._markupModel.getShowObjIds() else 0
            extras ['keyframeOnly'] = 1 if self._markupModel.getKeyframeOnlyPlayback() else 0

            openedOk = self._clipReader.open( fullPath, outW, outH, self._fileStart, extras )
            if openedOk:
                # ClipReader's logFn is a native (ctypes) callback, so it logs
                # its decode-path choice through us instead (GPU/NVDEC vs cv2).
                self._logger.info("Playback decode: %s" %
                                  self._clipReader.decodeInfo())
            if openedOk and self._audioEnabled:
                self._clipReader.setMute( self._muted )
                # If we rolled into this file mid-playback (unmuted), start its
                # audio from the top so it continues across the clip boundary.
                if not self._muted:
                    self._clipReader.playAudioFrom(0)

            if not openedOk:
                self._logger.error("Can't open: '%s'" % (filePath))
                return False
            return openedOk

        # Already open: that's success, not failure.  This used to fall off the
        # end returning None, so every caller read "the file you asked for is
        # ready" as "open failed" -- it just rarely came up, because callers
        # normally ask for a file they don't have open.
        return True

    ###########################################################
    def stopAudio(self):
        """Silence the current clip NOW, whatever the mute state.

        setMute() is a no-op when the mute flag already matches, so it can't be
        used to guarantee silence -- leaving the search view with audio playing
        left the clip audible over whatever the user switched to.  This is the
        unconditional stop; playback resumes from the current position when the
        clip is next played.
        """
        if self._clipReader is not None:
            self._clipReader.pauseAudio()


    ###########################################################
    def refreshAudio(self):
        """Re-assert audio for the position we are now at.

        Opening a new file restarts audio by itself, but moving to another
        segment INSIDE the same file reopens nothing -- so once the caller has
        silenced the outgoing segment, only this puts the sound back.  Honours
        the current mute state, which is where the panel's policy (audio pref,
        playing, 1x speed) already lives.
        """
        if not self._audioEnabled or self._clipReader is None:
            return
        if self._muted:
            self._clipReader.setMute(True)
        else:
            self._clipReader.playAudioFrom(max(0, self._curFrameMs))


    ###########################################################
    def setMute(self, mute):
        if mute == self._muted:
            return
        self._muted = mute
        if not self._audioEnabled:
            return
        if self._clipReader is not None:
            self._clipReader.setMute( self._muted )
            # Unmute (re)starts audio from the current play position; mute already
            # stopped it via setMute() above.
            if not self._muted:
                self._clipReader.playAudioFrom(self._curFrameMs)


    ###########################################################
    def setVideoDebugLines(self, triggerLines):
        """Set lines to be overlayed on the video when marking is enabled.

        @param  triggerLines  A list of TriggerLineSegment or TriggerRegion
                              objects, which can be used to retrieve a list of
                              (x1,y1,x2,y2) tuples defining lines to display on
                              the screen by calling their getPoints(coordSpace)
                              instance method.
        """
        self._videoDebugLines = triggerLines


    ###########################################################
    def getFrameAt(self, frameTime, gapDirection='any'):
        """Retrieve the frame corresponding to the given time

        If no recording covers frameTime at all -- it lands in a gap, which
        happens whenever the camera reconnected or the recorder cycled -- we
        return the nearest real frame instead of failing, and the caller reads
        the position it actually got back from getCurFrameOffset().  Callers
        that need a bound to move one way only (clip start / clip end) say so
        with gapDirection; None restores the old fail-on-gap behavior.

        @param  frameTime     The absolute time of the frame to retrieve
        @param  gapDirection  Which way to look for the nearest real frame when
                              frameTime falls in a recording gap: 'any'
                              (nearest, default), 'after', 'before', or None to
                              not cross gaps at all.
        @return img           A PIL/raw image of the requested frame
        """
        afterCurFile = frameTime > self._fileStop
        beforeCurFile = frameTime < self._fileStart

        if afterCurFile or beforeCurFile:
            direction = 'after' if afterCurFile else 'before'
            newFile = self._clipManager.getFileAt(self._curVidCameraLoc,
                                                  frameTime,
                                                  3000,
                                                  direction)
            if not newFile and gapDirection:
                # Nothing adjacent within the rollover tolerance: frameTime is
                # inside a recording gap, so look for the closest real frame.
                newFile = self._findFileAcrossGap(frameTime, gapDirection)
            if not newFile or not self._openMarkedFile(newFile, self._objList):
                return None

            # Handlle cases where the desired frame time falls between physical files
            # When that happens, take the next closest frame
            if self._fileStart > frameTime:
                frameTime = self._fileStart
            elif frameTime > self._fileStop:
                frameTime = self._fileStop

        frame = self._clipReader.seek(frameTime-self._fileStart)

        return self._setCurrentFrame(frame)


    ###########################################################
    def _findFileAcrossGap(self, frameTime, gapDirection='any'):
        """Find the file holding the nearest real frame to a time with no video.

        Deliberately bounded to the clip that openMarkedVideo() set up: without
        that, a time in a gap could pull in footage from hours away (the next
        file after an overnight outage), which is never what the caller wants.

        @param  frameTime     Absolute ms that no recording covers.
        @param  gapDirection  'any' (nearest), 'after' or 'before'.
        @return filename      The file to open, or None if the clip has no
                              usable video on the requested side of the gap.
        """
        directions = ('after', 'before') if gapDirection == 'any' \
                     else (gapDirection,)

        bestFile = None
        bestDist = None
        for direction in directions:
            newFile = self._clipManager.getFileAt(self._curVidCameraLoc,
                                                  frameTime, None, direction)
            if not newFile:
                continue

            fileStart, fileStop = \
                    self._clipManager.getFileTimeInformation(newFile)
            if fileStop < self._firstMs or fileStart > self._lastMs:
                # Outside the clip we opened -- don't wander.
                continue

            dist = (fileStart - frameTime) if direction == 'after' \
                   else (frameTime - fileStop)
            if bestDist is None or dist < bestDist:
                bestFile, bestDist = newFile, dist

        return bestFile


    ###########################################################
    def getNativeFrameAt(self, frameTime):
        """Decode ONE frame at the clip's FULL source resolution.

        Playback decodes at the display size (capped for real-time smoothness),
        so zooming into a playback frame only magnifies those pixels.  This
        re-grabs the same moment at native resolution through a short-lived
        second reader, so a zoomed-in paused frame shows real detail.

        The playback reader is deliberately left alone -- its position, audio
        and GPU pipe are untouched, so resuming playback is unaffected.  Boxes
        and zones are drawn by the usual _markFrame, which scales them from the
        analysis size to whatever this frame's size is, so overlays stay aligned.

        @param  frameTime  Absolute ms of the frame to fetch.
        @return frame      Full-resolution marked-up frame, or None if it isn't
                           available (caller should fall back to getFrameAt).
        """
        if not self._curVidPath:
            return None
        # Restricted to the file that's already open: this serves the PAUSED
        # frame, which is by definition inside it, and keeps us off the
        # file-switching path (which would disturb playback state).
        if frameTime < self._fileStart or frameTime > self._fileStop:
            return None

        reader = None
        try:
            fullPath = os.path.join(self._vidStoragePath, self._curVidPath)
            if not os.path.exists(fullPath):
                return None
            reader = ClipReader(self._logger.info)
            # (0, 0) = native size; 'nativeStill' keeps it off the GPU path,
            # whose output is capped to the playback pixel budget.  No audio:
            # this is a still grab, and the playback reader owns the audio.
            if not reader.open(fullPath, 0, 0, self._fileStart,
                               {'nativeStill': True}):
                return None
            frame = reader.seek(frameTime - self._fileStart)
            if frame is None:
                return None
            # NOTE: intentionally not _setCurrentFrame -- that would overwrite
            # the playback position with this reader's rounding.  The frame owns
            # its pixel buffer, so it stays valid after the reader closes.
            self._markFrame(frame)
            return frame
        except Exception:
            self._logger.warning("Native frame grab failed", exc_info=True)
            return None
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass


    ###########################################################
    def getNextFrame(self):
        """Retrieve the next frame.

        @return frame  A PIL image of the next frame, or None if no next
        """
        # If we're already past the end of the clip return None.
        if self._curFrameMs+self._fileStart > self._lastMs:
            return None

        nextClipReaderFrame = self._clipReader.getNextFrame()

        nextFrame = self._setCurrentFrame(nextClipReaderFrame)

        if self._curFrameMs+self._fileStart > self._lastMs:
            # If we're past the end now also return None
            return None

        if not nextClipReaderFrame:
            # If next frame was None try to open the next file.
            nextFile = self._clipManager.getNextFile(self._curVidPath)
            if not nextFile or not self._openMarkedFile(nextFile, self._objList):
                return None
            return self.getNextFrame()

        # Return the marked-up frame
        return nextFrame


    ###########################################################
    def getPrevFrame(self):
        """Retrieve the previous frame from the currently opened video

        @return frame  A PIL image of the requested frame, or None if no prev
        """
        if self._curFrameMs != -1 and \
           (self._curFrameMs + self._fileStart < self._firstMs):
            # If we're already before the beginning return None.
            return None

        prevClipReaderFrame = self._clipReader.getPrevFrame()

        prevFrame = self._setCurrentFrame(prevClipReaderFrame)
        if self._curFrameMs + self._fileStart < self._firstMs:
            # If we're before the beginning now, also return None.
            return None

        if not prevFrame:
            # if prevFrame was None try to open the previous file.
            prevFile = self._clipManager.getPrevFile(self._curVidPath)
            if not prevFile or not self._openMarkedFile(prevFile, self._objList):
                return None
            return self.getFrameAt(self._fileStop)

        return prevFrame


    ###########################################################
    def getFirstFrame(self):
        """Retrieve the first frame from the currently opened video

        @return frame  A PIL image of the requested frame
        """
        return self.getFrameAt(self._firstMs)


    ###########################################################
    def getLastFrame(self):
        """Retrieve the last frame from the currently opened video

        @return frame  A PIL image of the requested frame
        """
        return self.getFrameAt(self._lastMs)


    ###########################################################
    def _minimalFrameData( self, frame ):
        if not frame:
            self._curFrameMs = -1
        else:
            self._curFrameMs = frame.ms


    ###########################################################
    def _setCurrentFrame(self, frame):
        """Mark up frame with object and debug information.

        @param  frame        The frame to mark up.
        @param  procSize     The size of the image that processing happened on.
        @return markedFrame  A PIL image of the requested frame, or None.
        """
        if not frame:
            self._curFrameMs = -1
            return None

        self._curFrameMs = frame.ms
        self._markFrame(frame)
        return frame


    ###########################################################
    def _scaleBox(self, x1, y1, x2, y2, sx, sy, ox=0, oy=0):
        """Scale a box from analysis space to image space, never inverted.

        The -1 turns an exclusive far edge into an inclusive one, which is
        right up until the box scales down far enough that both edges round to
        the same pixel -- a narrow object in a 64px-wide search-results preview
        does.  PIL then raises "x1 must be greater than or equal to x0", and in
        _markThumb that aborts the whole thumbnail: getThumb swallows the
        exception and returns None, so the results list falls back to decoding
        a full frame for that row (~1s on a 4K camera) instead of using the
        cached JPEG it already had.

        @param  x1, y1, x2, y2  The box in analysis space; far edge exclusive.
        @param  sx, sy          Horizontal and vertical scale factors.
        @param  ox, oy          Origin of the visible area, in image space --
                                non-zero when drawing onto a cropped image (the
                                zoomed search preview).  A box outside the crop
                                lands outside the image and PIL clips it.
        @return box             (x0, y0, x1, y1) inclusive, at least one pixel
                                each way.
        """
        x0, y0 = round(x1 * sx - ox), round(y1 * sy - oy)
        return (x0, y0,
                max(x0, round(x2 * sx - ox) - 1),
                max(y0, round(y2 * sy - oy) - 1))


    ###########################################################
    def _markFrame(self, frame):
        """Draw display-only markup onto a decoded playback frame.

        Mirrors the static thumbnail markup (_markThumb) but is time-synced to
        this frame, so bounding boxes, per-type colors, and region zones appear
        during playback exactly as they do on the preview.  Controlled live by
        the View-menu toggles in self._markupModel (Show Boxes Around Objects /
        Show Different Color Boxes / Show Region Zones), so changes take effect
        on the next frame.

        The overlay is drawn on the in-memory frame ONLY; the stored/exported
        clip is never modified.

        @param  frame  A ClipReader frame (already validated non-None).
        """
        showBoxes = self._markupModel.getShowBoxesAroundObjects()
        showZones = self._markupModel.getShowRegionZones()
        if not (showBoxes or showZones):
            return

        procW, procH = self._figureOutProcSize2(self._curVidPath)
        if procW == 0 or procH == 0:
            return

        sx = frame.width / float(procW)
        sy = frame.height / float(procH)

        # Draw directly on the frame's live RGB ndarray with cv2 (in place, so
        # frame.buffer stays valid).  This replaces a per-frame PIL round-trip
        # (asPil + updateFromPil = two full-frame conversions, ~13-17ms/frame)
        # that was tipping high-res clips over their frame budget and making
        # Search playback stutter.  Colors are RGB tuples because the buffer is
        # RGB; cv2 writes tuple values straight into the R,G,B channels.
        arr = frame.asNumpy()

        # Bounding boxes: one per object, at the sample nearest this frame's
        # time.  Per-object color resolution matches _markThumb (tuple carries
        # the type at [3]; a bare id is looked up), so Show Different Color
        # Boxes is honored via _getLabelColorForType.
        if showBoxes and self._objList:
            absMs = self._fileStart + frame.ms
            objColors = {}
            for obj in self._objList:
                if isinstance(obj, tuple):
                    objColors[obj[0]] = self._rgbForType(obj[3])
                else:
                    objColors[obj] = self._rgbForLabel(obj)

            # One batched query for all objects in this frame's window instead
            # of a SQL round-trip per object per frame.
            allBoxes = self.getObjectBboxesBetweenTimes(
                list(objColors.keys()),
                absMs - _kBoxMatchToleranceMs, absMs + _kBoxMatchToleranceMs)

            # Keep the box nearest this frame's time for each object.
            nearest = {}
            for b in allBoxes:
                oid = b[6]
                if oid not in nearest or \
                   abs(b[5] - absMs) < abs(nearest[oid][5] - absMs):
                    nearest[oid] = b

            for oid, b in nearest.items():
                box = self._scaleBox(b[0], b[1], b[2], b[3], sx, sy)
                cv2.rectangle(arr, box[:2], box[2:],
                              objColors.get(oid, (0, 0, 255)), 1)

        # Region zones: static polygon outlines (mirrors _getRegionZones).
        if showZones and self._videoDebugLines:
            for triggerObj in self._videoDebugLines:
                points = triggerObj.getPoints((procW, procH))
                if len(points) >= 2:
                    pts = np.array([(round(px * sx), round(py * sy))
                                    for (px, py) in points], dtype=np.int32)
                    cv2.polylines(arr, [pts], True, (255, 0, 0), 1)


    ###########################################################
    def getNextFrameOffset(self):
        """Return the offset of the next frame in the video.

        This is relative to the realFirstMs returned by openMarkedVideo().

        @return offset  The offset of the next frame, or -1 if no next frame
        """
        ms = self._clipReader.getNextFrameOffset()
        if -1 == ms:
            nextStart = self._clipManager.getNextFileStartTime(self._curVidPath)
            if nextStart == -1:
                return -1
            return nextStart-self._firstMs

        nextOffset = ms+self._fileStart-self._firstMs

        # If we're past the end of this clip, return -1...
        duration = (self._lastMs - self._firstMs + 1)
        if nextOffset >= duration:
            return -1

        return nextOffset


    ###########################################################
    def getCurFrameOffset(self):
        """Return the offset of the current video frame

        @return offset  The offset of the current frame
        """
        if self._curFrameMs == -1:
            return -1
        return self._curFrameMs+self._fileStart-self._firstMs


    ###########################################################
    def getFileStartMs(self):
        """Return the current start of the current file.

        @return fileStartMs  The start of the current file, in absolute ms.
        """
        return self._fileStart


    ###########################################################
    def getCurFilename(self):
        """Return the name of the currently open file

        @return filename  The name of the currently open file
        """
        return os.path.split(self._curVidPath)[1]


    ###########################################################
    def getCurClipPath(self):
        """Return the absolute path of the currently open video file.

        Note that a single search result may span several files chained by
        prevFile / nextFile; this is the one holding the current frame.

        @return path  Absolute path of the open file; None if none is open.
        """
        if not self._curVidPath:
            return None
        return os.path.normpath(os.path.join(self._vidStoragePath,
                                             self._curVidPath))


    ###########################################################
    def getSingleFrame(self, camLoc, ms=0, size=(0,240), useTolerance=True,
                       wantProcSize=False, markupObjList=None, tolerance=None):
        """Retrieve a single frame from a video file; NOT marked up.

        @param  camLoc        The camera location to obtain a frame from
        @param  ms            The absolute ms of the frame to retreive.
        @param  useTolerance  If False, we will not allow the clip manager
                              to use a tolerance value when getting the file.
        @param  wantProcSize  If True, we'll return a 3rd value: procSize.
        @param  tolerance     Explicit ms to search either side of ms; overrides
                              useTolerance.  None keeps the old behaviour (the
                              clip manager's own default, or 0).
        @return img           A PIL image of the requested frame; not a copy,
                              so please don't draw on this.
        @return ms            The absolute ms from this frame.
        @return [procSize]    Only returned if wantProcSize is True; is always
                              None if img is None.
        """
        if wantProcSize:
            defaultReturn = (None, ms, None)
        else:
            defaultReturn = (None, ms)

        if tolerance is not None:
            # An explicit window -- the response path widens to the event span
            # when previewMs itself falls in a recording hole, so that a frame
            # from elsewhere in the event is used instead of failing outright.
            fileName = self._clipManager.getFileAt(camLoc, ms, tolerance)
        elif useTolerance:
            fileName = self._clipManager.getFileAt(camLoc, ms)
        else:
            fileName = self._clipManager.getFileAt(camLoc, ms, 0)

        if not fileName:
            return defaultReturn

        filePath = os.path.join(self._vidStoragePath, fileName)
        if not filePath or not os.path.exists(filePath):
            return defaultReturn

        # Requested timestamp may occur between the last frame of the previous file
        # and the first frame of this file ... this makes sure we get a frame in the area.
        fileStart, fileStop = self._clipManager.getFileTimeInformation(fileName)
        ms = min(fileStop, max(fileStart, ms))

        # One frame, then this reader is discarded -- tell ClipReader so it
        # doesn't spawn an NVDEC process it can never amortize.
        extras = {'singleFrame': 1}
        if self._markupModel.getShowBoxesAroundObjects():
            if markupObjList is not None:
                boxOverlay = self._getBoundingBoxes(fileName, markupObjList, ms-100, ms+100)
                extras['boxList'] = boxOverlay


        clipReader = ClipReader(self._logger.info)
        fileStartTime, fileStopTime = \
            self._clipManager.getFileTimeInformation(fileName)
        if int(os.getenv("SV_CLIP_DEBUG", "0")) > 0:
            self._logger.info("Getting a frame from %s starting at %d at ms %d (extras=%s)" % (filePath, fileStartTime, ms, str(extras)) )
        openedOk = clipReader.open(filePath, size[0], size[1], fileStartTime, extras)
        if not openedOk:
            # No assert here.  Asserts run by default, so one fired BEFORE this
            # return and made it dead code.  A clip that exists but will not
            # open (corrupt header, still being written, a stalled segment)
            # then raised into ResponseRunner's bare except, which re-queues
            # the export and retries every ~60s forever -- the same trap
            # saveCurrentClip carries a comment about.
            self._logger.warning("getSingleFrame: can't open '%s'"
                                 % (filePath,))
            return defaultReturn
        frame = clipReader.seek(ms-fileStartTime)
        if not frame:
            return defaultReturn

        if wantProcSize:
            procSize = self._figureOutProcSize(fileName,
                                               clipReader.getInputSize())
            return frame.asPil(), \
                   min((frame.ms + fileStartTime), fileStopTime), \
                   procSize
        else:
            return frame.asPil(), \
                   min((frame.ms + fileStartTime), fileStopTime)

    ###########################################################
    def makeThumbnail(self, camLoc, ms, outputFile, maxSize=(0, 0)):
        """Retrieve a thumbnail for the camera at the given time.

        @param  camLoc      The camera location to use.
        @param  ms          The absolute ms of the thumbnail frame.
        @param  outputFile  The file to save the thumbnail to.
        @param  maxSize     The maximum size of the create image.
        @return True if thumbnail was created, False on error.
        """
        # TODO: may need to check the requested size, but for now
        #       pre-created thumbs should work for all existing clients
        thumbFile, _ = self.getThumbFileFromCache(camLoc, ms)
        if thumbFile is not None:
            shutil.copy(thumbFile, outputFile)
            return True

        # Find the file path.
        fileName = self._clipManager.getFileAt(camLoc, ms)
        if not fileName:
            # smart logging: since this happens quite often only warn if we know
            # if that file should already exist and isn't being moved from
            # temp ...
            if time.time() > ms/1000 + 20*60:
                self._logger.warning("file for %d@%s not found" % (ms, camLoc))
            return False

        fullPath = os.path.join(self._vidStoragePath, fileName)
        if not fullPath or not os.path.exists(fullPath):
            self._logger.error("no output path for %d@%s" % (ms, camLoc))
            return False

        # Encoder is *terrible* at tiny resolution, and we need to force a min
        # anyway to guard against nonsensical values like 5, 5
        thumbWidth, thumbHeight = maxSize
        if thumbWidth  > 0: thumbWidth  = max(thumbWidth, 160)
        if thumbHeight > 0: thumbHeight = max(thumbHeight, 120)

        # We need to fetch an image from the file to retrieve the actual ms
        # and file resolution.
        clipReader = ClipReader(self._logger.info)
        openedOk = clipReader.open(fullPath, thumbWidth, thumbHeight, 0,
                                   {'singleFrame': 1})
        if not openedOk:
            # See getSingleFrame: the assert that used to be here made this
            # return unreachable.
            self._logger.error("cannot open clip for %d@%s (%s)"
                               % (ms, camLoc, fullPath))
            return False

        fileStartTime, _ = self._clipManager.getFileTimeInformation(fileName)

        frame = clipReader.seek(ms-fileStartTime)
        if not frame:
            self._logger.warning("no frame available for %d@%s" % (ms, camLoc))
            return False

        frame.asPil().save(outputFile, "JPEG")
        return True

    ###########################################################
    def saveEventSnapshot(self, camLoc, ms, img, snapshotPath='', snapshotSubfolder=''):
        """Save an annotated event image to the configured snapshot folder.

        The image is placed in a dated hierarchy under snapshotPath:
            <snapshotPath>/<yyyy>/<mm>/<yyyy-mm-dd>/[<subfolder>/]<filename>.jpg

        EXIF DateTimeOriginal and DateTimeDigitized are embedded using the
        local wall-clock time derived from ms.

        @param  camLoc            The camera location.
        @param  ms                The absolute timestamp in ms.
        @param  img               A PIL Image to save.
        @param  snapshotPath      Base output directory; must be non-empty.
        @param  snapshotSubfolder Optional named subfolder inside the dated dir.
        @return path              The saved file path, or None on failure.
        """
        if not snapshotPath:
            # Silent until 2026-08-21.  Two 09_WestTerrace rules failed every
            # snapshot for a whole day with nothing in the log but the caller's
            # "failed to write snapshot", because an unconfigured folder returns
            # here and looks exactly like a disk error.  Say which it is.
            self._logger.error("no snapshot folder configured for %s; the "
                               "snapshot at %d was dropped" % (camLoc, ms))
            return None
        tmpPath = None
        claimPath = None
        filePath = None
        outDir = None
        try:
            lt = time.localtime(ms / 1000.0)
            dateStr = time.strftime('%Y-%m-%d', lt)
            yearStr = time.strftime('%Y', lt)
            monStr  = time.strftime('%m', lt)
            timeStr = time.strftime('%H%M%S', lt)

            outDir = os.path.join(snapshotPath, yearStr, monStr, dateStr)
            sub = snapshotSubfolder.strip().strip('/\\')
            if sub:
                outDir = os.path.join(outDir, sub)

            # exist_ok: two threads reaching a brand-new dated folder together
            # both saw it missing and both called makedirs; the loser raised
            # FileExistsError and lost its snapshot.
            os.makedirs(outDir, exist_ok=True)

            # The camera MUST be in the name.  Until 2026-08-19 the filename
            # was date+HHMMSS only -- camLoc was a parameter and went unused --
            # so any two cameras whose previewMs landed in the same second
            # resolved to one path.  Measured over 5 days of Response.log:
            # 8061 writes over 6836 distinct paths, 145 of them written by more
            # than one CAMERA and 231 by more than one rule.  The loser was
            # silently destroyed while both rules logged "completed
            # successfully", and concurrent writes to the same path also
            # produced truncated/black JPEGs (09_Jungle 2026-08-18-191722.jpg
            # came out pure black from a clip with no dark frame in it).
            safeCam = ''.join(
                c if (c.isalnum() or c in '._-') else '_'
                for c in (camLoc or 'cam'))
            # Two RULES on the same camera in the same second collide
            # (AfterHours_Jungle and deletemetest both wrote 062539), so suffix
            # rather than overwrite -- an event is evidence.  Claiming the name
            # has to be ATOMIC: the old exists()-then-use loop let two threads
            # pick the SAME name, and since they also shared one '.tmp' path,
            # the second os.replace failed on a source the first had already
            # moved.  Measured 2026-08-21 with the real function: 4 threads on
            # one camera-second produced 0 successes and ZERO files on disk.
            # The claim is a sidecar rather than the .jpg itself so that no
            # reader ever sees an empty .jpg where an image is about to be.
            lastClaimErr = None
            for n in range(1, 100):
                if n == 1:
                    cand = os.path.join(
                        outDir, '%s-%s-%s.jpg' % (dateStr, timeStr, safeCam))
                else:
                    cand = os.path.join(
                        outDir, '%s-%s-%s-%d.jpg' % (dateStr, timeStr, safeCam, n))
                if os.path.exists(cand):
                    continue
                try:
                    fd = os.open(cand + '.claim',
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                except PermissionError as e:
                    # Windows only: a .claim that another writer is deleting
                    # right now sits in delete-pending state and cannot be
                    # opened at all -- the error is EACCES, not EEXIST.  The
                    # slot is in use either way, so take the next index instead
                    # of failing the snapshot.  Measured before this clause: 1
                    # lost snapshot per ~48 simultaneous writers on one
                    # camera-second.
                    lastClaimErr = e
                    continue
                os.close(fd)
                if os.path.exists(cand):
                    # The claim is released only AFTER a writer's os.replace,
                    # so a name can be completed between our exists() test and
                    # our claim.  Re-test now that we hold it: from here on
                    # nobody else can create this file, so an empty slot stays
                    # empty and a taken one is taken for good.  (Without this
                    # re-test, 16 threads on one camera-second reported 16
                    # successes but left 15 files -- one silently overwritten.)
                    try:
                        os.remove(cand + '.claim')
                    except Exception:
                        pass
                    continue
                filePath = cand
                claimPath = cand + '.claim'
                break
            if filePath is None:
                raise IOError("no free snapshot name for %s at %s (last claim "
                              "error: %s)" % (safeCam, timeStr, lastClaimErr))

            exif = img.getexif()
            dtExif = time.strftime('%Y:%m:%d %H:%M:%S', lt)
            # DateTimeOriginal/DateTimeDigitized live in the Exif sub-IFD
            # (0x8769), NOT IFD0 -- writing them at the top level leaves them
            # unreadable to standard readers (Windows "Date taken", ExifTool).
            exif[306] = dtExif                # DateTime (IFD0)
            exifIfd = exif.get_ifd(0x8769)
            exifIfd[36867] = dtExif           # DateTimeOriginal
            exifIfd[36868] = dtExif           # DateTimeDigitized
            # Write to a temp file in the SAME directory, then os.replace onto
            # the target.  A direct img.save() is not atomic: a reader (or a
            # second writer) could see a half-written file, which is how the
            # black and "Corrupt JPEG data ... extraneous bytes" images
            # appeared.  Same directory matters -- os.replace is only atomic
            # within a volume.
            # The temp name must be unique per WRITER, not per target: two
            # threads sharing 'X.jpg.tmp' interleave their bytes and then race
            # to move it.  The atomic claim above already guarantees no two
            # writers share a target, and this guarantees no two share a temp.
            tmpPath = '%s.%d-%d.tmp' % (filePath, os.getpid(),
                                        threading.get_ident())
            img.save(tmpPath, "JPEG", exif=exif.tobytes())
            os.replace(tmpPath, filePath)
            tmpPath = None
            return filePath
        except Exception:
            # Never silent again: this swallowed the reason for a whole day of
            # "failed to write snapshot" on 09_WestTerrace.
            self._logger.error(
                "failed to save snapshot for %s at %d into %s: %s" %
                (camLoc, ms, filePath or outDir or snapshotPath,
                 traceback.format_exc()))
            try:
                if tmpPath and os.path.exists(tmpPath):
                    os.remove(tmpPath)
            except Exception:
                pass
            return None
        finally:
            # Release the claim whether or not the write worked; a stale one
            # would only cost the next writer one suffix index, but leaving
            # them around would accumulate.
            try:
                if claimPath and os.path.exists(claimPath):
                    os.remove(claimPath)
            except Exception:
                pass

    ###########################################################
    def _getClipSize(self, filename):
        if self._clipReader is not None:
            clipSize = self._clipReader.getInputSize()
        else:
            clipReader = ClipReader(self._logger.info)
            fullPath = os.path.join(self._vidStoragePath, filename)
            if not os.path.exists(fullPath) or \
               not clipReader.open( fullPath, 0, 0, 0, {'singleFrame': 1} ):
                clipSize = (0,0)
            else:
                clipSize = clipReader.getInputSize()
            clipReader = None
        return clipSize

    ###########################################################
    def _figureOutProcSize2(self, filename):
        """ Same as _figureOutProcSize, except attempting to determine
            clipSize from currently active clipReader
        """
        return self._figureOutProcSize(filename, self._getClipSize(filename))

    ###########################################################
    def _figureOutProcSize(self, fileName, inputSize):
        """Figure out what size the given fileName was processed at.

        Normally the clip manager holds this, but for old data it might
        not have it.  In that case, we guess using the input file size.

        @param  fileName   The filename of the clip.
        @param  inputSize  The size of the input video.
        @return procSize   The size the video was (probably) processed at.
        """
        procSize = self._clipManager.getProcSize(fileName)
        if procSize == (0, 0):
            # If we're here, we're looking at video recorded before 1.0
            # release.  In that video, processing and recording always
            # happened at the same size, so just use the input size.
            procWidth, procHeight = inputSize

            # OK, I lied.  The above statement is almost true, except if
            # procWidth / procHeight is big.  In that case, it means that we're
            # looking at video recorded in high resolution of one of the lucky
            # beta testers of 1.0.  Due to the way the code worked, if one of
            # the saved file dimensions is > (320, 240), it means we tried to
            # record at 640x480.  Processing size should be roughly half that,
            # and always divisible by 8.  This might be off by a pixel, but
            # it's as close as we'll get (sorry beta testers!)
            #
            # NOTE: Internal users (not using official build 5543) might have
            # video recorded with slightly different math.  Tough luck.
            #
            # I think the only case that the math really matters is with non-
            # standard camera resolutions.  If the camera gave us 640x480,
            # everyone should be good.
            if (procWidth > 320) or (procHeight > 240):
                procHeight = int(procHeight / 2) & (~7)
                procWidth  = int(procWidth / 2) & (~7)

            return (procWidth, procHeight)
        else:
            return procSize

    ###########################################################
    def _thumbDebug(self, msg):
        self._logger.debug(msg)

    ###########################################################
    def _getLabelColor(self, obj):
        label = self.getObjectType(obj)
        return self._getLabelColorForType(label)

    ###########################################################
    def _getLabelColorForType(self, label):
        if self._markupModel.getShowDifferentColorBoxes():
            labelColor = {"person":"yellow", "vehicle":"orange", "animal":"pink"}.get(label, "green")
        else:
            labelColor = "blue"
        return labelColor

    ###########################################################
    def _rgbForType(self, label):
        """RGB tuple for a type label (for cv2 in-place drawing on RGB frames)."""
        return ImageColor.getrgb(self._getLabelColorForType(label))

    ###########################################################
    def _rgbForLabel(self, obj):
        """RGB tuple for a bare object id (for cv2 in-place drawing)."""
        return ImageColor.getrgb(self._getLabelColor(obj))

    ###########################################################
    def _thumbProcSize(self, camLoc, ms):
        """Analysis-frame size the boxes for a thumbnail are expressed in.

        @return procSize  (width, height); height is never 0.
        """
        procW, procH = self._getProcSize(camLoc, ms=ms)
        if procH == 0:
            # Should not happen on any of the modern versions
            procW, procH = 320, 240
        return procW, procH


    ###########################################################
    def _thumbBoxes(self, camLoc, ms, objList):
        """The detection boxes belonging on a cached thumbnail taken at ms.

        Shared by the two things that need to know where the detection is:
        _markThumb draws these, and _detectionCrop frames the preview on the
        biggest of them.  Going through one function is what stops the zoom
        from framing somewhere the box isn't.

        Matching is the same rule playback uses (_markFrame): motion samples
        are sparse, so take the one nearest ms within _kBoxMatchToleranceMs
        rather than demanding an exact hit.  Thumbnails are written at motion
        times so an exact match usually exists, but not always -- and a miss
        used to mean a preview silently drew no box at all.

        @param  camLoc   The camera location.
        @param  ms       Absolute ms the thumbnail was captured at.
        @param  objList  Object ids, or (objId, _, _, typeLabel) tuples.
        @return boxes    [(x1, y1, x2, y2, labelColor), ...] in analysis space,
                         at most one per object.
        """
        colorById = {}
        for obj in objList:
            if isinstance(obj, tuple):
                colorById[obj[0]] = self._getLabelColorForType(obj[3])
            else:
                colorById[obj] = self._getLabelColor(obj)
        if not colorById:
            return []

        # One query for every object, not one per object: a busy clip used to
        # cost a SQL round-trip per object on every preview row.
        nearest = {}
        for b in self.getObjectBboxesBetweenTimes(
                list(colorById.keys()),
                ms - _kBoxMatchToleranceMs, ms + _kBoxMatchToleranceMs):
            objId = b[6]
            if objId not in nearest or \
               abs(b[5] - ms) < abs(nearest[objId][5] - ms):
                nearest[objId] = b

        return [(b[0], b[1], b[2], b[3], colorById[objId])
                for objId, b in nearest.items()]


    ###########################################################
    def _detectionCrop(self, camLoc, ms, objList, srcSize, outSize):
        """Where to crop a thumbnail so the preview frames the detection.

        Backs View > Show Detection Zoomed.  A preview is 64px wide, so an
        object that is 60x120 in the analysis frame draws as about 6x12 --
        enough to see that something happened, not enough to see what.

        Frames the LARGEST detection rather than the union of all of them: two
        objects at opposite corners would union to nearly the whole frame, and
        that is exactly the busy clip worth zooming into.  Boxes for the others
        are still drawn, just clipped to the crop.

        @param  camLoc   The camera location.
        @param  ms       Absolute ms the thumbnail was captured at.
        @param  objList  Objects to frame on; see _thumbBoxes.
        @param  srcSize  (width, height) of the thumbnail we are cropping.
        @param  outSize  (width, height) the crop will be scaled down to.
        @return box      (left, upper, right, lower) for PIL crop(), or None if
                         no detection could be located (caller should fall back
                         to a plain centre crop so every row still matches).
        """
        boxes = self._thumbBoxes(camLoc, ms, objList)
        if not boxes:
            return None

        srcW, srcH = srcSize
        outW, outH = outSize
        if not (srcW and srcH and outW and outH):
            return None

        _, procH = self._thumbProcSize(camLoc, ms)
        scale = srcH / float(procH)

        # Biggest by area, in analysis space (scale is uniform, so the ordering
        # is the same either side of it).
        x1, y1, x2, y2, _ = max(boxes,
                                key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        boxW = max(1.0, (x2 - x1) * scale)
        boxH = max(1.0, (y2 - y1) * scale)
        cx = (x1 + x2) / 2.0 * scale
        cy = (y1 + y2) / 2.0 * scale

        # Give the subject room to sit in, then square up to the output shape
        # by growing the short side -- never by cropping the detection out.
        cropW = boxW * _kZoomContextFactor
        cropH = boxH * _kZoomContextFactor
        aspect = outW / float(outH)
        if cropW / cropH < aspect:
            cropW = cropH * aspect
        else:
            cropH = cropW / aspect

        # Never magnify past 1:1 (there is no detail below one source pixel),
        # and never ask for more than the thumbnail holds.
        cropW = min(srcW, max(cropW, outW))
        cropH = min(srcH, max(cropH, outH))

        # Centre on the detection, then SHIFT back inside the image rather than
        # shrinking, so the crop keeps the size -- and so the shape -- it just
        # worked out.
        left = min(max(cx - cropW / 2.0, 0), srcW - cropW)
        upper = min(max(cy - cropH / 2.0, 0), srcH - cropH)
        return (int(round(left)), int(round(upper)),
                int(round(left + cropW)), int(round(upper + cropH)))


    ###########################################################
    def _centerCrop(self, srcSize, outSize):
        """Largest centred crop of srcSize having outSize's aspect ratio.

        The fallback when Show Detection Zoomed is on but the detection cannot
        be located.  Without it those rows would come out a different shape
        from their neighbours and the list would look ragged.

        @return box  (left, upper, right, lower) for PIL crop().
        """
        srcW, srcH = srcSize
        outW, outH = outSize
        if not (srcW and srcH and outW and outH):
            return (0, 0, srcW, srcH)

        aspect = outW / float(outH)
        cropW, cropH = float(srcW), srcW / aspect
        if cropH > srcH:
            cropH, cropW = float(srcH), srcH * aspect
        left = (srcW - cropW) / 2.0
        upper = (srcH - cropH) / 2.0
        return (int(round(left)), int(round(upper)),
                int(round(left + cropW)), int(round(upper + cropH)))


    ###########################################################
    def _markThumb(self, camLoc, thumb, ms, objList, srcCrop=None,
                   srcSize=None):
        """Draw the detection boxes onto a cached thumbnail.

        @param  camLoc   The camera location.
        @param  thumb    PIL Image to draw on, in place.
        @param  ms       Absolute ms the thumbnail was captured at.
        @param  objList  Objects to draw; see _thumbBoxes.
        @param  srcCrop  The region of the original thumbnail `thumb` now
                         shows, as (left, upper, right, lower), or None if it
                         is the whole thing.  Set when zoomed.
        @param  srcSize  Size of the original thumbnail, needed only alongside
                         srcCrop.
        """
        boxes = self._thumbBoxes(camLoc, ms, objList)
        if not boxes:
            return

        _, procH = self._thumbProcSize(camLoc, ms)

        if srcCrop is None:
            # analysis space -> thumbnail, straight scale.
            scale = thumb.height / float(procH)
            sx = sy = scale
            ox = oy = 0
        else:
            # analysis space -> original thumbnail -> crop -> displayed size.
            left, upper, right, lower = srcCrop
            srcH = srcSize[1]
            zoom = thumb.height / float(max(1, lower - upper))
            sx = sy = (srcH / float(procH)) * zoom
            ox, oy = left * zoom, upper * zoom

        draw = ImageDraw.Draw(thumb)
        for x1, y1, x2, y2, labelColor in boxes:
            draw.rectangle(self._scaleBox(x1, y1, x2, y2, sx, sy, ox, oy),
                           outline=labelColor)

    ###########################################################
    def markBoundingBoxes(self, camLoc, img, ms, objList, windowMs=500):
        """Return a copy of img with boxes drawn around objList's positions.

        Used by the snapshot-save response, whose events-folder JPEGs go
        through getSingleFrame() rather than the getThumb()/_markThumb()
        cached-thumbnail path, so they get no markup otherwise.

        @param  camLoc    The camera location (for proc-size lookup).
        @param  img       PIL Image to mark up; not modified in place.
        @param  ms        The absolute ms the image was captured at.
        @param  objList   Objects to draw -- bare object IDs, or
                          (objId, _, _, typeLabel) tuples.
        @param  windowMs  How far from ms to search for each object's
                          nearest tracked position (a snapshot's preview ms
                          is a range midpoint, so it rarely lands exactly on
                          a tracked frame's timestamp).
        @return marked    A new PIL Image with boxes drawn.
        """
        procW, procH = self._getProcSize(camLoc, ms=ms)
        if procH == 0:
            return img

        marked = img.copy()
        draw = ImageDraw.Draw(marked)
        scale = marked.height / float(procH)

        for obj in objList:
            if isinstance(obj, tuple):
                objId = obj[0]
                labelColor = self._getLabelColorForType(obj[3])
            else:
                objId = obj
                labelColor = self._getLabelColor(obj)

            bboxes = self.getObjectBboxesBetweenTimes(
                [objId], ms - windowMs, ms + windowMs)
            if not bboxes:
                continue

            x1, y1, x2, y2, _, _, _ = min(bboxes, key=lambda b: abs(b[5] - ms))
            draw.rectangle(self._scaleBox(x1, y1, x2, y2, scale, scale),
                           outline=labelColor, width=2)

        return marked

    ###########################################################
    def _populateThumbCache(self, camLoc, timeIndex):
        dirname = os.path.join(self._vidStoragePath, camLoc, timeIndex, kThumbsSubfolder)

        resTimes = []
        if os.path.isdir(dirname):
            thumbmask = os.path.join(dirname, "*.jpg")
            files = glob.glob(thumbmask)
            for file in files:
                stem = os.path.splitext(os.path.basename(file))[0]
                try:
                    # Epoch ms -- what the recorder actually writes, and the
                    # name of every thumbnail on disk.  Trying the datetime
                    # form first cost a strptime ValueError per file: measured
                    # 175ms vs 2.4ms over a 5,000-thumb folder.
                    fileMs = int(stem)
                except ValueError:
                    try:
                        # Older builds wrote '2026-06-23-072947.jpg'.
                        fileMs = int(time.mktime(
                            time.strptime(stem, '%Y-%m-%d-%H%M%S')) * 1000)
                    except ValueError:
                        continue
                resTimes.append(fileMs)
            resTimes.sort()
        self._thumbCache[camLoc][timeIndex] = (getTimeAsMs(), resTimes)

    ###########################################################
    def _reduceThumbCache(self, camLoc, timeIndex):
        """ Reduce the cached thumb entries if needed
        """
        _kMaxCachedEntries = 5
        if len(self._thumbCache[camLoc]) < _kMaxCachedEntries:
            return
        diff = 0
        toDelete = None
        for key in self._thumbCache[camLoc]:
            newDiff = _folderDiff(key, timeIndex)
            if newDiff == 0:
                # repopulation of this entry had been requested
                toDelete = key
                break
            if newDiff > diff:
                # remove the entry furthest in time from the current one
                toDelete = key
                diff = newDiff
        del self._thumbCache[camLoc][toDelete]



    ###########################################################
    def _closestValue(self, myList, myNumber):
        """
        Assumes myList is sorted. Returns closest value to myNumber.

        If two numbers are equally close, return the smallest number.
        """
        if len(myList) == 0:
            return None

        pos = bisect_left(myList, myNumber)
        if pos == 0:
            return myList[0]
        if pos == len(myList):
            return myList[-1]
        before = myList[pos - 1]
        after = myList[pos]
        if after - myNumber < myNumber - before:
           return after
        else:
           return before

    ###########################################################
    def getThumbFileFromCache(self, camLoc, ms, tolerance=3000):
        """ Retrieve best thumbnail image, caching file list for the folder
            This prevents repetitive glob operations when retrieving a sequential
            list of thumbs, which is beneficial for slower file systems
        """
        _kMaxCacheAge = 10*60*1000
        _kSafetyTimeBuffer = 10*1000

        try:
            reqMsAsStr=str(ms)
            prevMsAsStr=str(ms-tolerance)
            nextMsAsStr=str(ms+tolerance)

            toCheck = [reqMsAsStr]

            # when we're close to the edge of folder, the closest thumb may be in the prev/next folder
            if _msToFolder(ms) != _msToFolder(ms - tolerance):
                toCheck.append(prevMsAsStr)
            elif _msToFolder(ms) != _msToFolder(ms + tolerance):
                toCheck.append(nextMsAsStr)


            if not camLoc in self._thumbCache:
                self._thumbCache[camLoc] = {}

            closestTime = None
            for msAsStr in toCheck:
                # ms may arrive as a fractional-ms float string (e.g. interpolated
                # frame times like '...806.5'); int() chokes on those, so go via float.
                timeIndex = _msToFolder(int(float(msAsStr)))

                cacheEntry = self._thumbCache[camLoc].get(timeIndex, None)
                # Populate the cache when the entry isn't found
                # ... or when requested time occurs after the time entry was cached or very close to it
                # ... or when the cache is older than a configured age
                if cacheEntry is None or \
                    cacheEntry[0] <= ms + _kSafetyTimeBuffer or \
                    getTimeAsMs() - cacheEntry[0] > _kMaxCacheAge:
                    # need to populate thumb entries for a camera/timeIndex
                    self._reduceThumbCache(camLoc, timeIndex)
                    self._populateThumbCache(camLoc, timeIndex)
                    cacheEntry = self._thumbCache[camLoc][timeIndex]

                if cacheEntry is None:
                    continue

                timesArray = cacheEntry[1]
                closestTimeInFolder = self._closestValue(timesArray, ms)
                if closestTimeInFolder is not None:
                    if closestTime is None or \
                        abs(closestTime-ms) > abs(closestTimeInFolder-ms):
                        closestTime = closestTimeInFolder

            if closestTime is None:
                # No thumbnail found in any candidate folder.
                return None, None

            closestTime = int(closestTime)
            thumbDir = os.path.join(self._vidStoragePath, camLoc, _msToFolder(closestTime), kThumbsSubfolder)
            # Epoch ms first: that is what the recorder writes, so the
            # datetime name below is the fallback, not the other way round.
            filename = os.path.join(thumbDir, str(closestTime) + ".jpg")
            if not os.path.isfile(filename):
                # Fall back to the datetime name older builds wrote.
                filename = os.path.join(
                    thumbDir,
                    time.strftime('%Y-%m-%d-%H%M%S',
                                  time.localtime(closestTime/1000.0)) + ".jpg")

            if os.path.isfile(filename):
                res = filename, closestTime
            else:
                res = None, None
        except:
            self._logger.error(traceback.format_exc())
            res = None, None
        return res

    ###########################################################
    def getThumb(self, camLoc, ms, size, objList, tolerance=3000,
                 zoomObjList=None):
        """Return a cached thumbnail, scaled to size and optionally marked up.

        @param  camLoc       The camera location.
        @param  ms           Absolute ms wanted; the nearest thumbnail is used.
        @param  size         (width, height) to scale to; either may be 0 to
                             derive it from the other.  Both are required to
                             zoom.
        @param  objList      Objects to draw boxes for, or None for no boxes.
        @param  tolerance    Passed to getThumbFileFromCache.
        @param  zoomObjList  Objects to frame the image on (View > Show
                             Detection Zoomed), or None to show the whole
                             frame.  Separate from objList because the two menu
                             toggles are independent -- you can zoom without
                             drawing boxes, and vice versa.
        @return img          A PIL image, or None if there is no usable thumb.
        """
        # Requesting a full-size frame -- thumbs are inherently smaller
        if size == (0,0):
            return None

        try:
            # Figure out the location of our thumbnail
            thumbFile, fileMs = self.getThumbFileFromCache(camLoc, ms, tolerance)

            if thumbFile is not None:
                # Resize and return the thumb if found
                img = Image.open(thumbFile)

                # Do not return pre-created thumb if a requested dimension
                # greater than the actual thumb dimension
                imgSize = img.size
                if imgSize[0] < size[0] or \
                   imgSize[1] < size[1]:
                    return None

                if zoomObjList is not None and size[0] and size[1]:
                    # Crop the FULL-resolution thumbnail, then scale the crop
                    # down -- cropping after the downscale would have nothing
                    # left to magnify, and drawing boxes before it would
                    # magnify the box outlines too.
                    crop = self._detectionCrop(camLoc, fileMs, zoomObjList,
                                               imgSize, size)
                    if crop is None:
                        # Nothing to frame on.  Still crop to the requested
                        # shape, so this row matches the ones around it.
                        crop = self._centerCrop(imgSize, size)
                    img = img.crop(crop)
                    # resize(), not thumbnail(): the crop already has the right
                    # aspect ratio, and this guarantees every row comes out
                    # exactly the same size.
                    img = img.resize(size, Image.Resampling.LANCZOS)

                    if objList is not None:
                        self._markThumb(camLoc, img, fileMs, objList,
                                        crop, imgSize)
                    return img

                if size[0] == 0:
                    size = (10000, size[1])
                elif size[1] == 0:
                    size = (size[0], 10000)
                img.thumbnail(size, Image.Resampling.LANCZOS)

                if objList is not None:
                    self._markThumb(camLoc, img, fileMs, objList)
                return img

            return None
        except:
            self._logger.error(traceback.format_exc())
            return None



    ###########################################################
    def getSingleMarkedFrame(self, camLoc, ms=0, objList=[], size=(320,240),
                             useTolerance=True, tolerance=None,
                             zoomObjList=None):
        """Retrieve a single frame from a video file; marked up.

        @param  camLoc        The camera location to obtain a frame from
        @param  ms            The absolute ms of the frame to retreive.
        @param  objList       A list of the objects to draw bounding boxes for
        @param  useTolerance  If False, we will not allow the clip manager
                              to use a tolerance value when getting the file.
        @param  tolerance     Explicit ms to search either side of ms; see
                              getSingleFrame().
        @param  zoomObjList   Objects to frame the image on; see getThumb().
                              Only honoured on the cached-thumbnail path, which
                              is the one the search results list uses.
        @return frame         A PIL image of the requested frame
        """
        # Don't even attempt to use a cached thumb, if asking for a full-size
        if size != (0,0):
            img = self.getThumb(camLoc, ms, size, objList,
                                zoomObjList=zoomObjList)
            if img is not None:
                return img

        img, _ = self.getSingleFrame(
            camLoc, ms, size, useTolerance, False, objList, tolerance
        )
        return img


    ###########################################################
    def isCameraLocationLive(self, cameraLocation):
        """Determine whether a given camera location is live

        @param  cameraLocation  The name of the camera Location
        @return isLive          True if the location is live
        """
        if cameraLocation == "Live camera":
            return True
        return False


    ###########################################################
    def getImageAt(self, cameraLocation, ms, tolerance, direction='any', size=(0,240)):
        """Retrieve a single frame from a camera location.

        This won't be marked up at all.

        TODO: Figure out when clients should use this vs. getSingleFrame().
        I think this function will give you the closest frame if ms is not
        available, and that's the only difference (?).

        NOTE: This currently doesn't allow you to select the frame size,
              always returning a 320x240 image.  Only used by the
              QueryConstructionView so that's probably ok.

        @param  cameraLocation  The location to retrieve a sample image for.
        @param  ms              The absolute ms desired.
        @param  tolerance       The surrounding time to search before or after
                                if no image was found at ms
                                If None, means infinite tolerance...
        @param  direction       If no image initially matches, the direction in
                                which to seek.  Values are 'any', 'before', or
                                'after'.  If 'any' it will return the closest
                                result.
        @return frame           A PIL image of the live video or None
        @return ms              The ms of the frame.
        """
        if not self._clipManager:
            return (None, ms)

        filename = self._clipManager.getFileAt(cameraLocation, ms, tolerance,
                                               direction)
        if not filename:
            return (None, ms)

        fileStart, fileStop = self._clipManager.getFileTimeInformation(filename)
        ms = min(fileStop, max(fileStart, ms))

        return self.getSingleFrame(cameraLocation, ms, size)

    ###########################################################
    def getProcSize(self, cameraLocation):
        """ Legacy version of _getProcSize, without specifying ms
        """
        return self._getProcSize(cameraLocation)

    ###########################################################
    def _getProcSize(self, cameraLocation, ms=None):
        """Retrieve the resolution size that the camera at cameraLocation was
        processed at.

        @param  cameraLocation  The location to retrieve the processing size for.
        @return procSize        Resolution size that the camera at cameraLocation
                                was processed at. This will be (0,0) if the
                                data manager is unable to retrieve the processing
                                size of the given camera location.
        """

        # Initial processing size. We set this to (0,0) in case we can't get
        # the information requested.
        procSize = (0, 0)

        if not self._clipManager:
            return procSize

        if ms is None:
            ms = int(time.time()*1000)

        # see if we can get all procSizes in one sweep and cache them
        procSizes = self._procSizeCache.get(cameraLocation, None)
        if procSizes is None or ms > procSizes[len(procSizes)-1][3]:
            procSizes = self._clipManager.getUniqueProcSizesBetweenTimes(cameraLocation, None, int(time.time()*1000))
            if procSizes and len(procSizes) > 0:
                self._procSizeCache[cameraLocation] = procSizes
            else:
                procSizes = None

        # use cache, if possible
        if procSizes is not None:
            for w, h, start, end in procSizes:
                if (start is None or start <= ms) and end >= ms:
                    return (w, h)

        # Get the filename of the most recent clip saved in the database.
        filename = self._clipManager.getFileAt(cameraLocation, ms, None, 'before')

        if not filename:
            return procSize

        return self._figureOutProcSize(filename, procSize)


    ###########################################################
    def getUniqueProcSizesBetweenTimes(self, camLoc, startTime=None, endTime=None):
        """Retrieve a list of sizes the camera was processed at between the given times.

        @param  camLoc     The desired camera location.
        @param  startTime  The time to begin the search, None for the beginning
        @param  endTime    The time to stop the search, None for most recent
        @return results    A list of sizes the camera was processed at for
                           certain ranges of time. Contains a list of 4-tuples
                           of (procWidth, procHeight, firstMs, lastMs).
                           Note: if the list contains only one 4-tuple, then
                           procWidth and procHeight is unique, and firstMs and
                           lastMs should be ignored; they may hold None values.
                           If the list contains more than one 4-tuple, then
                           procWidth and procHeight are not unique, and you must
                           use the firstMs and lastMs to determine which
                           procSize was used for a specified period of time.
        """
        if not self._clipManager:
            return []

        return self._clipManager.getUniqueProcSizesBetweenTimes(
            camLoc, startTime, endTime
        )