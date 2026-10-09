#! /usr/local/bin/python

#*****************************************************************************
#
# testUserMediaAnalysis.py
#     Regression checks for the Image view's analysis core and database.
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

"""
## @file
Regression checks for UserMediaAnalysis / UserMediaDb / KnownFaces.

    venv\\Scripts\\python.exe scripts\\testUserMediaAnalysis.py

Runs with **no back end and no models**: the DetectionService is replaced by a
stub that returns scripted boxes.  That is the point -- these checks are about
the code around the detectors (label mapping, box normalisation, the attribute
gate, schema upgrades, what counts as stale), all of which can break silently
while a live run still "works" because a person is still found.

A real end-to-end run needs the app up; see the Image view's Analyze button.
"""

# Python imports...
import os
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Common 3rd-party imports...
import numpy as np

# Local imports...
from backEnd import KnownFaces
from backEnd import UserMediaDb
from backEnd import UserMediaAnalysis as UMA


_failures = []


##############################################################################
def check(label, condition, detail=""):
    """Record one assertion.

    @param  label      What is being checked.
    @param  condition  Truthy for pass.
    @param  detail     Extra text shown on the line.
    """
    print("  %-52s %s %s" % (label, "ok" if condition else "FAIL", detail))
    if not condition:
        _failures.append(label)


##############################################################################
class _StubClient(object):
    """Stands in for DetectionServiceClient, returning scripted results."""

    def __init__(self, dets=None, faces=None, nudity=None, caps=None):
        self.caps = caps if caps is not None else {"face": True,
                                                   "nudity": True}
        self.dets = dets or []
        self.faces = faces or []
        self.nudeResults = nudity or []
        self.yoloCalls = 0
        self.faceCalls = 0
        self.nudityCalls = 0
        self.lastFaceCropShape = None

    def ping(self):
        return self.caps

    def yolo(self, imgRgb, conf):
        self.yoloCalls += 1
        assert isinstance(imgRgb, np.ndarray), "service rejects non-ndarray"
        return self.dets

    def face(self, imgRgb):
        self.faceCalls += 1
        self.lastFaceCropShape = imgRgb.shape
        return self.faces

    def nudity(self, imgBgr):
        self.nudityCalls += 1
        return self.nudeResults


##############################################################################
def testLabelMapping():
    """COCO labels land in the right category, with real boxes."""
    print("\nlabel mapping and box normalisation:")
    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    client = _StubClient(dets=[
        ("person", 0.9, 10, 20, 110, 180),
        ("dog", 0.8, 200, 50, 300, 150),
        ("car", 0.7, 300, 10, 400, 90),
        ("toaster", 0.95, 0, 0, 10, 10),      # not a category we map
        ("bicycle", 0.9, 50, 50, 60, 60),     # documented gap: maps to nothing
    ])
    cfg = {"YOLO_CONF_THRESHOLD": 0.25, "PERSON_CONF_FOR_ATTRS": 0.99,
           "RUN_FACE": False, "RUN_NUDITY": False}
    dets = UMA.analyzeFrame(frame, client, cfg)

    byType = {}
    for d in dets:
        byType.setdefault(d["type"], []).append(d)
    check("person found", len(byType.get("person", [])) == 1)
    check("dog -> animal", len(byType.get("animal", [])) == 1)
    check("car -> vehicle", len(byType.get("vehicle", [])) == 1)
    check("unmapped labels dropped, not stored as unknown",
          len(dets) == 3, "%d detections" % len(dets))
    check("subType keeps the COCO label",
          byType["animal"][0]["subType"] == "dog")

    p = byType["person"][0]
    check("box normalised to 0..1",
          abs(p["x1"] - 10 / 400.0) < 1e-6 and abs(p["y2"] - 180 / 200.0) < 1e-6,
          "x1=%.4f y2=%.4f" % (p["x1"], p["y2"]))
    check("boxes are the REAL yolo boxes, not the full frame",
          p["x2"] < 1.0 and p["y1"] > 0.0)


##############################################################################
def testBoxClamping():
    """A box off the edge of the frame is clamped, not stored negative."""
    print("\nbox clamping:")
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    client = _StubClient(dets=[("person", 0.9, -50, -20, 150, 130)])
    cfg = {"YOLO_CONF_THRESHOLD": 0.25, "PERSON_CONF_FOR_ATTRS": 0.99,
           "RUN_FACE": False, "RUN_NUDITY": False}
    d = UMA.analyzeFrame(frame, client, cfg)[0]
    check("stays inside 0..1",
          0.0 <= d["x1"] <= 1.0 and 0.0 <= d["y2"] <= 1.0,
          "(%.2f,%.2f)-(%.2f,%.2f)" % (d["x1"], d["y1"], d["x2"], d["y2"]))
    check("x1 < x2 and y1 < y2", d["x1"] < d["x2"] and d["y1"] < d["y2"])


##############################################################################
def testAttributeGate():
    """Face/nudity run only on a confident person, and only when enabled."""
    print("\nattribute gate:")
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    lowConf = [("person", 0.30, 10, 10, 100, 190)]
    highConf = [("person", 0.90, 10, 10, 100, 190)]
    base = {"YOLO_CONF_THRESHOLD": 0.25, "PERSON_CONF_FOR_ATTRS": 0.50,
            "FACE_DET_CONF": 0.6, "FACEMATCH_CONF": 0.32, "MIN_FACE_SIZE": 20}

    c = _StubClient(dets=lowConf)
    UMA.analyzeFrame(frame, c, dict(base, RUN_FACE=True, RUN_NUDITY=False))
    check("below PERSON_CONF_FOR_ATTRS: no face pass", c.faceCalls == 0)

    c = _StubClient(dets=highConf)
    UMA.analyzeFrame(frame, c, dict(base, RUN_FACE=True, RUN_NUDITY=False))
    check("above it: face pass runs", c.faceCalls == 1)
    check("face pass gets a CROP, not the whole frame",
          c.lastFaceCropShape is not None
          and c.lastFaceCropShape[0] < frame.shape[0],
          str(c.lastFaceCropShape))

    c = _StubClient(dets=highConf)
    UMA.analyzeFrame(frame, c, dict(base, RUN_FACE=False, RUN_NUDITY=False))
    check("RUN_FACE off: no face pass", c.faceCalls == 0)

    c = _StubClient(dets=highConf)
    UMA.analyzeFrame(frame, c, dict(base, RUN_FACE=False, RUN_NUDITY=True))
    check("RUN_NUDITY on: nudity pass runs", c.nudityCalls == 1)


##############################################################################
def testNudityThresholds():
    """Per-class thresholds are honoured, not one global number."""
    print("\nnudity thresholds:")
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    cfg = {"YOLO_CONF_THRESHOLD": 0.25, "PERSON_CONF_FOR_ATTRS": 0.5,
           "RUN_FACE": False, "RUN_NUDITY": True,
           "NUDE_ENABLED": ["FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"],
           "NUDE_THRESHOLDS": {"FEMALE_BREAST_EXPOSED": 0.80,
                               "BUTTOCKS_EXPOSED": 0.20}}
    client = _StubClient(
        dets=[("person", 0.9, 10, 10, 100, 190)],
        nudity=[{"class": "FEMALE_BREAST_EXPOSED", "score": 0.50},
                {"class": "BUTTOCKS_EXPOSED", "score": 0.50},
                {"class": "MALE_GENITALIA_EXPOSED", "score": 0.99}])
    d = UMA.analyzeFrame(frame, client, cfg)[0]
    check("flagged at all", d.get("nudity") is True)
    detail = d.get("nudityDetail", "")
    check("class under ITS threshold excluded",
          "FEMALE_BREAST_EXPOSED" not in detail, detail)
    check("class over ITS threshold included",
          "BUTTOCKS_EXPOSED" in detail, detail)
    check("class not in NUDE_ENABLED excluded",
          "MALE_GENITALIA" not in detail, detail)


##############################################################################
def testKnownFaces():
    """Matching names the right person and respects the threshold."""
    print("\nknown faces:")
    import pickle
    tmp = os.path.join(tempfile.gettempdir(), "test_known_faces.dat")
    alice = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    bob = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    with open(tmp, "wb") as f:
        pickle.dump({"encodings": [alice * 7.0, bob * 3.0],
                     "names": ["Alice (front door)", "Bob"]}, f)

    count = KnownFaces.loadKnownFaces(tmp, force=True)
    check("library loaded", count == 2, "%d identities" % count)

    name, conf = KnownFaces.matchEmbedding(alice * 2.0, 0.32)
    check("names the right person", name == "Alice", repr(name))
    check("similarity is a cosine, so magnitude does not matter",
          conf is not None and abs(conf - 1.0) < 1e-5, str(conf))
    check("display suffix stripped", " (" not in name)

    name, _ = KnownFaces.matchEmbedding(
        np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32), 0.32)
    check("an unknown face is not named", name == "", repr(name))

    name, _ = KnownFaces.matchEmbedding(alice, 0.99999)
    check("threshold is applied", name == "Alice")
    name, _ = KnownFaces.matchEmbedding(alice * 0.5 + bob * 0.5, 0.95)
    check("a borderline match below threshold is refused", name == "")

    best, conf = KnownFaces.matchBest(
        [SimpleNamespace(embedding=np.array([0.0, 0.0, 0.0, 1.0])),
         SimpleNamespace(embedding=bob)], 0.32)
    check("matchBest picks the one that matches", best == "Bob", repr(best))

    KnownFaces.loadKnownFaces("", force=True)
    name, _ = KnownFaces.matchEmbedding(alice, 0.32)
    check("no library: faces stay unnamed rather than erroring", name == "")
    os.remove(tmp)


##############################################################################
def testDatabase():
    """Schema, staleness and round-tripping."""
    print("\ndatabase:")
    tmp = os.path.join(tempfile.gettempdir(), "test_usermedia.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)

    db = UserMediaDb.UserMediaDb().open(tmp)
    check("WAL journal mode", db._conn.execute(
        "PRAGMA journal_mode").fetchone()[0].lower() == "wal")

    fixture = os.path.join(tempfile.gettempdir(), "test_media_fixture.jpg")
    from PIL import Image
    Image.new("RGB", (640, 480), (10, 20, 30)).save(fixture, "JPEG")

    sig = "test-sig-1"
    check("an unseen file needs analysis", db.needsAnalysis(fixture, sig))

    db.saveResult(fixture, {
        "kind": "image", "width": 640, "height": 480, "durationMs": None,
        "captureMs": 1234, "modelSig": sig, "error": None,
        "detections": [
            {"atMs": 0, "type": "person", "subType": "person", "conf": 0.9,
             "x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4,
             "faceName": "Alice", "faceConf": 0.55, "faceDetConf": 0.8,
             "gender": "F", "age": 33, "nudity": False, "nudityDetail": None},
            {"atMs": 0, "type": "vehicle", "subType": "car", "conf": 0.7,
             "x1": 0.5, "y1": 0.5, "x2": 0.9, "y2": 0.9},
        ]})

    check("not stale straight after analysis",
          not db.needsAnalysis(fixture, sig))
    check("stale when the model signature changes",
          db.needsAnalysis(fixture, "test-sig-2"))

    rows = db.getDetections(fixture)
    check("both detections stored", len(rows) == 2, "%d rows" % len(rows))
    person = [r for r in rows if r["type"] == "person"][0]
    check("face name round-trips", person["faceName"] == "Alice")
    check("normalised box round-trips",
          abs(person["x1"] - 0.1) < 1e-6 and abs(person["y2"] - 0.4) < 1e-6)
    check("age round-trips as an int", person["age"] == 33)

    # Re-analysis must REPLACE, or every re-run doubles the rows.
    db.saveResult(fixture, {
        "kind": "image", "width": 640, "height": 480, "durationMs": None,
        "captureMs": 1234, "modelSig": sig, "error": None,
        "faceModelRan": True, "nudityModelRan": False,
        "detections": [{"atMs": 0, "type": "person", "subType": "person",
                        "conf": 0.95, "x1": 0, "y1": 0, "x2": 1, "y2": 1}]})
    fileCount, detCount = db.stats()
    check("re-analysis replaces rather than appends",
          fileCount == 1 and detCount == 1,
          "%d files, %d detections" % (fileCount, detCount))

    # A changed file must be re-analysed.
    time.sleep(1.1)
    Image.new("RGB", (640, 480), (99, 99, 99)).save(fixture, "JPEG")
    check("a file edited on disk goes stale",
          db.needsAnalysis(fixture, sig))

    check("filter finds the file by type",
          db.pathsMatching(os.path.dirname(fixture),
                           types=["person"]) == {fixture})
    check("filter excludes a type that is not there",
          db.pathsMatching(os.path.dirname(fixture),
                           types=["animal"]) == set())
    check("no filter selected means no filtering",
          db.pathsMatching(os.path.dirname(fixture)) is None)

    db.close()

    # Reopening must be a no-op, not a second CREATE that throws.
    db2 = UserMediaDb.UserMediaDb().open(tmp)
    check("reopen is clean", db2.stats()[0] == 1)
    db2.close()

    # Column sniffing: drop a column by rebuilding, and confirm it comes back.
    conn = sqlite3.connect(tmp)
    conn.execute("ALTER TABLE detections DROP COLUMN nudityDetail")
    conn.commit(); conn.close()
    db3 = UserMediaDb.UserMediaDb().open(tmp)
    have = {r[1] for r in db3._conn.execute("PRAGMA table_info(detections)")}
    check("a missing column is re-added on open", "nudityDetail" in have)
    check("new analysis stores the model flags",
          tuple(db3.getFile(fixture)[k] for k in
                ("faceModelRan", "nudityModelRan")) == (1, 0))
    db3.close()

    # Upgrading a database from before the model flags: analysed records are
    # marked as done once; never-analysed records stay NULL.
    conn = sqlite3.connect(tmp)
    conn.execute("ALTER TABLE files DROP COLUMN faceModelRan")
    conn.execute("ALTER TABLE files DROP COLUMN nudityModelRan")
    conn.execute("INSERT INTO files (path) VALUES ('never-analysed.jpg')")
    conn.commit(); conn.close()
    db4 = UserMediaDb.UserMediaDb().open(tmp)
    rows = {r["path"]: (r["faceModelRan"], r["nudityModelRan"]) for r in
            db4._conn.execute("SELECT * FROM files")}
    check("upgrade marks analysed records done", rows[fixture] == (1, 1))
    check("upgrade leaves unanalysed records NULL",
          rows["never-analysed.jpg"] == (None, None))
    check("model flags are advanced-search filters",
          db4.pathsMatching(None, query="nudityModelRan:eq:1",
                            allFolders=True) == {fixture})
    db4.close()

    os.remove(fixture)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)


##############################################################################
def testCaptureTime():
    """EXIF DateTimeOriginal wins over mtime when it is there."""
    print("\ncapture time:")
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), "test_exif.jpg")

    img = Image.new("RGB", (64, 48), (5, 5, 5))
    exif = img.getexif()
    # The sub-IFD, not IFD0 -- the trap DataManager's snapshot writer
    # documents, where a top-level write is unreadable to standard readers.
    exif.get_ifd(0x8769)[36867] = "2019:07:04 11:22:33"
    img.save(tmp, "JPEG", exif=exif.tobytes())

    ms = UMA._captureTimeMs(tmp)
    stamp = time.localtime(ms / 1000.0)
    check("reads DateTimeOriginal from the Exif sub-IFD",
          (stamp.tm_year, stamp.tm_mon, stamp.tm_mday) == (2019, 7, 4),
          time.strftime("%Y-%m-%d %H:%M:%S", stamp))
    check("capture time is NOT the file mtime",
          abs(ms - os.path.getmtime(tmp) * 1000) > 1000)

    plain = os.path.join(tempfile.gettempdir(), "test_noexif.png")
    Image.new("RGB", (8, 8)).save(plain)
    ms2 = UMA._captureTimeMs(plain)
    check("falls back to mtime with no EXIF",
          abs(ms2 - os.path.getmtime(plain) * 1000) < 2000)

    os.remove(tmp); os.remove(plain)


##############################################################################
def testStillEndToEnd():
    """A real file, decoded for real, detectors stubbed."""
    print("\nstill, end to end (stub detector):")
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), "test_still.jpg")
    Image.new("RGB", (800, 600), (40, 60, 80)).save(tmp, "JPEG")

    client = _StubClient(dets=[("person", 0.95, 100, 100, 300, 500)],
                         faces=[SimpleNamespace(det_score=0.9, sex="F",
                                                age=30, bbox=[0, 0, 50, 50],
                                                embedding=np.array([1.0, 0, 0, 0]))])
    cfg = {"YOLO_CONF_THRESHOLD": 0.25, "PERSON_CONF_FOR_ATTRS": 0.5,
           "RUN_FACE": True, "RUN_NUDITY": False, "FACE_DET_CONF": 0.6,
           "FACEMATCH_CONF": 0.32, "MIN_FACE_SIZE": 20,
           "YOLO_MODEL": "yolo26s.pt"}

    KnownFaces.loadKnownFaces("", force=True)
    result = UMA.analyzeStill(tmp, client, cfg)
    check("no error", result["error"] is None, str(result["error"]))
    check("dimensions read from the real file",
          (result["width"], result["height"]) == (800, 600),
          "%sx%s" % (result["width"], result["height"]))
    check("one detection", len(result["detections"]) == 1)
    d = result["detections"][0]
    check("gender and age recorded",
          d.get("gender") == "F" and d.get("age") == 30)
    check("unnamed when no library is loaded", not d.get("faceName"))
    check("modelSig recorded", "yolo26s.pt" in result["modelSig"])
    check("face model flagged as run", result["faceModelRan"] is True)
    check("nudity model flagged as skipped (off in config)",
          result["nudityModelRan"] is False)

    unloaded = _StubClient(dets=client.dets, faces=client.faces,
                           caps={"face": False, "nudity": False})
    result = UMA.analyzeStill(tmp, unloaded, cfg)
    check("face flagged as skipped when the service has it unloaded",
          result["faceModelRan"] is False)

    bad = os.path.join(tempfile.gettempdir(), "test_broken.jpg")
    with open(bad, "wb") as f:
        f.write(b"not a jpeg at all")
    broken = UMA.analyzeStill(bad, client, cfg)
    check("an undecodable file reports an error, not an exception",
          broken["error"] is not None and not broken["detections"],
          repr(broken["error"])[:48])

    os.remove(tmp); os.remove(bad)


##############################################################################
def main():
    """Run everything and exit non-zero on any failure."""
    print("UserMediaAnalysis / UserMediaDb / KnownFaces regression checks")
    print("(no back end, no models -- the detector is a stub)")
    testLabelMapping()
    testBoxClamping()
    testAttributeGate()
    testNudityThresholds()
    testKnownFaces()
    testDatabase()
    testCaptureTime()
    testStillEndToEnd()

    print()
    if _failures:
        print("FAILED (%d): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
