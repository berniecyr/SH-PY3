r"""Windows integration regression using queued native keys and the wx event loop.

Run with venv\Scripts\python.exe scripts\testImageSearchKeyboard.py.
Uses the live ImageView search-row builder and FrontEndFrame menu labels, without
starting cameras, detection workers, or opening the user's media database.
"""
import ast
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wx
from frontEnd.ImageView import ImageView
from frontEnd import MenuIds


class SearchHarness(ImageView):
    def __init__(self, frame):
        wx.Panel.__init__(self, frame)
        self._listPanel = self
        self._logger = logging.getLogger('keyboard-test')
        self.searches = []
        with patch('frontEnd.ImageView.getFrontEndPref', return_value=None):
            self._buildListPanel()

    def _applyFilters(self):
        self.searches.append(self._searchText.GetValue())


def main():
    if sys.platform != 'win32':
        raise SystemExit('This native keyboard regression requires Windows.')
    post = ctypes.windll.user32.PostMessageW
    post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post.restype = wintypes.BOOL
    app = wx.App(False)
    frame = wx.Frame(None, title='Image search keyboard regression', size=(1100, 350))
    menu = wx.Menu()
    tree = ast.parse((ROOT / 'frontEnd/FrontEndFrame.py').read_text(encoding='utf-8-sig'))
    controls = next(n for n in ast.walk(tree) if isinstance(n, ast.Tuple)
                    and n.elts and isinstance(n.elts[0], ast.Attribute)
                    and n.elts[0].attr == 'kControlsMenuEx')
    for entry in controls.elts[1:]:
        label = ast.literal_eval(entry.elts[0])
        if label is not None:
            menu.Append(wx.ID_ANY, label)
    original = [item.GetItemLabel() for item in menu.GetMenuItems()]
    bar = wx.MenuBar()
    bar.Append(menu, MenuIds.kControlsMenuEx)
    frame.SetMenuBar(bar)
    view = SearchHarness(frame)
    text = view._searchText
    failures = []
    shortcuts = []
    frame.Bind(wx.EVT_MENU, lambda e: shortcuts.append(e.GetId()))

    def key(vk, extended=False):
        flags = 1 | (int(extended) << 24)
        assert post(text.GetHandle(), 0x100, vk, flags)
        assert post(text.GetHandle(), 0x101, vk, flags | 0xC0000000)

    def check(value, caret=None, searches=None):
        assert text.GetValue() == value, repr(text.GetValue())
        if caret is not None:
            assert text.GetInsertionPoint() == caret, text.GetInsertionPoint()
        if searches is not None:
            assert len(view.searches) == searches, view.searches

    def disabled_baseline():
        # SearchView disables playback menu items on departure. They still eat
        # Space on Windows: reproduce that exact state before applying the fix.
        for item in menu.GetMenuItems():
            item.Enable(False)
        text.SetValue('bernie')
        text.SetInsertionPointEnd()
        key(32)

    def activate():
        check('bernie')
        view.setActiveView()
        view.setActiveView()  # repeated activation must preserve original labels
        text.SetValue('')

    def restored():
        view.deactivateView()
        assert [item.GetItemLabel() for item in menu.GetMenuItems()] == original
        for item in menu.GetMenuItems():
            item.Enable(True)
        key(32)

    def verify_restore():
        assert len(shortcuts) == 1, shortcuts
        check('bernie and beach', searches=2)
        view.setActiveView()
        text.SetInsertionPointEnd()
        key(32)

    steps = [disabled_baseline, activate]
    steps += [lambda c=c: key(ord(c.upper())) for c in 'bernie and beach']
    steps += [lambda: check('bernie and beach', 16), lambda: key(37),
              lambda: check('bernie and beach', 15), lambda: key(39),
              lambda: check('bernie and beach', 16), lambda: key(13),
              lambda: check('bernie and beach', searches=1),
              lambda: key(13, extended=True),
              lambda: check('bernie and beach', searches=2), restored,
              verify_restore, lambda: check('bernie and beach ', searches=2)]

    def advance():
        try:
            if not steps:
                frame.Destroy()
                return
            steps.pop(0)()
            wx.CallLater(35, advance)
        except Exception as exc:
            failures.append(exc)
            frame.Destroy()

    frame.Show()
    text.SetFocus()
    wx.CallLater(100, advance)
    app.MainLoop()
    if failures:
        raise failures[0]
    print('PASS: baseline reproduced; spaces, arrows, Return and numpad Enter; '
          'one search per Enter; playback shortcuts restored; repeated tab switches.')


if __name__ == '__main__':
    main()
