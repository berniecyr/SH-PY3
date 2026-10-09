"""Snapshot-response images added to the Images tab's database, on temporary data.

    venv\\Scripts\\python.exe scripts\\testUserMediaSnapshotImport.py
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from backEnd import UserMediaSnapshotImport as Snap
from backEnd.DataManager import DataManager
from backEnd.UserMediaDb import UserMediaDb


class _Client(object):
    def ping(self):
        return {"face": True, "nudity": False}

    def close(self):
        pass


class _DataMgr(object):
    """The DataManager calls the snapshot import makes, over scripted objects."""

    def __init__(self):
        self.types = {1: "person", 2: "vehicle", 3: "object"}
        self.attrs = {1: {"faceName": "Alice", "faceConf": 0.6, "faceDetConf": 0.9,
                          "gender": "F", "age": 30, "subType": "person",
                          "detConf": 0.92, "nudity": False, "nudityDetail": None}}

    def _getProcSize(self, camLoc, ms=None):
        return (320, 240)

    def getObjectBoxesNearTime(self, camLoc, ms, windowMs):
        return {1: (32, 24, 160, 240), 2: (200, 100, 320, 200), 3: (0, 0, 10, 10)}

    def getObjectBboxesBetweenTimes(self, objIds, startTime=None, endTime=None):
        return []

    def getObjectTypes(self, objIds):
        return {i: self.types.get(i, "unknown") for i in objIds}

    def getObjectAttributes(self, objIds):
        return {i: self.attrs[i] for i in objIds if i in self.attrs}


class SnapshotImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dbPath = str(self.root / "usermedia.db")
        self.ms = int(time.mktime((2026, 1, 15, 21, 30, 5, 0, 0, -1)) * 1000)
        self.path = str(self.root / "2026-01-15-213005-Gate.jpg")
        self.img = Image.new("RGB", (640, 480))
        exif = self.img.getexif()
        exif.get_ifd(0x8769)[36867] = "2026:01:15 21:30:05"
        self.img.save(self.path, "JPEG", exif=exif.tobytes())
        Snap._db = None

    def tearDown(self):
        if Snap._db is not None:
            Snap._db.close()
            Snap._db = None
        self.temp.cleanup()

    def record(self):
        with patch.object(Snap.UserMediaAnalysis, "openDetectionClient", return_value=_Client()), \
             patch.object(Snap.ImageCheckConfig, "loadConfig",
                          return_value={"RUN_FACE": True, "RUN_NUDITY": True}), \
             patch("backEnd.UserMediaSnapshotImport.UserMediaDb",
                   lambda logger: _OpenAt(self.dbPath)):
            return Snap.recordSnapshot(_DataMgr(), "Gate", self.path, self.ms, self.img, [1])

    def test_snapshot_recorded_with_camera_analysis(self):
        self.assertTrue(self.record())
        db = UserMediaDb().open(self.dbPath)
        row = db.getFile(self.path)
        self.assertEqual((row["kind"], row["width"], row["height"]), ("image", 640, 480))
        self.assertIsNotNone(row["analyzedMs"])
        self.assertEqual((row["faceModelRan"], row["nudityModelRan"]), (1, 0))
        self.assertEqual((row["exifDate"], row["exifTime"]), (20260115, 213005))
        self.assertEqual(row["captureMs"], self.ms)
        dets = db.getDetections(self.path)
        self.assertEqual(sorted(d["type"] for d in dets), ["person", "vehicle"])
        person = [d for d in dets if d["type"] == "person"][0]
        self.assertEqual((person["faceName"], person["age"]), ("Alice", 30))
        self.assertAlmostEqual(person["x1"], 0.1); self.assertAlmostEqual(person["y2"], 1.0)
        self.assertEqual(db.pathsMatching(None, types=["person"], allFolders=True), {self.path})
        db.close()

    def test_recording_again_or_reanalysing_never_duplicates(self):
        self.record(); self.record()
        db = UserMediaDb().open(self.dbPath)
        db.saveResult(self.path, {"kind": "image", "modelSig": "later", "detections": [
            {"type": "person", "subType": "person", "conf": 0.9}]})
        self.assertEqual(db.stats(), (1, 1))
        db.close()

    def test_database_failure_never_breaks_the_snapshot(self):
        with patch.object(Snap.ImageCheckConfig, "loadConfig", side_effect=OSError("disk")):
            self.assertFalse(Snap.recordSnapshot(_DataMgr(), "Gate", self.path, self.ms, self.img, [1]))

    def test_boxes_near_time_query(self):
        dm = DataManager.__new__(DataManager)
        dm._connection = sqlite3.connect(":memory:"); dm._cur = dm._connection.cursor()
        dm._cur.execute("CREATE TABLE objects (uid INTEGER PRIMARY KEY, camLoc TEXT)")
        dm._cur.execute("CREATE TABLE motion (objUid INTEGER, time INTEGER, x1, y1, x2, y2)")
        dm._cur.executemany("INSERT INTO objects VALUES (?,?)", [(1, "Gate"), (2, "Yard")])
        dm._cur.executemany("INSERT INTO motion VALUES (?,?,?,?,?,?)", [
            (1, 900, 0, 0, 1, 1), (1, 1050, 5, 5, 6, 6), (1, 3000, 9, 9, 9, 9),
            (2, 1000, 7, 7, 8, 8)])
        self.assertEqual(dm.getObjectBoxesNearTime("Gate", 1000, 500), {1: (5, 5, 6, 6)})


class _OpenAt(UserMediaDb):
    def __init__(self, path):
        UserMediaDb.__init__(self)
        self._target = path

    def open(self, filePath=None, timeout=15):
        return UserMediaDb.open(self, self._target, timeout)


if __name__ == "__main__":
    unittest.main()
