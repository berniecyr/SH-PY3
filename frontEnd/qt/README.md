# Porting screens from wx to PySide6 + Qt Designer

The point of this exercise: **stop expressing layout in Python.** In the wx
screens, where a control sits is the result of thirty lines of sizer calls that
you have to read like a program. In Qt, layout lives in a `.ui` file that you
open in a visual editor, drag things around in, and save. The Python file keeps
only the behaviour — validation, what the buttons do, talking to the back end.

`FtpSetupDialog` is the first screen done this way. It is a good pilot: small,
self-contained, one caller, and nothing breaks if it misbehaves.

---

## 1. The three files

| File | What it is | Who edits it |
|---|---|---|
| [`ui/FtpSetupDialog.ui`](ui/FtpSetupDialog.ui) | The layout. XML, but you never read it — Designer does. | **Qt Designer** |
| [`FtpSetupDialogQt.py`](FtpSetupDialogQt.py) | The behaviour. Validation, the Test button, the FTP config dict. | You, in an editor |
| [`QtCompat.py`](QtCompat.py) | Shared plumbing. Loads `.ui` files, owns the `QApplication`, bridges Qt to wx. | Rarely |

The original [`frontEnd/FtpSetupDialog.py`](../FtpSetupDialog.py) is untouched
and still the default. Nothing has been deleted.

**There is no build step.** The `.ui` is parsed at run time by
`QtCompat.loadUi()`. Save in Designer, reopen the screen, see the change. The
alternative — `pyside6-uic` compiling the `.ui` into Python — is what most Qt
tutorials show, and it is exactly the thing you said you didn't want: a
generated Python file you have to regenerate and never hand-edit.

---

## 2. Run it

From the repo root:

```bash
venv\Scripts\python.exe -m frontEnd.qt.FtpSetupDialogQt test
```

That opens the dialog on its own, with no back end running, and prints the
config dict when you hit OK. This is your sandbox — you can't hurt anything.

To see it inside the real app, set one environment variable before launching:

```bash
set SV_QT_FTP_DIALOG=1 && StartFrontend.bat
```

Then go to a rule's response config and click the FTP settings button. Without
that variable the app uses the old wx dialog exactly as before, so you can flip
between the two and compare.

---

## 3. Open it in Qt Designer

```bash
Designer.bat FtpSetupDialog.ui
```

(That's a wrapper around `venv\Scripts\pyside6-designer.exe`, which came with
PySide6. A bare filename is looked up in `frontEnd\qt\ui`; any other path is
used as-is. Run `Designer.bat` with no argument to just open the editor.)

### What you're looking at

- **Widget Box** (left) — the palette. Drag from here onto the form.
- **Object Inspector** (top right) — the widget tree. This is the fastest way
  to select something that's hard to click, and the clearest view of which
  layout a widget actually lives in.
- **Property Editor** (below it) — everything about the selected widget. The
  very first row is `objectName`. That one matters more than the rest; see §4.
- **Form** (middle) — the thing itself.

### The shortcuts worth memorising

| Key | Does |
|---|---|
| `Ctrl+R` | Preview the real dialog. Use this constantly. |
| `Ctrl+2` / `Ctrl+1` | Lay out selected widgets vertically / horizontally |
| `Ctrl+6` | Lay out in a **form layout** (label + field rows — what this dialog uses) |
| `Ctrl+5` | Lay out in a grid |
| `Ctrl+0` | **Break** the layout (widgets go back to floating) |
| `Ctrl+J` | Adjust size — shrink the form to fit its contents |
| `F3` / `F4` / `F5` / `F6` | Switch mode: edit widgets / signals & slots / buddies / tab order |

### The one concept that isn't obvious

Qt has no sizers. It has **layouts**, and a widget is either *inside* a layout
or it is floating at fixed pixel coordinates. Floating widgets don't resize,
don't align, and look wrong the moment the dialog is stretched.

So: when you drop a new widget on the form, it lands floating. To put it into a
layout, drag it onto an existing row until you see the blue insertion line, or
select it plus its neighbours and press one of the layout shortcuts above.
Designer shows a laid-out group with a thin red outline when you select it —
that outline is your confirmation that it's managed.

`Ctrl+R` after every change. If the preview looks right, it is right.

---

## 4. The contract between the two files: `objectName`

This is the whole interface. In Designer, every widget has an `objectName`.
In Python, `_initUiWidgets()` looks each one up by that exact string:

```python
self._hostField = findWidget(self._ui, QLineEdit, "hostField")
```

So:

- **Renaming a widget in Designer breaks the Python** — on purpose.
  `findWidget()` raises immediately with
  `UI file has no QLineEdit named 'hostField'`, rather than handing you a
  `None` that explodes ten minutes later. If you rename in Designer, rename in
  `_initUiWidgets()` too.
- **Moving, resizing, re-ordering, restyling** a widget changes nothing in
  Python. That's the payoff. Drag `Directory:` above `Host:`, widen the
  fields, change the spacing — no Python involved.
- **Label text is layout**, so it lives in the `.ui` now, not in the
  `_kHostLabelStr` constants the wx version had. The constants that remain in
  the Python file are only the ones for *runtime messages* (error boxes,
  progress text), which Designer knows nothing about.

---

## 5. Signals are wired in Python, not in Designer

Designer has a Signals/Slots mode (`F4`) that can connect a button to a slot
and store the connection in the `.ui`. **This dialog deliberately has none** —
`<connections/>` in the file is empty, and the wiring is in `_initUiWidgets()`:

```python
self._buttonBox.accepted.connect(self.OnOK)
self._buttonBox.rejected.connect(self.reject)
self._testButton.clicked.connect(self.OnTestUpload)
```

The reason is specific: `OnOK` validates before closing. If someone connected
`buttonBox.accepted` straight to the dialog's `accept()` in Designer — which is
what Designer's own dialog template does by default — OK would close the dialog
and skip every validation check, and the `.ui` file is the last place anyone
would look for that bug. Keep behaviour in Python where it's reviewable.

---

## 6. Try it: two exercises

**Exercise 1 — layout only, no Python.** Open the `.ui`, and:

1. Click the `Host:` row, drag it below `Directory:`.
2. Select `hostField`, find `minimumSize` in the Property Editor, change the
   width from 400 to 250.
3. `Ctrl+R` to preview, `Ctrl+S` to save.
4. Re-run the standalone command from §2.

The change is live. You never opened a Python file.

> While you're in there: the fields are all in column 1 of one `QFormLayout`,
> which is a single shared column — so the column is as wide as the *widest*
> minimum in it. That's why only `hostField` carries a `minimumSize` and the
> rest inherit the width.

**Exercise 2 — add a control, which does need Python.** Say you want an
"Anonymous login" checkbox:

1. In Designer, drag a Check Box into the form layout under `passiveCheckbox`.
2. Set its `objectName` to `anonymousCheckbox` and its `text` to
   `Anonymous login`. Save.
3. In `FtpSetupDialogQt.py`, add to `_initUiWidgets()`:
   ```python
   self._anonymousCheckbox = findWidget(self._ui, QCheckBox, "anonymousCheckbox")
   ```
4. Read and write it in `getFtpConfig()` / `_putFtpConfigToUi()` alongside
   `isPassive`.

Two files, because you added *behaviour*. Layout-only work stays in one.

---

## 7. wx → Qt cheat sheet

Widgets:

| wx | Qt |
|---|---|
| `wx.StaticText` | `QLabel` |
| `wx.TextCtrl` | `QLineEdit` (multi-line: `QPlainTextEdit`) |
| `wx.TextCtrl(style=wx.TE_PASSWORD)` | `QLineEdit`, `echoMode` = `Password` |
| `wx.ComboBox(style=wx.CB_DROPDOWN)` | `QComboBox`, `editable` = true |
| `wx.Choice` | `QComboBox`, `editable` = false |
| `wx.CheckBox` | `QCheckBox` |
| `wx.Button` | `QPushButton` |
| `wx.StdDialogButtonSizer` | `QDialogButtonBox` |

Layout:

| wx | Qt |
|---|---|
| `wx.BoxSizer(wx.VERTICAL)` | `QVBoxLayout` |
| `wx.BoxSizer(wx.HORIZONTAL)` | `QHBoxLayout` |
| `wx.FlexGridSizer(cols=2)` of label/field | `QFormLayout` |
| `wx.GridBagSizer` | `QGridLayout` |
| `sizer.AddStretchSpacer()` | a spacer item from the Widget Box |
| `wx.ALL, 12` border | the layout's `contentsMargins` |
| `proportion=1` | `sizePolicy` = Expanding, or the layout's stretch |
| `AddGrowableCol(1)` | `QFormLayout.fieldGrowthPolicy` |

Code:

| wx | Qt |
|---|---|
| `self.Bind(wx.EVT_BUTTON, h, btn)` | `btn.clicked.connect(h)` |
| `dlg.ShowModal()` → `wx.ID_OK` | `dlg.exec()` → `QDialog.Accepted` |
| `self.EndModal(wx.ID_OK)` | `self.accept()` |
| `textCtrl.GetValue()` / `SetValue()` | `.text()` / `.setText()` |
| `checkBox.GetValue()` / `SetValue()` | `.isChecked()` / `.setChecked()` |
| `comboBox.GetValue()` / `SetValue()` | `.currentText()` / `.setCurrentText()` |
| `ctrl.SetSelection(-1, -1)` | `.selectAll()` |
| `wx.MessageBox(..., wx.ICON_ERROR)` | `QMessageBox.critical(...)` |
| `wx.ProgressDialog` | `QProgressDialog` |
| `self.Fit()` | `self.adjustSize()` |
| `self.CenterOnParent()` | automatic for a dialog with a parent |
| `dlg.Destroy()` | `dlg.deleteLater()` |

### The wrapped-text trap — read this before porting any screen with prose

wx wraps a `StaticText` by calling `Wrap(350)`, which inserts real line breaks,
so the label's size is fixed and honest from then on. Qt's equivalent is
`wordWrap`, and it behaves differently in a way that silently breaks dialogs:

**`QLayout` computes its minimum height from each child's `minimumSize`, and
`minimumSize` does not consult `heightForWidth()`.** A word-wrapped `QLabel` is
precisely where those disagree — how tall it needs to be depends on how wide it
ends up — so the layout will happily make a dialog too short, and the last line
of text just disappears under whatever sits below it.

The vicious part is that it depends on the *string*. `RemoveCameraDialog`
looked perfect for a camera named "Front door" and cut off its last line for
one named "z_test2". It shipped, and a screenshot from the real app is what
caught it.

Two things are needed, and you want both:

1. **Give the label a definite width** — set `minimumSize` and `maximumSize`
   to the same width in Designer (350 here, matching the old `Wrap(350)`).
   Wrapping is then deterministic instead of depending on how the dialog was
   sized.
2. **Call `QtCompat.keepWrappedTextVisible(*labels)`** after setting their
   text. It pins each label's `minimumHeight` to the height its text actually
   needs, and keeps it pinned as the width changes, so the layout cannot
   squeeze it.

Then test more than one string. The spec for that screen sweeps a range of
camera names for exactly this reason; copy that check.

---

## 8. How Qt and wx share one process

During the migration both toolkits are loaded at once, which works but has
edges worth knowing:

- **One `QApplication`, forever.** `QtCompat.getQApplication()` creates it on
  first use and keeps it. Qt does not survive having its application object
  destroyed and rebuilt, so never do that.
- **`exec()` parks the wx loop.** A Qt modal dialog spins Qt's event loop, and
  wx's is stopped until it returns. On Windows both toolkits pull from the same
  thread message queue, so wx windows still repaint and respond — but anything
  driven by wx's own loop (wx timers, idle events) is suspended for the
  duration. Fine for a short modal settings dialog. **Not** fine for a
  long-lived or modeless screen, so don't port one that way without moving to a
  single Qt loop first.
- **Parenting is by HWND.** Qt can't see a wx window, so
  `QtCompat._attachToWxParent()` wraps the wx window's native handle in a
  `QWindow` and sets it as the transient parent. That's what keeps the dialog
  in front of the frame. It's Windows-specific and best-effort; if it fails you
  get an unparented dialog, not a crash.
- **wx windows are disabled while a Qt dialog is up**, via
  `wx.WindowDisabler()`, which is what reproduces wx's app-modal feel.

Verified working: the dialog opens over a live wx frame, disables it, returns
its config, and wx keeps running afterwards.

---

## 9. Known gaps

- **Frozen builds don't ship the `.ui` yet.** `frontEnd/setup-Win.py` builds
  with py2exe, and `.ui` files are data, not importable modules, so they need
  adding to `DATA_FILES`:
  ```python
  ("frontEnd/qt/ui", glob("frontEnd/qt/ui/*.ui")),
  ```
  `QtCompat.uiPath()` already looks next to the `.exe` when `sys.frozen` is
  set. PySide6's own Qt plugins (`platforms/qwindows.dll` especially) will also
  need bundling. **None of this is tested** — only source checkouts have been
  run. Do it before a build ships a Qt screen.
- **The FTP transfer still blocks the UI thread**, same as the wx version —
  up to the 30 s socket timeout. `QProgressDialog` repaints because
  `_updateProgress()` pumps events by hand. The real fix is a worker thread;
  that was left out to keep this a like-for-like port.
- **`vitaToolbox/wx/`** has no Qt counterpart yet. `fixSelection()` was simply
  dropped here (it worked around a Mac wx quirk). Bigger screens use more of
  that toolbox and will need real equivalents.

---

## 10. Two behaviour changes from the wx version

Both are deliberate.

1. **The Test button now works.** The wx version built the test file with
   `io.StringIO`; `ftplib.storlines()` compares each line against the bytes
   `b"\r\n"` and raises `TypeError` on a `str`, so on Python 3 that button
   always failed with "There was a problem while uploading". The Qt version
   uses `io.BytesIO`.
2. **`host` is stored as `str`, not `bytes`.** The wx version stored
   `field.GetValue().encode('ascii', 'strict')` — the encoded bytes — into the
   config dict. The Qt version validates that the host is ASCII (same error,
   same message) but stores the `str`. That matches `BackEndPrefs`, which
   defaults `host` to `""`, and `ResponseRunner`, which passes it to `ftplib`
   either way.

---

## 11. The verification harness

```bash
venv\Scripts\python.exe -m frontEnd.qt.screenTests.harness
```

Exits non-zero if anything fails, so it can gate a batch of conversions. Run
it after every port.

It exists because **a converted screen can import, compile and pass review
while still being broken.** The two things that actually go wrong are a widget
`objectName` that no longer matches the `.ui`, and a layout that changes shape
or clips its text — and neither is visible to any check except building the
widget and looking at it. So the harness builds every screen for real:

| Check | Catches |
|---|---|
| `.ui` parses standalone | malformed XML |
| builds (all objectNames bind) | renamed in Designer, not in Python |
| config round-trips | data lost on the way in or out |
| validation cases | a rule that stopped being enforced |
| rejections explain themselves | a screen that refuses input silently |
| nothing clipped | a label or button too small for its text |
| layout size unchanged | a layout that quietly changed shape |
| no Qt warnings | anything Qt itself complained about |

All of those are fault-injection tested — each one has been deliberately
broken and confirmed to fail.

Renders land in `screenTests/_renders/*.png`, one per screen. Those are for
eyeballing a batch: the harness can tell you a dialog changed size, but only
you can say whether it now looks right.

### Adding a screen to it

Copy [`screenTests/FtpSetupDialog.py`](screenTests/FtpSetupDialog.py) — it is
the template — and change `kUiFile`, `build()`, `readBack()`, `reset()`,
`cases()` and `kExpectedSize`. The harness discovers spec modules
automatically, and **fails if a `.ui` exists with no spec**, so a screen cannot
ship untested.

Two notes on writing `cases()`: include at least one case that should be
*accepted*, or a screen that rejects everything would pass; and the harness
also fails a rejection that shows the user no message, since a dialog that
silently refuses to close is its own bug.

`cases()` only fits a screen that accepts or rejects input. A confirmation
prompt has no rejection path, so a spec can instead supply `checks()` —
`(label, fn(dlg) -> (ok, detail))` — for arbitrary assertions.
[`screenTests/RemoveCameraDialog.py`](screenTests/RemoveCameraDialog.py) is the
example.

Set `kExpectedSize` **only after opening the render and confirming the screen
looks right.** Pasting whatever number turns it green defeats the entire check.

The harness renders using the real font stack and never shows a window
(`QWidget.grab()` paints into a pixmap). On a machine with no display, set
`SV_QT_HARNESS_OFFSCREEN=1` — the size check is skipped there, because
offscreen Qt ships no fonts and its geometry isn't comparable.

---

## 12. What to port next

Be careful with line count as a proxy — it is misleading here. Of the 19
smallest wx files, only **6 are `wx.Dialog` subclasses**; the rest are embedded
`wx.Panel`s, `ConstructionBlock`s, or not UI at all (`MenuIds`,
`FrontEndEvents`). **Embedded panels cannot use the bridge in §8** — it works
only because a modal `exec()` borrows the event loop and gives it back. A Qt
panel living inside a wx window is a different and much harder problem.

So the criteria are: subclasses `wx.Dialog`, shown with `ShowModal()`, and
doesn't pull in a heavy view. That gives roughly 16 candidates:

`EnrollFacePreviewDialog` (128) · `AboutBox` (199) · `FtpStatusDialog` (204) ·
~~`RemoveCameraDialog` (233)~~ *(done)* · `ClipRecordDialog` (334) ·
`DeleteClipDialog` (336) · `LoginDialog` (336) · `LocateVideoDialog` (356) ·
`ManageFacesDialog` (363) · `ScheduleLocationPicker` (373) ·
`RenameCameraDialog` (408) · `RuleScheduleDialog` (411) · `ArmCameras` (465) ·
`DetectionSearchDialog` (470) · `ExportClipsProgDialog` (481) ·
`MoveVideoDialog` (496)

Two exclusions worth knowing:

- **`QueryEditorDialog` (238)** looks small but embeds `QueryConstructionView`
  and `ResponseConfigPanel` (2,604 lines). It is not a leaf.
- **`DbCorruptedDialog` (164)** has no callers anywhere in the tree. Don't port
  it — work out whether it should be deleted.

Line count also hides toolbox coupling: `AboutBox` is 199 lines but pulls in
four `vitaToolbox.wx` widgets, which makes it harder than the 233-line
`RemoveCameraDialog` that pulls in one.

Leave the big ones — `FrontEndFrame`, `MonitorView`, `GridView` — until last.
Those are modeless and long-lived, and the wx/Qt event-loop trade in §8 doesn't
hold for them: they want a single Qt loop, which means the app's main loop has
to be Qt's. That's the actual end of the migration, not the start.
