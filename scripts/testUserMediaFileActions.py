"""Rename real temporary files and verify the media DB survives failures."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backEnd.UserMediaDb import UserMediaDb
from frontEnd.ImageFileSort import sortPaths


class FileActions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = self.root / 'one' / 'photo.jpg'
        self.b = self.root / 'two' / 'photo.jpg'
        for path in (self.a, self.b):
            path.parent.mkdir()
            path.write_bytes(b'identical photo bytes')
        self.db = UserMediaDb().open(str(self.root / 'media.db'))
        result = dict(kind='image', modelSig='test', detections=[
            dict(type='person', faceName='Bernie', faceDetConf=.9)])
        self.uid = self.db.saveResult(str(self.a), result)
        self.db.registerContent(str(self.b))
        self.db.saveDescriptions(str(self.a), 'beach; family', 'A day at the beach.')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def assertRecord(self, paths):
        self.assertEqual(self.db.stats(), (1, 1))
        for path in paths:
            self.assertTrue(path.exists())
            self.assertEqual(self.db.getFile(str(path))['uid'], self.uid)
            self.assertEqual(set(self.db.getLocations(str(path))), set(map(str, paths)))
            self.assertEqual(self.db.getDescriptions(str(path))['description_tags'], 'beach; family')
            self.assertEqual(self.db.getDetections(str(path))[0]['faceName'], 'Bernie')
            self.assertFalse(self.db.needsAnalysis(str(path), 'test'))

    def test_single_canonical_copy_then_reopen(self):
        target = self.a.with_name('renamed.jpg')
        self.db.renameFile(str(self.a), target.name)
        self.assertFalse(self.a.exists())
        self.assertIsNone(self.db.getFile(str(self.a)))
        self.db.close()
        self.db.open(str(self.root / 'media.db'))
        self.assertRecord([target, self.b])
        matches = self.db.pathsMatching(str(self.root), query='filename:renamed', recursive=True)
        self.assertIn(str(target), matches)

    def test_single_noncanonical_copy(self):
        target = self.b.with_name('other.jpg')
        self.db.renameFile(str(self.b), target.name)
        self.assertRecord([self.a, target])
        self.assertIsNone(self.db.getFile(str(self.b)))

    def test_all_copies(self):
        self.db.renameFile(str(self.a), 'Family beach.jpg', allCopies=True)
        self.assertRecord([p.with_name('Family beach.jpg') for p in (self.a, self.b)])
        self.assertFalse(self.a.exists())
        self.assertFalse(self.b.exists())

    def test_collision_preflight_does_not_move_any_copy(self):
        target = self.b.with_name('taken.jpg')
        target.write_bytes(b'keep existing file')
        with self.assertRaises(FileExistsError):
            self.db.renameFile(str(self.a), target.name, True)
        self.assertRecord([self.a, self.b])
        self.assertEqual(target.read_bytes(), b'keep existing file')

    def test_second_move_failure_restores_first(self):
        original = os.rename
        def fail_second(source, target):
            if source == str(self.b):
                raise PermissionError('Simulated locked file')
            original(source, target)
        with patch('backEnd.UserMediaDb.os.rename', side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.db.renameFile(str(self.a), 'new.jpg', True)
        self.assertRecord([self.a, self.b])
        self.assertFalse(self.a.with_name('new.jpg').exists())

    def test_database_failure_restores_files(self):
        self.db._conn.execute("""CREATE TRIGGER reject_rename BEFORE UPDATE OF path
            ON file_locations BEGIN SELECT RAISE(ABORT, 'simulated DB failure'); END""")
        self.db._conn.commit()
        with self.assertRaises(Exception):
            self.db.renameFile(str(self.a), 'new.jpg', True)
        self.assertRecord([self.a, self.b])
        self.assertFalse(self.a.with_name('new.jpg').exists())

    def test_changed_duplicate_rejected_before_any_move(self):
        self.b.write_bytes(b'different content')
        with self.assertRaises(ValueError):
            self.db.renameFile(str(self.a), 'new.jpg', True)
        self.assertTrue(self.a.exists())
        self.assertFalse(self.a.with_name('new.jpg').exists())

    def test_missing_duplicate_rejected_before_any_move(self):
        self.b.unlink()
        with self.assertRaises(ValueError):
            self.db.renameFile(str(self.a), 'new.jpg', True)
        self.assertTrue(self.a.exists())

    def test_invalid_names_and_extension_changes(self):
        for name in ('', '../x.jpg', 'CON.jpg', 'x?.jpg', 'x.jpg.', 'x.jpg ', 'x.png'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.db.renameFile(str(self.a), name)
        self.assertRecord([self.a, self.b])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows case-insensitive paths')
    def test_case_only_rename(self):
        target = self.a.with_name('PHOTO.JPG')
        self.db.renameFile(str(self.a), target.name)
        self.assertRecord([target, self.b])
        self.assertIn('PHOTO.JPG', os.listdir(self.a.parent))

    def test_unindexed_file(self):
        source = self.root / 'unindexed.jpg'
        source.write_bytes(b'new')
        self.db.renameFile(str(source), 'new.jpg')
        self.assertTrue((self.root / 'new.jpg').exists())
        self.assertIsNone(self.db.getFile(str(self.root / 'new.jpg')))

    def test_sort_name_uses_basename_not_folder(self):
        paths = [str(self.root / 'a' / 'Z.jpg'), str(self.root / 'z' / 'A.jpg')]
        self.assertEqual(sortPaths(paths), paths[::-1])
        self.assertEqual(sortPaths(paths, 1), paths)

    def test_date_sort_and_missing_files(self):
        os.utime(self.a, (100, 100))
        os.utime(self.b, (200, 200))
        missing = str(self.root / 'missing.jpg')
        paths = [str(self.a), missing, str(self.b)]
        self.assertEqual(sortPaths(paths, 2), [str(self.b), str(self.a), missing])
        self.assertEqual(sortPaths(paths, 3), [str(self.a), str(self.b), missing])


if __name__ == '__main__':
    unittest.main(verbosity=2)
