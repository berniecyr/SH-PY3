#! /usr/local/bin/python

"""
## @file
Add "save a snapshot" response images to the Images tab's database.

The camera pipeline has already found the objects in the frame (and their
faces and nudity), so the snapshot is recorded as analysed with those
results instead of being run through the detectors a second time.  Only the
snapshot response calls this; recorded video and other responses never do.

A snapshot analysed again later from the Images tab replaces this record
(UserMediaDb.saveResult upserts by path), so it is never duplicated.
"""

# Python imports...
import threading

# Local imports...
from backEnd import ImageCheckConfig
from backEnd import UserMediaAnalysis
from backEnd.UserMediaDb import UserMediaDb


# The types the Images tab stores; unclassified motion is left out, as there.
_kStoredTypes = ("person", "vehicle", "animal")

# One connection for the process, opened on first use.  Response actions run
# on several threads, and a UserMediaDb connection must be used by one at a time.
_lock = threading.Lock()
_db = None


##############################################################################
def _objectId(obj):
    """objList holds bare ids or (objId, _, _, typeLabel) tuples."""
    return obj[0] if isinstance(obj, tuple) else obj


##############################################################################
def buildResult(dataMgr, camLoc, path, ms, width, height, objList, cfg,
                running, windowMs=500):
    """The UserMediaDb result for one snapshot, from the camera's detections.

    @param  dataMgr   The response runner's DataManager.
    @param  camLoc    The camera location.
    @param  path      The saved snapshot.
    @param  ms        The ms the snapshot frame shows.
    @param  width     Snapshot width, pixels.
    @param  height    Snapshot height, pixels.
    @param  objList   The rule's objects; every other object of this camera
                      in the frame is included too.
    @param  cfg       The ImageCheckConfig dict.
    @param  running   UserMediaAnalysis.optionalModelsRunning() result.
    @param  windowMs  How far from ms to look for each object's position.
    @return dict      Ready for UserMediaDb.saveResult.
    """
    procW, procH = dataMgr._getProcSize(camLoc, ms=ms)
    boxes = dataMgr.getObjectBoxesNearTime(camLoc, ms, windowMs)
    for obj in objList:
        objId = _objectId(obj)
        if objId not in boxes:
            near = dataMgr.getObjectBboxesBetweenTimes(
                [objId], ms - windowMs, ms + windowMs)
            if near:
                boxes[objId] = min(near, key=lambda b: abs(b[5] - ms))[:4]

    types = dataMgr.getObjectTypes(list(boxes))
    attrs = dataMgr.getObjectAttributes(list(boxes))
    detections = []
    for objId in sorted(boxes):
        objType = types.get(objId)
        if objType not in _kStoredTypes:
            continue
        a = attrs.get(objId, {})
        x1, y1, x2, y2 = boxes[objId]
        det = {"atMs": 0, "type": objType,
               "subType": a.get("subType") or objType,
               "conf": a.get("detConf")}
        # Tracked boxes are in processing-size pixels; stored boxes are 0..1.
        # With no known processing size the box is left out, not zeroed.
        if procW and procH:
            det.update(zip(("x1", "y1", "x2", "y2"), UserMediaAnalysis._clampBox(
                x1, y1, x2, y2, procW, procH)))
        for key in ("faceName", "faceConf", "faceDetConf", "gender", "age",
                    "nudity", "nudityDetail"):
            det[key] = a.get(key)
        detections.append(det)

    taken = UserMediaAnalysis.exifDateTaken(path)
    result = {"kind": "image", "width": width, "height": height,
              "durationMs": None, "captureMs": int(ms),
              "modelSig": UserMediaAnalysis.modelSignature(cfg), "error": None,
              "detections": detections}
    result.update(running)
    result.update(UserMediaAnalysis.exifDateTimeFields(taken))
    return result


##############################################################################
def recordSnapshot(dataMgr, camLoc, path, ms, img, objList, logger=None):
    """Add one saved snapshot to the Images tab's database.

    Never raises: a database problem is logged and the snapshot itself stays
    saved, since the response has already succeeded.

    @param  dataMgr  The response runner's DataManager.
    @param  camLoc   The camera location.
    @param  path     The saved snapshot.
    @param  ms       The ms the snapshot frame shows.
    @param  img      The PIL image that was saved.
    @param  objList  The rule's objects.
    @param  logger   Optional logger.
    @return bool     True if recorded.
    """
    global _db
    try:
        cfg = ImageCheckConfig.loadConfig()
        client = UserMediaAnalysis.openDetectionClient(logger)
        if client is None:
            running = {"faceModelRan": False, "nudityModelRan": False}
        else:
            try:
                running = UserMediaAnalysis.optionalModelsRunning(client, cfg)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        result = buildResult(dataMgr, camLoc, path, ms, img.width,
                             img.height, objList, cfg, running)
        with _lock:
            if _db is None:
                _db = UserMediaDb(logger).open()
            try:
                _db.saveResult(path, result)
            except Exception:
                # saveResult has no rollback of its own; a half-written record
                # would otherwise hold the write lock against the Images tab
                # and be committed by the next snapshot.
                _db._conn.rollback()
                raise
        return True
    except Exception as e:
        if logger is not None:
            logger.warning("could not add snapshot %s to the image database: "
                           "%s" % (path, e))
        return False
