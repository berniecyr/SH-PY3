"""Exercise the live thumbnail row, context menu and rename dialog wiring."""
import logging
from pathlib import Path
import queue
import sys
import tempfile
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wx
from backEnd.UserMediaDb import UserMediaDb
from frontEnd.ImageView import ImageView


class Harness(ImageView):
    def __init__(self, parent, db, folder, paths):
        wx.Panel.__init__(self, parent)
        self._listPanel = self
        self._logger = logging.getLogger('file-actions-ui-test')
        self._db = db
        self._dbLock = threading.Lock()
        self._currentDir = str(folder)
        self._allFiles = list(map(str, paths))
        self._files = []
        self._busyPath = None
        self._scanning = False
        self._workQueue = queue.Queue()
        self._detailPanel = MagicMock()
        self._detailPanel.getPath.return_value = None
        self._detailPanel.setFile.side_effect = self.setDetailPath
        with patch('frontEnd.ImageView.getFrontEndPref', return_value=None):
            self._buildListPanel()

    def setDetailPath(self, path, isVideo):
        self._detailPanel.getPath.return_value = path

    def _refreshFaceNames(self):
        pass

    def _showStoredDetections(self, path):
        pass

    def _updateVideoPreview(self):
        pass


def main():
    app = wx.App(False)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        paths = [root / 'z.jpg', root / 'A.jpg']
        for path in paths:
            path.write_bytes(b'identical')
        db = UserMediaDb().open(str(root / 'test.db'))
        for path in paths:
            db.registerContent(str(path))
        frame = wx.Frame(None, size=(1100, 600))
        view = Harness(frame, db, root, paths)
        try:
            frame.Show()
            frame.Layout()
            view._applyFilters()
            assert view._files == list(map(str, paths[::-1]))
            view._fileList.selectPath(str(paths[0]))
            view._sortChoice.SetSelection(1)
            view.OnSortChanged(None)
            assert view._files == list(map(str, paths))
            assert view._fileList.getSelection() == str(paths[0])
            view._searchAllFolders.SetValue(True)
            view._searchText.SetValue('filename:jpg')
            view._applyFilters()
            assert view._files == list(map(str, paths))
            labels = []
            def popup(menu):
                labels.extend(i.GetItemLabel() for i in menu.GetMenuItems())
            view._fileList.PopupMenu = popup
            event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU, view._fileList.GetId())
            event.SetPosition(view._fileList.ClientToScreen(wx.Point(5, 5)))
            view.OnThumbnailContextMenu(event)
            assert labels == ['Show in Explorer', 'Rename file...'], labels
            assert view._fileList.getSelection() == str(paths[0])
            with patch('subprocess.Popen') as launch:
                view._showInExplorer(str(paths[0]))
                assert launch.call_args.args[0] == ['explorer.exe', '/select,', str(paths[0])]
            name = MagicMock()
            name.ShowModal.return_value = wx.ID_OK
            name.GetValue.return_value = 'renamed.jpg'
            scope = MagicMock()
            scope.ShowModal.return_value = wx.ID_OK
            scope.GetSelection.return_value = 0
            with patch('wx.TextEntryDialog', return_value=name), \
                    patch('wx.SingleChoiceDialog', return_value=scope), \
                    patch('wx.MessageBox') as error:
                view._renameFile(str(paths[0]))
                error.assert_not_called()
            assert (root / 'renamed.jpg').exists()
            assert paths[1].exists()
            assert not paths[0].exists()
            assert view._files == [str(root / 'renamed.jpg'), str(paths[1])]
            view._detailPanel.remapDescriptionDrafts.assert_called_once()
            print('PASS: folder/search sorting, selection, context menu, Explorer command, '
                  'duplicate scope dialog and rename refresh.')
        finally:
            view._fileList.stop()
            frame.Destroy()
            app.Yield()
            db.close()


if __name__ == '__main__':
    main()
