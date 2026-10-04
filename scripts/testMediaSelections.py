"""Snapshot scope, persistence controls and fresh-search defaults through live UI code."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wx
from backEnd.UserMediaDb import UserMediaDb
from frontEnd.AdvancedMediaSearch import AdvancedMediaSearchDialog
from frontEnd.MediaSelections import remapSelections, uniquePaths
from testImageFileActionsUi import Harness


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        folder = self.root / 'local'; folder.mkdir()
        other = self.root / 'other'; other.mkdir()
        self.a, self.b, self.c = folder / 'door.jpg', folder / 'dog.jpg', other / 'copy.jpg'
        for path, content in [(self.a, b'a'), (self.b, b'b'), (self.c, b'a')]: path.write_bytes(content)
        self.db = UserMediaDb().open(str(self.root / 'db.sqlite'))
        for path in (self.a, self.b, self.c): self.db.registerContent(str(path))
        self.db.saveDescriptions(str(self.a), 'door', 'A door outside')
        self.db.saveDescriptions(str(self.b), 'dog', 'A dog outside')
        self.frame = wx.Frame(None, size=(1200, 600))
        self.view = Harness(self.frame, self.db, folder, [self.a, self.b])
        self.frame.Show(); self.frame.Layout()

    def tearDown(self):
        self.view._fileList.stop(); self.frame.Destroy(); self.app.Yield()
        self.db.close(); self.temp.cleanup()

    def test_scope_does_not_add_duplicate_locations(self):
        self.view._selectionScope = [str(self.a)]
        self.view._searchText.ChangeValue('"door"'); self.view._applyFilters()
        self.assertEqual(self.view._files, [str(self.a)])
        self.view._selectionScope = [str(self.c)]
        self.view._applyFilters()
        self.assertEqual(self.view._files, [str(self.c)])

    def test_empty_selection_never_widens(self):
        self.view._selectionScope = []
        self.view._searchText.ChangeValue('outside'); self.view._applyFilters()
        self.assertEqual(self.view._files, [])
        self.view._searchText.ChangeValue(''); self.view._applyFilters()
        self.assertEqual(self.view._files, [])

    def test_load_snapshot_ignores_old_query_filters_and_reports_missing(self):
        self.view._searchText.ChangeValue('no matching text')
        next(iter(self.view._filterChecks.values())).SetValue(True)
        self.view._loadMediaSelection('Saved', [str(self.c), str(self.root / 'missing.jpg')])
        self.assertEqual(self.view._files, [str(self.c)])
        self.assertFalse(any(c.GetValue() for c in self.view._filterChecks.values()))
        self.assertEqual(self.view._searchText.GetValue(), '')
        self.assertIn('1 saved file', self.view._searchError.GetLabel())
        self.assertIn('Selection: Saved', self.view._folderLabel.GetLabel())

    def test_normal_search_returns_to_folder_scope(self):
        self.view._selectionScope = [str(self.c)]
        self.view._searchText.ChangeValue('outside'); self.view.OnSearch(None)
        self.assertIsNone(self.view._selectionScope)
        self.assertEqual(set(self.view._files), {str(self.a), str(self.b)})

    def test_snapshot_helpers_rename_without_modifying_original(self):
        saved = {'one': [str(self.a), str(self.a)], 'two': [str(self.b)]}
        renamed = str(self.a.with_name('new.jpg'))
        result = remapSelections(saved, {str(self.a): renamed})
        self.assertEqual(result, {'one': [renamed], 'two': [str(self.b)]})
        self.assertEqual(len(saved['one']), 2)
        self.assertEqual(uniquePaths([str(self.a), str(self.a)]), [str(self.a)])

    def test_save_load_delete_selection_and_default_new_scope(self):
        stored = {}
        def get(key): return copy.deepcopy(stored.get(key, {}))
        def save(key, value): stored[key] = copy.deepcopy(value)
        with patch('frontEnd.AdvancedMediaSearch.getFrontEndPref', side_effect=get), \
             patch('frontEnd.AdvancedMediaSearch.setFrontEndPref', side_effect=save):
            dialog = AdvancedMediaSearchDialog(None, self.db.getSearchFields(), self.db.compileSearch,
                                               selectionPaths=[str(self.a), str(self.c)])
            try:
                self.assertEqual(dialog.scope.GetSelection(), 0)
                dialog.scope.SetSelection(1)
                entry = MagicMock(); entry.ShowModal.return_value = wx.ID_OK; entry.GetValue.return_value = 'Doors'
                with patch('wx.TextEntryDialog', return_value=entry): dialog._saveSelection(None)
                self.assertEqual(stored['imageMediaSelections']['Doors'], [str(self.a), str(self.c)])
                self.assertNotIn('scope', dialog.getState())
            finally: dialog.Destroy(); self.app.Yield()
            dialog = AdvancedMediaSearchDialog(None, self.db.getSearchFields(), self.db.compileSearch)
            try:
                self.assertEqual(dialog.scope.GetSelection(), 0)
                dialog.selections.SetStringSelection('Doors')
                with patch.object(dialog, 'EndModal') as end:
                    dialog._loadSelection(None); end.assert_called_once_with(wx.ID_APPLY)
                self.assertEqual(dialog.loadedSelection, ('Doors', [str(self.a), str(self.c)]))
                with patch('wx.MessageBox', return_value=wx.YES): dialog._deleteSelection(None)
                self.assertEqual(stored['imageMediaSelections'], {})
                self.assertTrue(self.a.exists()); self.assertTrue(self.c.exists())
            finally: dialog.Destroy(); self.app.Yield()


if __name__ == '__main__': unittest.main(verbosity=2)
