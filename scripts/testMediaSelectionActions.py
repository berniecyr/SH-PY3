"""Multi-select in the Images grid and the right-click selection actions,
on temporary files.

    venv\\Scripts\\python.exe scripts\\testMediaSelectionActions.py
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wx
from PIL import Image
from appCommon.InstallPaths import getExifToolExe
from backEnd.UserMediaDb import UserMediaDb
from frontEnd import MediaSelectionActions as Actions
from testImageLargeView import Harness


def photo(path, taken='2026:01:15 21:30:05'):
    img = Image.new('RGB', (64, 48), (10, 80, 160))
    exif = img.getexif()
    exif.get_ifd(0x8769)[36867] = taken
    exif[0x010F] = 'TestCamera'
    img.save(path, 'JPEG', exif=exif.tobytes())


class CopyRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'a').mkdir(); (self.root / 'b').mkdir()
        self.paths = [str(self.root / 'a' / 'img.jpg'), str(self.root / 'b' / 'img.jpg'),
                      str(self.root / 'b' / 'other.jpg')]
        for path in self.paths:
            photo(path)

    def tearDown(self):
        self.temp.cleanup()

    def test_temp_folder_is_new_and_timestamped(self):
        now = time.strptime('2026-10-09 16:19:40', '%Y-%m-%d %H:%M:%S')
        first = Actions.makeTempFolder(str(self.root / 'out'), now)
        second = Actions.makeTempFolder(str(self.root / 'out'), now)
        self.assertEqual(os.path.basename(first), '20261009-161940')
        self.assertEqual(os.path.basename(second), '20261009-161940-2')

    def test_flat_copy_suffixes_clashing_names(self):
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        copies, failed = Actions.copyFiles(self.paths + [str(self.root / 'missing.jpg')], dest)
        self.assertEqual(sorted(os.listdir(dest)), ['img (2).jpg', 'img.jpg', 'other.jpg'])
        self.assertEqual(len(copies), 3)
        self.assertEqual([os.path.basename(p) for p, _ in failed], ['missing.jpg'])

    def test_structured_copy_keeps_drive_and_folders(self):
        self.assertEqual(Actions.structuredPath('C:\\out', 'G:\\Photos\\2026\\a.jpg'),
                         'C:\\out\\G\\Photos\\2026\\a.jpg')
        self.assertEqual(Actions.structuredPath('C:\\out', '\\\\nas\\share\\x\\a.jpg'),
                         'C:\\out\\nas\\share\\x\\a.jpg')
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        copies, failed = Actions.copyFiles(self.paths, dest, keepStructure=True)
        self.assertEqual(failed, [])
        for original, copy in zip(self.paths, copies):
            self.assertEqual(copy, Actions.structuredPath(dest, original))
            self.assertTrue(os.path.isfile(copy))

    def test_cancel_stops_copying(self):
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        copies, _ = Actions.copyFiles(self.paths, dest,
                                      progressFn=lambda done, total, path: done < 1)
        self.assertEqual(len(copies), 1)

    @unittest.skipIf(getExifToolExe() is None, 'ExifTool not installed')
    def test_strip_removes_metadata_from_copies_only(self):
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        copies, _ = Actions.copyFiles(self.paths, dest)
        self.assertEqual(Actions.stripMetadata(copies), [])
        for copy in copies:
            with Image.open(copy) as img:
                self.assertEqual(dict(img.getexif()), {}, copy)
            self.assertFalse(os.path.exists(copy + '_original'))
        for original in self.paths:
            with Image.open(original) as img:
                self.assertEqual(img.getexif().get(0x010F), 'TestCamera', 'original untouched')

    @unittest.skipIf(getExifToolExe() is None, 'ExifTool not installed')
    def test_strip_handles_non_ascii_names(self):
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        names = [os.path.join(dest, n) for n in ('写真.jpg', 'été 📷.jpg', 'Zoë.jpg')]
        for name in names:
            photo(name)
        self.assertEqual(Actions.stripMetadata(names), [])
        for name in names:
            with Image.open(name) as img:
                self.assertEqual(dict(img.getexif()), {}, name)

    @unittest.skipIf(getExifToolExe() is None, 'ExifTool not installed')
    def test_strip_reports_a_file_it_cannot_clean(self):
        dest = Actions.makeTempFolder(str(self.root / 'out'))
        good = os.path.join(dest, 'good.jpg'); photo(good)
        bad = os.path.join(dest, 'bad.jpg')
        Path(bad).write_bytes(b'not really a jpeg')
        failed = Actions.stripMetadata([good, bad])
        self.assertEqual([p for p, _ in failed], [bad])

    def test_batch_failures_are_counted_not_guessed(self):
        batch = ['C:\\x\\a.jpg', 'C:\\x\\b.jpg']
        self.assertEqual(Actions._batchFailures(batch, 0, '    2 image files updated', ''), [])
        self.assertEqual(Actions._batchFailures(
            batch, 0, '    1 image files updated\n    1 image files unchanged', ''), [])
        # A failure naming one file blames only that file.
        self.assertEqual([p for p, _ in Actions._batchFailures(
            batch, 1, '    1 image files updated', 'Error: Not a valid JPG - C:/x/b.jpg')],
            ['C:\\x\\b.jpg'])
        # A failure that names nothing recognisable blames the whole batch.
        self.assertEqual(len(Actions._batchFailures(batch, 1, '', 'Error: something')), 2)
        # So does a summary that does not account for every file.
        self.assertEqual(len(Actions._batchFailures(batch, 0, '    1 image files updated', '')), 2)

    def test_missing_exiftool_is_reported_not_downloaded(self):
        with patch.object(Actions, 'getExifToolExe', return_value=None):
            with self.assertRaises(FileNotFoundError) as caught:
                Actions.stripMetadata(self.paths)
        self.assertIn('exiftool.exe', str(caught.exception))


class GridAndMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = [str(self.root / ('%d.jpg' % i)) for i in range(5)]
        for path in self.paths:
            photo(path)
        self.db = UserMediaDb().open(str(self.root / 'test.db'))
        for path in self.paths:
            self.db.registerContent(path)
        self.frame = wx.Frame(None, size=(1100, 700))
        self.view = Harness(self.frame, self.db, self.root, self.paths)
        self.frame.Show(); self.frame.Layout()
        self.view._applyFilters()
        self.grid = self.view._fileList

    def tearDown(self):
        self.frame.Destroy(); self.db.close(); self.temp.cleanup()

    def click(self, index, ctrl=False, shift=False):
        rect = self.grid._tileRectOnScreen(index)
        event = wx.MouseEvent(wx.wxEVT_LEFT_DOWN)
        event.SetPosition(wx.Point(rect.x + 10, rect.y + 10))
        event.SetControlDown(ctrl); event.SetShiftDown(shift)
        self.grid.OnLeftDown(event)

    def menu(self):
        """(label, enabled) for each item the right-click menu would show."""
        shown = []
        def popup(menu):
            shown.extend((i.GetItemLabel(), i.IsEnabled()) for i in menu.GetMenuItems()
                         if not i.IsSeparator())
        self.grid.PopupMenu = popup
        event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU, self.grid.GetId())
        rect = self.grid._tileRectOnScreen(self.grid.getSelectedIndices()[0])
        event.SetPosition(self.grid.ClientToScreen(wx.Point(rect.x + 10, rect.y + 10)))
        self.view.OnThumbnailContextMenu(event)
        return dict(shown)

    def test_click_ctrl_shift_and_ctrl_a(self):
        self.click(1)
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[1]])
        self.click(3, ctrl=True)
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[1], self.paths[3]])
        self.click(1, ctrl=True)
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[3]])
        self.click(0); self.click(2, shift=True)
        self.assertEqual(self.grid.getSelectedPaths(), self.paths[0:3])
        self.click(4)
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[4]])
        self.grid.selectAll()
        self.assertEqual(self.grid.getSelectedPaths(), self.paths)

    def test_menu_enables_single_file_actions_only_for_one(self):
        self.click(1)
        items = self.menu()
        self.assertTrue(items['Rename file...'] and items['Show in Explorer'])
        self.assertTrue(items['Delete selection'])
        self.click(3, ctrl=True)
        items = self.menu()
        self.assertFalse(items['Rename file...'] or items['Show in Explorer'])
        self.assertTrue(all(enabled for label, enabled in items.items()
                            if label.startswith(('Delete', 'Copy'))))
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[1], self.paths[3]],
                         'right-click inside the selection keeps it')

    def test_right_click_outside_the_selection_selects_just_that_file(self):
        self.click(0); self.click(1, ctrl=True)
        point = self.grid._tileRectOnScreen(4)
        self.grid.selectAtPosition(wx.Point(point.x + 10, point.y + 10))
        self.assertEqual(self.grid.getSelectedPaths(), [self.paths[4]])

    def test_delete_selection(self):
        self.click(1); self.click(3, ctrl=True)
        recycled = []
        def recycle(path, hwnd=None):
            recycled.append(path); os.remove(path)
        with patch('wx.MessageBox', return_value=wx.YES) as box, \
             patch('frontEnd.RecycleBin.recycle', side_effect=recycle):
            self.view._deletePaths(self.grid.getSelectedPaths())
        self.assertIn('these 2 files', box.call_args_list[0][0][0])
        self.assertFalse(box.call_args_list[0][0][2] & wx.NO_DEFAULT)
        self.assertEqual(recycled, [self.paths[1], self.paths[3]])
        self.assertEqual(self.view._files, [self.paths[0], self.paths[2], self.paths[4]])
        self.assertIsNone(self.db.getFile(self.paths[1]))
        self.assertIsNotNone(self.db.getFile(self.paths[0]))

    def test_delete_selection_reports_failures(self):
        self.click(0); self.click(1, ctrl=True)
        def recycle(path, hwnd=None):
            if path == self.paths[1]:
                raise OSError('in use')
            os.remove(path)
        with patch('wx.MessageBox', return_value=wx.YES) as box, \
             patch('frontEnd.RecycleBin.recycle', side_effect=recycle):
            self.view._deletePaths(self.grid.getSelectedPaths())
        self.assertIn('1 file(s) could not be deleted', box.call_args_list[-1][0][0])
        self.assertIn(self.paths[1], self.view._files)
        self.assertNotIn(self.paths[0], self.view._files)

    def test_copy_actions_open_a_new_folder(self):
        self.click(0); self.click(2, ctrl=True)
        opened = []
        out = str(self.root / 'out')
        with patch.object(Actions, 'showInExplorer', side_effect=opened.append), \
             patch.object(Actions, 'makeTempFolder', side_effect=lambda: _folder(out)):
            self.view._copySelection(self.grid.getSelectedPaths())
            self.view._copySelection(self.grid.getSelectedPaths(), keepStructure=True)
        self.assertEqual(len(opened), 2)
        self.assertEqual(sorted(os.listdir(opened[0])), ['0.jpg', '2.jpg'])
        self.assertTrue(os.path.isfile(Actions.structuredPath(opened[1], self.paths[2])))

    def test_strip_action_without_exiftool_explains(self):
        self.click(0)
        with patch('appCommon.InstallPaths.getExifToolExe', return_value=None), \
             patch('wx.MessageBox') as box, \
             patch.object(Actions, 'showInExplorer') as shown:
            self.view._copySelection(self.grid.getSelectedPaths(), stripExif=True)
        self.assertIn('ExifTool', box.call_args[0][0])
        shown.assert_not_called()


_counter = [0]


def _folder(root):
    _counter[0] += 1
    path = os.path.join(root, 'copy%d' % _counter[0])
    os.makedirs(path)
    return path


if __name__ == '__main__':
    unittest.main()
