"""Exercise folder imports against a disposable database, without inference."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backEnd.UserMediaDb import UserMediaDb
from backEnd import UserMediaFolderImport as folder


class FolderImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = UserMediaDb().open(str(self.root / 'test.db'))
        self.lock = threading.Lock()
        self.cfg = dict(RUN_FACE=True, RUN_NUDITY=True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def media(self, relative, data=b'photo'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def runImport(self, analyze=False, **kwargs):
        return folder.importFolder(str(self.root), self.db, self.lock,
                                   self.cfg, analyze=analyze, **kwargs)

    def test_recursive_exclusions(self):
        keep = {self.media('a.jpg'), self.media('nested/b.JPG')}
        for name in ('.hidden.jpg', '.cache/c.jpg', 'nested/.cache/d.jpg',
                     'RAW/e.jpg', 'camera_raw_files/f.jpg', 'readme.txt'):
            self.media(name)
        self.assertEqual(set(folder.folderFiles(str(self.root))), keep)
        self.assertEqual(list(folder.folderFiles(str(self.root / 'RAW'))), [])
        self.assertEqual(self.runImport()['registered'], 2)

    def test_filenames_only_links_text_and_preserves_detections(self):
        a = self.media('a.jpg')
        b = self.media('nested/b.jpg')
        self.db.saveResult(a, dict(kind='image', modelSig='old', detections=[dict(type='person')]))
        self.db.saveDescriptions(a, 'Beach; Bernie', 'Existing description')
        with patch.object(folder.UserMediaAnalysis, 'analyzeFile', side_effect=AssertionError('inference')):
            self.runImport()
        self.assertEqual(self.db.getFile(a)['uid'], self.db.getFile(b)['uid'])
        self.assertEqual(self.db.getDescriptions(a), self.db.getDescriptions(b))
        self.assertEqual(len(self.db.getLocations(b)), 2)
        self.assertEqual(len(self.db.getDetections(b)), 1)

    def test_same_filename_different_content_stays_separate(self):
        a = self.media('one/a.jpg', b'first')
        b = self.media('two/a.jpg', b'other')
        self.runImport()
        self.assertNotEqual(self.db.getFile(a)['uid'], self.db.getFile(b)['uid'])
        self.assertIsNone(self.db.getFile(a)['analyzedMs'])

    def test_analysis_reuses_duplicate_and_retries_errors(self):
        self.media('a.jpg')
        self.media('sub/b.jpg')
        result = dict(kind='image', modelSig=folder.UserMediaAnalysis.modelSignature(self.cfg),
                      detections=[], error=None)
        with patch.object(folder.UserMediaAnalysis, 'analyzeFile', return_value=result) as detect:
            counts = self.runImport(True)
            self.assertEqual(detect.call_count, 1)
            self.assertEqual(counts['analyzed'], 1)
            self.assertEqual(counts['reused'], 1)
            self.runImport(True)
            self.assertEqual(detect.call_count, 1)
            path = str(self.root / 'a.jpg')
            self.db.saveResult(path, dict(result, error='Previous failure'))
            self.runImport(True)
            self.assertEqual(detect.call_count, 2)

    def test_cancellation_does_not_save_partial_analysis(self):
        path = self.media('a.mp4')
        cancel = threading.Event()
        def detect(*args, **kwargs):
            cancel.set()
            return dict(kind='video', detections=[])
        with patch.object(folder.UserMediaAnalysis, 'analyzeFile', side_effect=detect):
            self.runImport(True, cancelled=cancel.is_set)
        self.assertIsNone(self.db.getFile(path)['analyzedMs'])

    def test_error_is_counted_and_next_file_runs(self):
        self.media('a.jpg', b'a')
        self.media('b.jpg', b'b')
        with patch.object(folder.UserMediaAnalysis, 'analyzeFile',
                          side_effect=[RuntimeError('bad'), dict(kind='image', detections=[])]):
            counts = self.runImport(True)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['analyzed'], 1)

    def test_optional_model_configuration_is_private(self):
        cfg, missing = folder.availableConfig(self.cfg, dict(face=True, nudity=False))
        self.assertEqual(missing, ['Nudity'])
        self.assertFalse(cfg['RUN_NUDITY'])
        self.assertTrue(self.cfg['RUN_NUDITY'])


if __name__ == '__main__':
    unittest.main()
