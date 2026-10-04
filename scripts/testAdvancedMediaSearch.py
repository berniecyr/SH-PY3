"""Advanced builder, saved-search UI and matching evidence on temporary data."""
import copy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wx
from frontEnd.AdvancedMediaSearch import AdvancedMediaSearchDialog, buildQuery, matchingValues, showMatches
from backEnd.UserMediaDb import UserMediaDb


def state(field='files.description_ai', op='Contains', value='door', **kwargs):
    return dict(base='', join='All', rules=[dict(field=field, op=op, value=value, **kwargs)])


class AdvancedSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = UserMediaDb().open(str(self.root / 'test.db'))

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def test_date_day_end_and_numeric_between(self):
        query = buildQuery(state('files.captureMs', 'On date', '2026-09-18'))
        low = int(datetime(2026, 9, 18).timestamp() * 1000)
        high = int(datetime(2026, 9, 19).timestamp() * 1000) - 1
        self.assertEqual(query, 'files.captureMs:between:"%d,%d"' % (low, high))
        sql, params = self.db.compileSearch(query)
        self.assertEqual(params, [low, high])
        self.assertEqual(buildQuery(state('files.durationMs', 'Between', '1000', end='5000')),
                         'files.durationMs:between:"1000,5000"')

    def test_boolean_mixed_base_and_exclusions(self):
        config = state('all', 'Whole word', 'door')
        config.update(base='person:Bernie', join='Any')
        config['rules'].append(dict(field='files.description_tags', op='Has exact tag', value='outdoor', exclude=True))
        query = buildQuery(config)
        self.assertEqual(query, '(person:Bernie) AND (all:word:"door" OR NOT (files.description_tags:tag:"outdoor"))')
        self.db.compileSearch(query)

    def test_blank_fields_quotes_and_invalid_ranges(self):
        self.assertEqual(buildQuery(state('files.description_ai', 'Is blank')), 'empty:files.description_ai')
        query = buildQuery(state(value='a "quoted" word'))
        self.assertEqual(self.db.compileSearch(query)[1], ['a "quoted" word'])
        for config in (state('all', 'Is blank'), state(value=''),
                       state('files.captureMs', 'On date', 'bad-date')):
            with self.assertRaises(ValueError): buildQuery(config)
        with self.assertRaises(ValueError): self.db.compileSearch(buildQuery(state('files.size', 'Between', '5', end='2')))

    def test_match_evidence(self):
        values = [('files.description_ai', 'A door outdoors.'), ('detections.faceName', 'Bernie'),
                  ('files.description_tags', 'front door; BEACH'), ('files.size', 40)]
        self.assertEqual(matchingValues('ai:word:door', values), [values[0]])
        self.assertEqual(matchingValues('person:exact:Bernie', values), [values[1]])
        self.assertEqual(matchingValues('tags:tag:beach', values), [values[2]])
        self.assertEqual(matchingValues('files.size:between:"30,50"', values), [values[3][0:1] + ('40',)])
        self.assertEqual(matchingValues('NOT word:doorbell', values), [])

    def test_builder_contains_remains_substring_with_quoted_values(self):
        values = [('files.description_ai', 'outdoor')]
        query = buildQuery(state(value='door'))
        self.assertEqual(query, 'files.description_ai CONTAINS "door"')
        self.assertEqual(matchingValues(query, values), values)
        self.assertEqual(matchingValues('files.description_ai:"door"', values), [])

    def test_dialog_saved_roundtrip_and_validation(self):
        config = state('files.captureMs', 'Between dates', '2026-09-01', end='2026-09-18')
        with patch('frontEnd.AdvancedMediaSearch.getFrontEndPref', return_value={}), \
             patch('frontEnd.AdvancedMediaSearch.setFrontEndPref') as save:
            dialog = AdvancedMediaSearchDialog(None, self.db.getSearchFields(), self.db.compileSearch, config)
            try:
                self.assertEqual(dialog.getState()['rules'][0]['op'], 'Between dates')
                self.assertEqual(dialog.error.GetLabel(), '')
                self.assertEqual(dialog.preview.GetValue(), buildQuery(config))
                entry = MagicMock(); entry.ShowModal.return_value = wx.ID_OK; entry.GetValue.return_value = 'September'
                with patch('wx.TextEntryDialog', return_value=entry): dialog._save(None)
                saved = copy.deepcopy(save.call_args.args[1])
                self.assertIn('September', saved)
                dialog.setState(state(value='other'))
                dialog.saved.SetStringSelection('September'); dialog._load(None)
                self.assertEqual(dialog.preview.GetValue(), buildQuery(config))
                dialog.setState(state(value=''))
                self.assertTrue(dialog.error.GetLabel())
                with patch.object(dialog, 'EndModal') as end:
                    dialog._search(None); end.assert_not_called()
                with patch('wx.MessageBox', return_value=wx.YES): dialog._delete(None)
                self.assertEqual(save.call_args.args[1], {})
            finally:
                dialog.Destroy(); self.app.Yield()

    def test_matching_window_renders_highlighted_value(self):
        import wx.richtext as rt
        seen = []
        def inspect(dialog):
            control = next(child for child in dialog.GetChildren() if isinstance(child, rt.RichTextCtrl))
            text = control.GetValue()
            self.assertIn('A door outdoors.', text)
            style = rt.RichTextAttr()
            control.GetStyle(text.index('A door outdoors.'), style)
            self.assertEqual(style.GetBackgroundColour(), wx.Colour(255, 245, 150))
            seen.append(True)
            return wx.ID_OK
        with patch.object(wx.Dialog, 'ShowModal', inspect):
            showMatches(None, 'ai:word:door', [('files.description_ai', 'A door outdoors.')])
        self.assertTrue(seen)
        self.app.Yield()


if __name__ == '__main__':
    unittest.main(verbosity=2)
