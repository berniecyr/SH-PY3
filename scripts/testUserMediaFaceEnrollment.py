"""Face enrollment regression checks. Uses temporary data and a fake detector.

Run: python scripts/testUserMediaFaceEnrollment.py
Requires numpy and Pillow; no camera, model, running backend or wx installation.
"""
import ast
import base64
import importlib.util
import io
import logging
import os
from pathlib import Path
import pickle
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
# During preparation, unchanged imports come from the source checkout.
REPO = Path(os.environ.get('IMAGECHECK_TEST_SOURCE', str(ROOT)))
sys.path.insert(0, str(REPO))
import backEnd
backEnd.__path__.insert(0, str(ROOT / 'backEnd'))
from backEnd import UserMediaFaceEnrollment as media
from backEnd import FaceEnrollment as baseline


def face(score=.95, bbox=(2, 2, 32, 32), embedding=None):
    return types.SimpleNamespace(det_score=score, bbox=bbox,
        embedding=np.array([1., 0., 0.]) if embedding is None else embedding)


class FakeDetector:
    def __init__(self, logger=None):
        self.crops = []
        self.closed = False
    def ping(self):
        return {'face': True}
    def face(self, crop):
        self.crops.append(crop.copy())
        return [face()]
    def close(self):
        self.closed = True


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.photo = self.folder / 'person é.png'
        self.frame = np.zeros((80, 120, 3), dtype=np.uint8)
        self.frame[:, 60:, 0] = 255
        Image.fromarray(self.frame).save(self.photo)
        self.db = self.folder / 'usermedia' / 'usermedia.db'
        self.db.parent.mkdir()
        con = sqlite3.connect(self.db)
        con.executescript('''CREATE TABLE files (uid, path, kind, size, mtime, analyzedMs);
            CREATE TABLE detections (uid, fileUid, type, faceDetConf, atMs, x1, y1, x2, y2);
        ''')
        stat = self.photo.stat()
        con.execute('INSERT INTO files VALUES (1, ?, "image", ?, ?, 1234567890123)',
                    (str(self.photo), stat.st_size, int(stat.st_mtime)))
        con.execute('INSERT INTO detections VALUES (7, 1, "person", .95, 2500, .5, 0, 1, 1)')
        con.commit()
        con.close()

    def resolve(self, **kwargs):
        args = dict(dbPath=self.db, path=str(self.photo), detectionId=7,
                    analyzedMs='1234567890123')
        args.update(kwargs)
        return media.resolveDetection(**args)

    def test_readonly_resolution_and_stale_guards(self):
        before = self.db.read_bytes()
        self.assertEqual(self.resolve()['atMs'], 2500)
        for kwargs in ({'detectionId': 8}, {'path': 'other.png'}, {'analyzedMs': '0'}):
            with self.assertRaises(ValueError):
                self.resolve(**kwargs)
        self.assertEqual(before, self.db.read_bytes())
        self.photo.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.resolve()

    def test_missing_database_is_not_created(self):
        missing = self.folder / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError):
            self.resolve(dbPath=missing)
        self.assertFalse(missing.exists())

    def test_selected_person_crop_and_jpeg(self):
        detector = FakeDetector()
        result = media.candidatesFromFrame(self.frame, (.5, 0, 1, 1), detector, .8, 20)
        self.assertTrue(result['ok'])
        self.assertEqual(detector.crops[0].shape, (80, 60, 3))
        self.assertTrue(np.all(detector.crops[0][:, :, 0] == 255))
        candidate = result['candidates'][0]
        with Image.open(io.BytesIO(candidate['jpeg'])) as image:
            self.assertEqual(image.mode, 'RGB')
        self.assertAlmostEqual(np.linalg.norm(candidate['embedding']), 1.)

    def test_quality_and_bad_box_rejections(self):
        detector = FakeDetector()
        for faces in ([face(score=.2)], [face(bbox=(0, 0, 2, 2))],
                      [face(embedding=np.zeros(3))], [face(embedding=np.array([np.nan]))], []):
            detector.face = lambda crop, faces=faces: faces
            result = media.candidatesFromFrame(self.frame, (0, 0, 1, 1), detector, .8, 20)
            self.assertIn('error', result)
        for box in ((.5, 0, .2, 1), (0, 0, float('nan'), 1), (-1, 0, 1, 1)):
            self.assertIn('error', media.candidatesFromFrame(self.frame, box, detector, .8, 20))

    def test_overlapping_faces_are_preview_candidates(self):
        detector = FakeDetector()
        detector.face = lambda crop: [face(score=.9), face(score=.99)]
        candidates = media.candidatesFromFrame(self.frame, (0, 0, 1, 1), detector, .8, 20)['candidates']
        self.assertEqual([c['det'] for c in candidates], [.99, .9])

    def harvest_context(self, detector):
        modules = {
            'backEnd.DetectionServiceClient': types.SimpleNamespace(
                DetectionServiceClient=lambda logger: detector),
            'backEnd.UserMediaAnalysis': types.SimpleNamespace(
                _readImageRgb=lambda path: np.asarray(Image.open(path).convert('RGB'))),
        }
        return patch.dict(sys.modules, modules)

    def test_photo_harvest_closes_client_and_never_saves(self):
        detector = FakeDetector()
        before = self.photo.read_bytes()
        with self.harvest_context(detector), patch.object(baseline, '_enrollFloors', return_value=(.8, 20)):
            result = media.harvestUserMediaFace(self.db, str(self.photo), 7, '1234567890123', None)
        self.assertTrue(result['ok'])
        self.assertTrue(detector.closed)
        self.assertEqual(self.photo.read_bytes(), before)
        self.assertEqual(set(p.name for p in self.folder.iterdir()), {'person é.png', 'usermedia'})

    def test_video_uses_selected_offset_and_releases_capture(self):
        con = sqlite3.connect(self.db)
        con.execute('UPDATE files SET kind = "video"')
        con.commit()
        con.close()
        cap = types.SimpleNamespace(isOpened=lambda: True,
            set=lambda key, value: setattr(cap, 'position', value),
            read=lambda: (True, self.frame), release=lambda: setattr(cap, 'released', True))
        cv = types.SimpleNamespace(VideoCapture=lambda path: cap, CAP_PROP_POS_MSEC=0,
                                   COLOR_BGR2RGB=1, cvtColor=lambda frame, flag: frame)
        with self.harvest_context(FakeDetector()), patch.dict(sys.modules, {'cv2': cv}), \
                patch.object(baseline, '_enrollFloors', return_value=(.8, 20)):
            self.assertTrue(media.harvestUserMediaFace(self.db, str(self.photo), 7, '1234567890123', None)['ok'])
            self.assertEqual(cap.position, 2500)
            self.assertTrue(cap.released)
            cap.read = lambda: (False, None)
            self.assertIn('error', media.harvestUserMediaFace(self.db, str(self.photo), 7, '1234567890123', None))
            self.assertTrue(cap.released)

    def test_rpc_preview_then_real_commit_in_temporary_baseline(self):
        # Execute the actual RPC methods without starting the server/processes.
        source = ast.parse((ROOT / 'backEnd/NetworkMessageServer.py').read_text(encoding='utf-8'))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'NetworkMessageServer')
        names = {'_faceHarvestStore', '_harvestFaceFromUserMedia', '_commitFaceHarvest'}
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        namespace = {'os': os, 'time': time}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<RPC methods>', 'exec'), namespace)
        server = types.SimpleNamespace(_localDataDir=str(self.folder), _faceHarvests={},
            _faceHarvestLock=threading.Lock(), _logger=logging.getLogger('test'))
        for name in names:
            setattr(server, name, types.MethodType(namespace[name], server))
        target = self.folder / 'Baseline'
        dat = self.folder / 'known_faces.dat'
        with self.harvest_context(FakeDetector()), \
                patch.object(baseline, '_enrollFloors', return_value=(.8, 20)), \
                patch.object(baseline, 'kBaselineDir', str(target)), \
                patch.object(baseline, 'kKnownFacesDat', str(dat)):
            result = server._harvestFaceFromUserMedia(str(self.photo), 7, '1234567890123')
            self.assertTrue(result['ok'], result)
            self.assertNotIn('embedding', result['candidates'][0])
            self.assertTrue(base64.b64decode(result['candidates'][0]['jpegB64']))
            self.assertFalse(dat.exists())
            committed = server._commitFaceHarvest(result['token'], [0], 'Example', '')
            self.assertTrue(committed['ok'], committed)
            self.assertEqual(len(list(target.rglob('*.jpg'))), 1)
            with dat.open('rb') as f:
                saved = pickle.load(f)
            self.assertEqual(saved['names'], ['Example'])
            self.assertIn('error', server._commitFaceHarvest(result['token'], [0], 'Example', ''))


class FrontendFlowTests(unittest.TestCase):
    def test_cancel_and_selection(self):
        dialogs, commits, messages = [], [], []
        class Preview:
            answer = 0
            def __init__(self, *args, **kwargs):
                dialogs.append(kwargs)
            def ShowModal(self): return self.answer
            def getName(self): return 'Example'
            def getGender(self): return 'F'
            def getSelectedIndices(self): return [1]
            def Destroy(self): pass
        wx = types.SimpleNamespace(ID_OK=1, OK=1, ICON_INFORMATION=2, ICON_ERROR=4,
                                    MessageBox=lambda *args: messages.append(args))
        deps = {'wx': wx,
                'frontEnd.BackEndClient': types.SimpleNamespace(BackEndClient=object),
                'frontEnd.EnrollFacePreviewDialog': types.SimpleNamespace(EnrollFacePreviewDialog=Preview)}
        spec = importlib.util.spec_from_file_location('flow', ROOT / 'frontEnd/ImageFaceEnrollment.py')
        flow = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, deps):
            spec.loader.exec_module(flow)
        client = types.SimpleNamespace(
            harvestFaceFromUserMedia=lambda *args: {'ok': True, 'token': 't', 'candidates': [{}, {}]},
            getBaselinePeople=lambda: [{'name': 'Example'}],
            commitFaceHarvest=lambda *args: commits.append(args) or {'ok': True, 'added': 1})
        flow._request = lambda parent, message, op, logger: op(client)
        detection = {'uid': 7, 'faceName': 'Example', 'gender': 'F'}
        self.assertFalse(flow.enrollFace(None, 'photo', detection, 12, None))
        self.assertEqual(commits, [])
        Preview.answer = 1
        self.assertTrue(flow.enrollFace(None, 'photo', detection, 12, None))
        self.assertEqual(commits, [('t', [1], 'Example', 'F')])
        self.assertEqual(dialogs[-1]['prefillName'], 'Example')
        client.harvestFaceFromUserMedia = lambda *args: {'error': 'Unavailable'}
        self.assertFalse(flow.enrollFace(None, 'photo', detection, 12, None))
        self.assertEqual(len(commits), 1)

    def test_face_controls_clear_with_selection(self):
        tree = ast.parse((ROOT / 'frontEnd/ImageDetailPanel.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_setFaceDetections')
        namespace = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<detail method>', 'exec'), namespace)
        class Control:
            def Set(self, labels): self.labels = labels
            def SetSelection(self, selection): self.selection = selection
            def Show(self, visible): self.visible = visible
            def Enable(self, enabled): self.enabled = enabled
        panel = types.SimpleNamespace(_isVideo=True, _faceChoice=Control(), _addFaceButton=Control())
        update = types.MethodType(namespace['_setFaceDetections'], panel)
        update([{'type': 'person', 'faceDetConf': .9, 'atMs': 2500},
                {'type': 'person', 'faceDetConf': None}], {'analyzedMs': 12})
        self.assertEqual(len(panel._faceRows), 1)
        self.assertIn('00:00:02', panel._faceChoice.labels[0])
        self.assertTrue(panel._addFaceButton.enabled)
        update([], None)
        self.assertFalse(panel._addFaceButton.visible)
        self.assertEqual(panel._faceRows, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
