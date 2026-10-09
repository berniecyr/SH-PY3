"""Real Windows key messages against the large view, under the main window's
menus: open a thumbnail large, then press Delete and Escape.

    venv\\Scripts\\python.exe scripts\\testImageLargeViewKeys.py

The keys are posted as WM_KEYDOWN/WM_KEYUP and handled by the app's own main
loop, which is where Windows matches menu shortcuts -- the path on which the
Tools > Delete Clip shortcut (Del) swallowed the key before the large view
saw it.  Calling an EVT_KEY_DOWN handler directly would never show that.
With --no-suspend the shortcut is left in place, to show the failure.
"""
import ctypes
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wx
from PIL import Image
from backEnd.UserMediaDb import UserMediaDb
from frontEnd import MenuIds
from frontEnd.ImageThumbGrid import ThumbSelectedEvent, myEVT_THUMB_ACTIVATED
from testImageLargeView import Harness


kWmKeyDown, kWmKeyUp = 0x0100, 0x0101
# lParam as a keyboard sends it: repeat count 1 and the scan code in bits
# 16-23.  Delete is also an extended key (bit 24); without that bit Windows
# reports numpad Del.
kKeys = {'Delete': (0x2E, 0x01000001 | (0x53 << 16)),
         'Escape': (0x1B, 0x00000001 | (0x01 << 16))}


def pressKey(name):
    """Post one key press to whichever window has the focus."""
    vk, lparam = kKeys[name]
    focus = wx.Window.FindFocus()
    hwnd = focus.GetHandle() if focus else wx.GetTopLevelWindows()[0].GetHandle()
    ctypes.windll.user32.PostMessageW(hwnd, kWmKeyDown, vk, lparam)
    ctypes.windll.user32.PostMessageW(hwnd, kWmKeyUp, vk, lparam | 0xC0000000)


def hookKey(name):
    """Deliver a key as wxEVT_CHAR_HOOK from the focused control upwards.

    Real typing raises this event before the control sees the key; a posted
    message does not, so the cases where focus is on some other control are
    driven this way.
    """
    focus = wx.Window.FindFocus()
    event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
    event.SetKeyCode({'Delete': wx.WXK_DELETE, 'Escape': wx.WXK_ESCAPE}[name])
    event.SetEventObject(focus)
    event.ResumePropagation(wx.EVENT_PROPAGATE_MAX)
    focus.GetEventHandler().ProcessEvent(event)


def addMainMenus(frame):
    """The menus that hold key shortcuts, as FrontEndFrame builds them."""
    menuBar = wx.MenuBar()
    controls = wx.Menu()
    controls.Append(wx.ID_ANY, 'Play\tSpace')
    menuBar.Append(controls, MenuIds.kControlsMenuEx)
    tools = wx.Menu()
    deleteClip = tools.Append(wx.ID_ANY, MenuIds.kDeleteClipMenuEx)
    deleteClip.Enable(False)
    menuBar.Append(tools, MenuIds.kToolsMenuEx)
    frame.SetMenuBar(menuBar)
    return deleteClip


def main():
    suspend = '--no-suspend' not in sys.argv
    app = wx.App(False)
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    paths = [str(root / ('%d.jpg' % i)) for i in range(3)]
    for i, path in enumerate(paths):
        Image.new('RGB', (400, 300), (i * 80, 90, 120)).save(path, 'JPEG')
    db = UserMediaDb().open(str(root / 'test.db'))
    for path in paths:
        db.registerContent(path)
    frame = wx.Frame(None, size=(1100, 700))
    deleteClip = addMainMenus(frame)
    view = Harness(frame, db, root, paths)
    failures = []
    asked = []
    box = patch('wx.MessageBox', side_effect=lambda *a, **k: asked.append(a) or wx.NO)
    box.start()

    def steps():
        if suspend:
            view._suspendPlaybackAccelerators()
        view._applyFilters()
        yield
        # (where focus is, whether Delete should ask to delete the photo)
        for label, focusOn, deletes in (
                ('straight after the double-click', None, True),
                ('with focus on the sort choice', view._sortChoice, True),
                # A text box keeps Delete for its text; Escape still closes.
                ('with focus on the search box', view._searchText, False)):
            # A double-click as the grid delivers it.
            view._fileList.SetFocus()
            view._fileList.GetEventHandler().ProcessEvent(
                ThumbSelectedEvent(myEVT_THUMB_ACTIVATED, view._fileList.GetId(), paths[0]))
            if focusOn is not None:
                focusOn.SetFocus()
            yield
            del asked[:]
            press = pressKey if focusOn is None else hookKey
            press('Delete')
            yield
            if deletes and not asked:
                failures.append('Delete did nothing %s' % label)
            if not deletes and asked:
                failures.append('Delete was taken from the text box %s' % label)
            press('Escape')
            yield
            if view._largeView.IsShown():
                failures.append('Escape did nothing %s' % label)
                view._closeLargeView()
        # Leaving the Images tab puts the Delete Clip shortcut back.
        view._restorePlaybackAccelerators()
        if not deleteClip.GetItemLabel().endswith('\t' + MenuIds._kDeleteKey):
            failures.append('Delete Clip shortcut not restored: %r' % deleteClip.GetItemLabel())

    runner = steps()

    def advance():
        try:
            next(runner)
            wx.CallLater(300, advance)
        except StopIteration:
            frame.Destroy()
        except Exception as exc:
            failures.append('error: %r' % exc)
            frame.Destroy()

    frame.Show(); frame.Raise()
    wx.CallLater(300, advance)
    app.MainLoop()
    box.stop()
    db.close()
    temp.cleanup()
    if failures:
        print('FAILED: ' + '; '.join(failures))
        return 1
    print('All checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
