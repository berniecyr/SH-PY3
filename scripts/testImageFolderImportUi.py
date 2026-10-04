"""Test folder UI decisions and service isolation without live user data."""
import logging
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wx
from frontEnd.ImageView import ImageView


class FolderUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.frame = wx.Frame(None)
        self.view = ImageView.__new__(ImageView)
        wx.Panel.__init__(self.view, self.frame)
        v = self.view
        v._currentDir = self.temp.name
        v._scanning = False
        v._stopEvent = threading.Event()
        v._workQueue = queue.Queue()
        v._logger = logging.getLogger('folder-ui-test')
        v._getDb = MagicMock(return_value=MagicMock())
        v._ensureWorker = MagicMock()
        v._scanStatus = wx.StaticText(v)
        v._analyzeButton = wx.Button(v)
        v._dbLock = threading.Lock()
        v._post = lambda fn, *args: fn(*args)
        v._folderFinished = MagicMock()
        v._folderProgress = MagicMock()

    def tearDown(self):
        self.frame.Destroy()
        self.app.Yield()
        self.temp.cleanup()

    def choose(self, mode=0, answer=wx.ID_OK):
        dialog = MagicMock()
        dialog.ShowModal.return_value = answer
        dialog.GetSelection.return_value = mode
        with patch('wx.SingleChoiceDialog', return_value=dialog):
            self.view.OnAnalyzeFolder(MagicMock())
        dialog.SetSelection.assert_called_once_with(0)
        dialog.Destroy.assert_called_once()

    def test_default_analysis_and_stop(self):
        self.choose()
        job = self.view._workQueue.get_nowait()
        self.assertTrue(job['analyze'])
        self.view.OnAnalyzeFolder(MagicMock())
        self.assertTrue(job['cancel'].is_set())

    def test_cancel_mode_dialog(self):
        self.choose(answer=wx.ID_CANCEL)
        self.assertTrue(self.view._workQueue.empty())
        self.assertFalse(self.view._scanning)

    def test_filename_mode_never_connects(self):
        self.choose(1)
        with patch('backEnd.UserMediaAnalysis.openDetectionClient') as connect, \
                patch('backEnd.UserMediaFolderImport.importFolder',
                      return_value=dict(registered=1, analyzed=0, reused=0, failed=0)) as run:
            self.view._importFolder(self.view._workQueue.get_nowait())
        connect.assert_not_called()
        self.assertFalse(run.call_args.args[5])

    def test_model_warning_abort_and_continue(self):
        for answer in (wx.ID_NO, wx.ID_YES):
            self.view._scanning = False
            self.choose()
            client = MagicMock()
            client.ping.return_value = dict(face=False, nudity=True)
            warning = MagicMock()
            warning.ShowModal.return_value = answer
            with patch('backEnd.UserMediaAnalysis.loadConfig',
                       return_value=dict(RUN_FACE=True, RUN_NUDITY=True)), \
                    patch('backEnd.UserMediaAnalysis.openDetectionClient', return_value=client), \
                    patch('wx.MessageDialog', return_value=warning), \
                    patch('backEnd.UserMediaFolderImport.importFolder',
                          return_value=dict(registered=0, analyzed=0, reused=0, failed=0)) as run:
                self.view._importFolder(self.view._workQueue.get_nowait())
            warning.SetYesNoLabels.assert_called_once_with('Continue', 'Abort')
            self.assertEqual(run.called, answer == wx.ID_YES)
            if run.called:
                self.assertFalse(run.call_args.args[3]['RUN_FACE'])
            client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
