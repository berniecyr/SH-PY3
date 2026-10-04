"""Verify the relocated default database and explicit database overrides."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backEnd.UserMediaDb import UserMediaDb, getUserMediaDbPath


class DatabasePathTests(unittest.TestCase):
    def test_explicit_service_data_directory(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(Path(getUserMediaDbPath(root)),
                             Path(root) / 'usermedia' / 'usermedia.db')

    def test_default_opens_existing_relocated_database(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'usermedia' / 'usermedia.db'
            db = UserMediaDb().open(str(target))
            db.saveDescriptions(str(Path(root) / 'photo.jpg'), 'existing tag', 'Existing text')
            db.close()
            with patch('appCommon.InstallPaths.getUserDataDir', return_value=root):
                db = UserMediaDb().open()
                try:
                    self.assertEqual(db.getDescriptions(str(Path(root) / 'photo.jpg'))[
                        'description_ai'], 'Existing text')
                    self.assertFalse((Path(root) / 'usermedia.db').exists())
                finally:
                    db.close()

    def test_explicit_preview_database_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'import-preview.db'
            db = UserMediaDb().open(str(target))
            db.close()
            self.assertTrue(target.exists())
            self.assertFalse((Path(root) / 'usermedia').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
