#!/usr/bin/env python

#*****************************************************************************
#
# ManageFacesDialog.py
#   Review and fix the face-recognition baseline (Options -> AI Detection ->
#   "Manage enrollments...").  Shows each person's enrolled images as
#   thumbnails with their file size (junk crops are conspicuously tiny) and
#   supports deleting images, moving them to the right person, renaming and
#   deleting people, and rebuilding known_faces.dat.
#
#   Thumbnails are read straight from the local Baseline folder (established
#   pattern); every MUTATION goes through a back-end RPC so it serializes
#   with in-progress enrollments and finishes with an automatic rebuild of
#   known_faces.dat (which has no image->embedding mapping).  Cameras
#   hot-reload the rebuilt file within seconds.
#
#*****************************************************************************

import os
import threading

import wx
import wx.lib.scrolledpanel

# Module top of FaceEnrollment is stdlib-only — safe to import in the FE.
from backEnd.FaceEnrollment import kBaselineDir, displayNameFromFolder

_kThumbPx = 128
_kImageExts = ('.jpg', '.jpeg', '.png', '.bmp')


class ManageFacesDialog(wx.Dialog):
    """Browse / correct / prune the face-enrollment baseline."""

    ###########################################################
    def __init__(self, parent, backEndClient):
        wx.Dialog.__init__(self, parent, -1, "Manage face enrollments",
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._backEndClient = backEndClient
        self._thumbChecks = []          # [(wx.CheckBox, relPath)]
        self._busy = False

        # -- left: people ----------------------------------------------------
        peopleLabel = wx.StaticText(self, -1, "People:")
        self._peopleList = wx.ListBox(self, -1, size=(190, -1))
        self._peopleList.Bind(wx.EVT_LISTBOX, self._onPersonSelect)

        self._renameBtn = wx.Button(self, -1, "Rename person...")
        self._deletePersonBtn = wx.Button(self, -1, "Delete person...")
        self._renameBtn.Bind(wx.EVT_BUTTON, self._onRenamePerson)
        self._deletePersonBtn.Bind(wx.EVT_BUTTON, self._onDeletePerson)

        leftSizer = wx.BoxSizer(wx.VERTICAL)
        leftSizer.Add(peopleLabel, 0, wx.BOTTOM, 4)
        leftSizer.Add(self._peopleList, 1, wx.EXPAND | wx.BOTTOM, 8)
        leftSizer.Add(self._renameBtn, 0, wx.EXPAND | wx.BOTTOM, 4)
        leftSizer.Add(self._deletePersonBtn, 0, wx.EXPAND)

        # -- right: thumbnails -----------------------------------------------
        thumbsLabel = wx.StaticText(
            self, -1, "Enrolled images (tick to select):")
        self._thumbPanel = wx.lib.scrolledpanel.ScrolledPanel(
            self, -1, size=(560, 380), style=wx.BORDER_SUNKEN)
        self._thumbSizer = wx.WrapSizer(wx.HORIZONTAL)
        self._thumbPanel.SetSizer(self._thumbSizer)
        self._thumbPanel.SetupScrolling(scroll_x=False)

        self._deleteImgsBtn = wx.Button(self, -1, "Delete selected")
        self._moveImgsBtn = wx.Button(self, -1, "Move selected to...")
        self._rebuildBtn = wx.Button(self, -1, "Rebuild encodings")
        self._deleteImgsBtn.Bind(wx.EVT_BUTTON, self._onDeleteImages)
        self._moveImgsBtn.Bind(wx.EVT_BUTTON, self._onMoveImages)
        self._rebuildBtn.Bind(wx.EVT_BUTTON, self._onRebuild)

        actionRow = wx.BoxSizer(wx.HORIZONTAL)
        actionRow.Add(self._deleteImgsBtn, 0, wx.RIGHT, 6)
        actionRow.Add(self._moveImgsBtn, 0, wx.RIGHT, 6)
        actionRow.AddStretchSpacer(1)
        actionRow.Add(self._rebuildBtn, 0)

        rightSizer = wx.BoxSizer(wx.VERTICAL)
        rightSizer.Add(thumbsLabel, 0, wx.BOTTOM, 4)
        rightSizer.Add(self._thumbPanel, 1, wx.EXPAND | wx.BOTTOM, 8)
        rightSizer.Add(actionRow, 0, wx.EXPAND)

        bodySizer = wx.BoxSizer(wx.HORIZONTAL)
        bodySizer.Add(leftSizer, 0, wx.EXPAND | wx.RIGHT, 10)
        bodySizer.Add(rightSizer, 1, wx.EXPAND)

        self._statusLabel = wx.StaticText(self, -1, "")
        self._statusLabel.SetForegroundColour(wx.Colour(110, 110, 110))

        buttonSizer = self.CreateStdDialogButtonSizer(wx.CLOSE)
        self.SetEscapeId(wx.ID_CLOSE)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(bodySizer, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(self._statusLabel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(buttonSizer, 0, wx.EXPAND | wx.BOTTOM, 12)
        self.SetSizer(sizer)
        self.SetMinSize((820, 560))
        self.Fit()
        self.CenterOnParent()

        self._refreshPeople()

    # ------------------------------------------------------------------ data

    ###########################################################
    def _personFolders(self):
        """Enrollment folders on disk (skips 'ignore'), sorted."""
        out = []
        try:
            for d in sorted(os.listdir(kBaselineDir)):
                if d.lower() == 'ignore':
                    continue
                if os.path.isdir(os.path.join(kBaselineDir, d)):
                    out.append(d)
        except OSError:
            pass
        return out

    ###########################################################
    def _imagesIn(self, folder):
        """Image filenames in one person folder, sorted."""
        try:
            return sorted(f for f in
                          os.listdir(os.path.join(kBaselineDir, folder))
                          if f.lower().endswith(_kImageExts))
        except OSError:
            return []

    ###########################################################
    def _selectedFolder(self):
        i = self._peopleList.GetSelection()
        if i == wx.NOT_FOUND:
            return None
        return self._peopleList.GetClientData(i)

    ###########################################################
    def _selectedImagePaths(self):
        return [rel for check, rel in self._thumbChecks if check.GetValue()]

    # ------------------------------------------------------------ UI refresh

    ###########################################################
    def _refreshPeople(self, keepFolder=None):
        if keepFolder is None:
            keepFolder = self._selectedFolder()
        self._peopleList.Clear()
        select = 0
        for i, folder in enumerate(self._personFolders()):
            label = "%s  (%d)" % (displayNameFromFolder(folder),
                                  len(self._imagesIn(folder)))
            self._peopleList.Append(label, folder)
            if folder == keepFolder:
                select = i
        if self._peopleList.GetCount():
            self._peopleList.SetSelection(select)
        self._refreshThumbs()

    ###########################################################
    def _refreshThumbs(self):
        self._thumbSizer.Clear(delete_windows=True)
        self._thumbChecks = []

        folder = self._selectedFolder()
        if folder is not None:
            for fn in self._imagesIn(folder):
                full = os.path.join(kBaselineDir, folder, fn)
                cell = wx.BoxSizer(wx.VERTICAL)
                bmp = self._makeThumb(full)
                if bmp is not None:
                    cell.Add(wx.StaticBitmap(self._thumbPanel, -1, bmp), 0,
                             wx.ALIGN_CENTER_HORIZONTAL)
                check = wx.CheckBox(self._thumbPanel, -1,
                                    self._shorten(fn, 22))
                check.SetToolTip(fn)
                self._thumbChecks.append((check, "%s/%s" % (folder, fn)))
                cell.Add(check, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
                try:
                    sizeKb = os.path.getsize(full) / 1024.0
                    caption = wx.StaticText(self._thumbPanel, -1,
                                            "%.1f KB" % sizeKb)
                    caption.SetForegroundColour(wx.Colour(110, 110, 110))
                    cell.Add(caption, 0, wx.ALIGN_CENTER_HORIZONTAL)
                except OSError:
                    pass
                self._thumbSizer.Add(cell, 0, wx.ALL, 6)

        self._thumbPanel.Layout()
        self._thumbPanel.SetupScrolling(scroll_x=False)

    ###########################################################
    def _makeThumb(self, path):
        try:
            img = wx.Image(path)
            if not img.IsOk():
                return None
            w, h = img.GetWidth(), img.GetHeight()
            scale = min(1.0, float(_kThumbPx) / max(w, h, 1))
            if scale < 1.0:
                img = img.Scale(max(1, int(w * scale)),
                                max(1, int(h * scale)),
                                wx.IMAGE_QUALITY_HIGH)
            return wx.Bitmap(img)
        except Exception:
            return None

    ###########################################################
    @staticmethod
    def _shorten(text, maxLen):
        return text if len(text) <= maxLen else text[:maxLen - 1] + "…"

    ###########################################################
    def _onPersonSelect(self, event=None):
        self._refreshThumbs()

    # -------------------------------------------------------------- RPC ops

    ###########################################################
    def _runOp(self, desc, fn):
        """Run a mutating RPC on a worker thread with busy UI."""
        if self._busy:
            return
        self._busy = True
        for btn in (self._renameBtn, self._deletePersonBtn,
                    self._deleteImgsBtn, self._moveImgsBtn, self._rebuildBtn):
            btn.Disable()
        self._statusLabel.SetLabel(desc + "…")

        def _worker():
            try:
                result = fn()
            except Exception as e:
                result = {"error": str(e)}
            wx.CallAfter(self._onOpDone, result)

        threading.Thread(target=_worker, daemon=True).start()

    ###########################################################
    def _onOpDone(self, result):
        self._busy = False
        for btn in (self._renameBtn, self._deletePersonBtn,
                    self._deleteImgsBtn, self._moveImgsBtn, self._rebuildBtn):
            btn.Enable()
        if result and result.get("ok"):
            self._statusLabel.SetLabel(
                "Done — %d people, %d encodings%s." % (
                    result.get("people", 0), result.get("totalEncodings", 0),
                    (", %d image(s) skipped" % result["skipped"])
                    if result.get("skipped") else ""))
        else:
            msg = (result or {}).get("error", "operation failed")
            self._statusLabel.SetLabel("Error: " + msg)
            wx.MessageBox(msg, "Operation failed", wx.OK | wx.ICON_ERROR, self)
        self._refreshPeople(keepFolder=(result or {}).get("folder"))

    # ------------------------------------------------------------- handlers

    ###########################################################
    def _onDeleteImages(self, event=None):
        paths = self._selectedImagePaths()
        if not paths:
            wx.MessageBox("Tick the image(s) to delete first.", "Nothing "
                          "selected", wx.OK | wx.ICON_INFORMATION, self)
            return
        if wx.MessageBox(
                "Delete %d image(s) from the baseline?\n\nThe recognition "
                "encodings are rebuilt automatically." % len(paths),
                "Delete images", wx.YES_NO | wx.ICON_WARNING, self) != wx.YES:
            return
        self._runOp("Deleting %d image(s)" % len(paths),
                    lambda: self._backEndClient.deleteBaselineImages(paths))

    ###########################################################
    def _onMoveImages(self, event=None):
        paths = self._selectedImagePaths()
        if not paths:
            wx.MessageBox("Tick the image(s) to move first.", "Nothing "
                          "selected", wx.OK | wx.ICON_INFORMATION, self)
            return
        current = self._selectedFolder()
        others = [f for f in self._personFolders() if f != current]
        kNew = "<New person…>"
        dlg = wx.SingleChoiceDialog(
            self, "Move %d image(s) to:" % len(paths), "Move images",
            [displayNameFromFolder(f) for f in others] + [kNew])
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            sel = dlg.GetSelection()
        finally:
            dlg.Destroy()

        if sel < len(others):
            target = others[sel]
        else:
            nameDlg = wx.TextEntryDialog(self, "New person's name:",
                                         "New person")
            try:
                if nameDlg.ShowModal() != wx.ID_OK:
                    return
                newName = nameDlg.GetValue().strip()
            finally:
                nameDlg.Destroy()
            if not newName:
                return
            gDlg = wx.SingleChoiceDialog(self, "Gender (folder suffix):",
                                         "New person", ["—", "Male", "Female"])
            try:
                gender = ""
                if gDlg.ShowModal() == wx.ID_OK:
                    gender = {1: "M", 2: "F"}.get(gDlg.GetSelection(), "")
            finally:
                gDlg.Destroy()
            target = newName + ("-%s" % gender if gender else "")

        self._runOp("Moving %d image(s)" % len(paths),
                    lambda: self._backEndClient.moveBaselineImages(
                        paths, target))

    ###########################################################
    def _onRenamePerson(self, event=None):
        folder = self._selectedFolder()
        if folder is None:
            return
        dlg = wx.TextEntryDialog(self, "New name for %s:" %
                                 displayNameFromFolder(folder),
                                 "Rename person",
                                 displayNameFromFolder(folder))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            newName = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        if not newName:
            return
        self._runOp("Renaming",
                    lambda: self._backEndClient.renameBaselinePerson(
                        folder, newName))

    ###########################################################
    def _onDeletePerson(self, event=None):
        folder = self._selectedFolder()
        if folder is None:
            return
        name = displayNameFromFolder(folder)
        count = len(self._imagesIn(folder))
        if wx.MessageBox(
                "Delete %s and all %d enrolled image(s)?\n\nThey will no "
                "longer be recognized." % (name, count),
                "Delete person", wx.YES_NO | wx.ICON_WARNING, self) != wx.YES:
            return
        self._runOp("Deleting %s" % name,
                    lambda: self._backEndClient.deleteBaselinePerson(folder))

    ###########################################################
    def _onRebuild(self, event=None):
        self._runOp("Rebuilding encodings",
                    lambda: self._backEndClient.rebuildKnownFaces())
