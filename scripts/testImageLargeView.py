"""Double-click to view large, wheel to step, Delete and Escape, on temporary files.

    venv\\Scripts\\python.exe scripts\\testImageLargeView.py
"""
import logging
from pathlib import Path
import queue
import sys
import tempfile
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wx
from PIL import Image
from backEnd.UserMediaDb import UserMediaDb
from frontEnd.ImageLargeView import fitSize
from frontEnd.ImageView import ImageView


class Harness(ImageView):
    """The list panel of the real ImageView, without the folder tree or back end."""

    def __init__(self, parent, db, folder, paths):
        wx.Panel.__init__(self, parent)
        self._listPanel = self
        self._logger = logging.getLogger('large-view-test')
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
        self._sizer = self.GetSizer()

    def setDetailPath(self, path, isVideo):
        self._detailPanel.getPath.return_value = path

    def _refreshFaceNames(self):
        pass

    def _showStoredDetections(self, path):
        pass

    def _updateVideoPreview(self):
        pass


def activate(view, path):
    from frontEnd.ImageThumbGrid import ThumbSelectedEvent, myEVT_THUMB_ACTIVATED
    view.OnThumbnailActivated(ThumbSelectedEvent(myEVT_THUMB_ACTIVATED, 0, path))


def wheel(view, rotation):
    event = wx.MouseEvent(wx.wxEVT_MOUSEWHEEL)
    event.SetWheelRotation(rotation)
    view._largeView.OnMouseWheel(event)


def key(view, code):
    event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
    event.SetKeyCode(code)
    view._largeView.OnKeyDown(event)


def main():
    assert fitSize((4000, 3000), (800, 800)) == (800, 600)
    assert fitSize((1000, 2000), (800, 800)) == (400, 800)

    app = wx.App(False)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        paths = [str(root / ('%d.jpg' % i)) for i in range(4)]
        for i, path in enumerate(paths):
            Image.new('RGB', (400 + i, 300), (i * 60, 90, 120)).save(path, 'JPEG')
        copy = str(root / 'sub' / '1-copy.jpg')
        Path(copy).parent.mkdir()
        Path(copy).write_bytes(Path(paths[1]).read_bytes())
        db = UserMediaDb().open(str(root / 'test.db'))
        for path in paths + [copy]:
            db.registerContent(path)
        db.saveResult(paths[2], {'kind': 'image', 'modelSig': 's', 'detections': [
            {'type': 'person', 'subType': 'person', 'conf': 0.9}]})

        frame = wx.Frame(None, size=(1100, 700))
        view = Harness(frame, db, root, paths)
        recycled = []
        try:
            frame.Show(); frame.Layout()
            view._applyFilters()
            assert view._files == paths, view._files

            # Double-click opens the large view in the grid's place.
            activate(view, paths[0])
            assert view._largeView.IsShown() and not view._fileList.IsShown()
            assert view._largeView.getPath() == paths[0]
            assert view._largeView._image is not None, 'image decoded'

            # Wheel down = next, up = previous, stopping at either end.
            wheel(view, -120); assert view._largeView.getPath() == paths[1]
            wheel(view, -120); assert view._largeView.getPath() == paths[2]
            wheel(view, 120); assert view._largeView.getPath() == paths[1]
            wheel(view, 120); wheel(view, 120); assert view._largeView.getPath() == paths[0]
            assert view._fileList.getSelection() == paths[0], 'grid follows'

            # End jumps to the last file and Home back to the first; the grid
            # selection and the details panel follow.
            key(view, wx.WXK_END)
            assert view._largeView.getPath() == paths[-1]
            assert view._fileList.getSelection() == paths[-1]
            assert view._detailPanel.getPath() == paths[-1]
            key(view, wx.WXK_HOME)
            assert view._largeView.getPath() == paths[0]
            assert view._fileList.getSelection() == paths[0]
            assert view._detailPanel.getPath() == paths[0]

            # Delete, answered No: nothing happens.
            wheel(view, -120); wheel(view, -120)
            with patch('wx.MessageBox', return_value=wx.NO) as box, \
                 patch('frontEnd.RecycleBin.recycle', side_effect=lambda path, hwnd=None: recycled.append(path)):
                key(view, wx.WXK_DELETE)
            assert recycled == [] and view._files == paths
            # Yes is the default button, so Enter confirms.
            style = box.call_args[0][2]
            assert style & wx.YES_NO and not style & wx.NO_DEFAULT, hex(style)

            # Delete, answered Yes: recycled, forgotten, and the NEXT file shows.
            def fakeRecycle(path, hwnd=None):
                recycled.append(path); Path(path).unlink()
            with patch('wx.MessageBox', return_value=wx.YES), \
                 patch('frontEnd.RecycleBin.recycle', side_effect=fakeRecycle):
                key(view, wx.WXK_DELETE)
            assert recycled == [paths[2]]
            assert db.getFile(paths[2]) is None and db.getDetections(paths[2]) == []
            assert view._files == [paths[0], paths[1], paths[3]]
            assert view._largeView.getPath() == paths[3], 'next file after a delete'

            # Deleting the LAST file shows the previous one instead.
            with patch('wx.MessageBox', return_value=wx.YES), \
                 patch('frontEnd.RecycleBin.recycle', side_effect=fakeRecycle):
                key(view, wx.WXK_DELETE)
            assert view._largeView.getPath() == paths[1], 'previous file after deleting the last'

            # A deleted copy leaves the other copy's record intact.
            with patch('wx.MessageBox', return_value=wx.YES), \
                 patch('frontEnd.RecycleBin.recycle', side_effect=fakeRecycle):
                key(view, wx.WXK_DELETE)
            assert db.getFile(paths[1]) is None
            assert db.getFile(copy) is not None and db.getLocations(copy) == [copy]
            assert view._largeView.getPath() == paths[0]

            # A failed recycle changes nothing.
            with patch('wx.MessageBox', return_value=wx.YES), \
                 patch('frontEnd.RecycleBin.recycle', side_effect=OSError('locked')):
                key(view, wx.WXK_DELETE)
            assert view._files == [paths[0]] and db.getFile(paths[0]) is not None

            # Escape returns to the grid, on the last file viewed.
            key(view, wx.WXK_ESCAPE)
            assert view._fileList.IsShown() and not view._largeView.IsShown()
            assert view._fileList.getSelection() == paths[0]

            # Changing the listing while viewing large also returns to the grid.
            activate(view, paths[0])
            view._applyFilters()
            assert view._fileList.IsShown() and not view._largeView.IsShown()
        finally:
            frame.Destroy()
            db.close()
    print('All checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
