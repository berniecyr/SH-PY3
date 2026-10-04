#!/usr/bin/env python

#*****************************************************************************
#
# SnapshotResponse.py
#     Response: save an annotated snapshot image to the events folder
#
#*****************************************************************************

from .BaseResponse import BaseResponse
from backEnd import MessageIds

# Minimum ms between snapshot saves for the same object.  Must exceed the
# recorder's own flush debounce (StreamReader._kRemuxFlushDebounceSecs = 60s)
# -- at the old 10s cadence, a sustained object could ask for a fresh flush
# far more often than the recorder can ever honor one, so most of its repeat
# requests raced a debounce window doomed to drop them (FINDINGS.md item 9).
# Measured 2026-08-21: this repeat-fire pattern is a MINOR contributor to
# item 9's volume in practice (only one object fleet-wide showed >=3 re-fires
# in the retained logs) -- the dominant cause is many DIFFERENT simultaneous
# objects on cluttered cameras sharing that same 60s-debounced flush slot,
# which this constant cannot fix (see tools/FINDINGS.md item 9 write-up).
# Raised anyway: it is free and correct for the one case it does help, and it
# stops this response from ever being the one racing its own recorder.
_kMinRepeatMs = 65000

# Forget a tracked object after it has been absent this long
_kObjectTimeoutMs = 30000

# Do not snapshot sooner than this after object first appears
_kMinTimeSinceStartMs = 2000


###############################################################
class SnapshotResponse(BaseResponse):
    """Saves an annotated JPEG snapshot to the events folder on detection."""

    ###########################################################
    def __init__(self, ruleName, camLoc, msgQueue, responseRunnerQueue, config={}):
        super(SnapshotResponse, self).__init__()
        self._ruleName = ruleName
        self._camLoc = camLoc
        self._msgQueue = msgQueue
        self._responseRunnerQueue = responseRunnerQueue
        self._snapshotPath = config.get('snapshotPath', '')
        self._snapshotSubfolder = config.get('snapshotSubfolder', '')
        self._drawBoundingBox = bool(config.get('drawBoundingBox', False))
        # objId -> (firstMs, lastMs, lastSnapshotMs)
        self._activeObjects = {}

    ###########################################################
    def addRanges(self, ms, rangeDict):
        # Update tracked objects with new range data
        for objId, ranges in rangeDict.items():
            firstMs = ranges[0][0][1]
            lastMs  = ranges[-1][1][1]
            prev = self._activeObjects.get(objId, (firstMs, lastMs, 0))
            self._activeObjects[objId] = (prev[0], lastMs, prev[2])

        # Decide which objects need a snapshot
        toSnapshot = []
        for objId in list(self._activeObjects):
            firstMs, lastMs, lastSnapMs = self._activeObjects[objId]
            elapsed      = ms - firstMs
            sinceLast    = lastMs - lastSnapMs
            sinceLastSeen = ms - lastMs

            if elapsed > _kMinTimeSinceStartMs and sinceLast > _kMinRepeatMs:
                toSnapshot.append((objId, firstMs, lastMs))

            if sinceLastSeen > _kObjectTimeoutMs and lastSnapMs > 0:
                del self._activeObjects[objId]
            else:
                updatedSnap = ms if (elapsed > _kMinTimeSinceStartMs and sinceLast > _kMinRepeatMs) else lastSnapMs
                if objId in self._activeObjects:
                    self._activeObjects[objId] = (firstMs, lastMs, updatedSnap)

        for objId, firstMs, lastMs in toSnapshot:
            self._msgQueue.put([MessageIds.msgIdFlushVideo, self._camLoc])
            self._responseRunnerQueue.put([
                MessageIds.msgIdSaveSnapshot,
                self._ruleName, self._camLoc,
                [objId], firstMs, lastMs, self._snapshotPath,
                self._snapshotSubfolder, self._drawBoundingBox,
            ])

    ###########################################################
    def startNewSession(self):
        self._activeObjects = {}
