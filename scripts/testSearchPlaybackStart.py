"""Exercise the real clip loader with a fake decoder and no live databases."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frontEnd.SearchResultsDataModel import SearchResultsDataModel


class PlaybackStartTests(unittest.TestCase):
    def load(self, enabled=True, starts=(15000,), entry=None, offset=None,
             gap=False, matching=True):
        clip = SimpleNamespace(camLoc='test', startTime=10000, stopTime=30000,
                               playStart=12000, objList=[1], startList=list(starts))
        decoder = Mock()
        decoder.openMarkedVideo.return_value = (10000, 30000)
        decoder.getFrameAt.side_effect = [None, object()] if gap else None
        decoder.getCurFrameOffset.return_value = 4000
        decoder.getFileStartMs.return_value = 10000
        model = SimpleNamespace(
            _getMatchingClipOrCache=lambda: (clip, 0 if matching else None),
            _videoLoadTimerRetries=0, _dataMgr=decoder, _desiredVideoSize=(640, 360),
            _enterClipAt=entry, _matchingClips=[clip], _isVideoLoaded=False)
        with patch('frontEnd.SearchResultsDataModel.getFrontEndPref', return_value=enabled):
            self.assertTrue(SearchResultsDataModel._loadClip(model, offset))
        self.assertTrue(model._isVideoLoaded)
        return [call.args[0] for call in decoder.getFrameAt.call_args_list]

    def test_detection_minus_one_second_not_padded_play_start(self):
        self.assertEqual(self.load(), [14000])

    def test_unchecked_starts_at_clip_beginning(self):
        self.assertEqual(self.load(enabled=False), [10000])

    def test_earliest_of_unsorted_detection_times(self):
        self.assertEqual(self.load(starts=(23000, 18000, 15000)), [14000])

    def test_early_detection_clamps_to_available_start(self):
        self.assertEqual(self.load(starts=(10500,)), [10000])
        self.assertEqual(self.load(starts=(8000,)), [10000])

    def test_missing_detection_starts_at_beginning(self):
        self.assertEqual(self.load(starts=(), matching=False), [10000])

    def test_explicit_timeline_entry_takes_priority(self):
        self.assertEqual(self.load(entry=22000), [22000])

    def test_reload_preserves_current_position(self):
        self.assertEqual(self.load(offset=21000), [21000])

    def test_gap_falls_back_to_recorded_clip_start(self):
        self.assertEqual(self.load(gap=True), [14000, 10000])


if __name__ == '__main__':
    unittest.main(verbosity=2)
