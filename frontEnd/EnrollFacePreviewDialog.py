#!/usr/bin/env python

#*****************************************************************************
#
# EnrollFacePreviewDialog.py
#   Preview-and-confirm modal for enrolling a face into the recognition
#   baseline.  Shows the harvested crops with their quality (detection score
#   and face pixel size) so the user can see EXACTLY what would be enrolled,
#   untick weak crops, pick/enter the person, and only then commit.
#
#   Replaces the old blind AddFaceDialog: crops used to be saved sight-unseen,
#   which let junk thumbnails pollute the baseline.
#
#*****************************************************************************

import base64
import io

import wx

_kThumbMaxPx = 160


class EnrollFacePreviewDialog(wx.Dialog):
    """Preview harvested face crops; confirm which to enroll and for whom.

    candidates: list of {'det': float, 'w': int, 'h': int, 'jpegB64': str}
    (best first, as returned by the harvest RPC).
    """

    def __init__(self, parent, candidates, existingNames, prefillName="",
                 prefillGender=""):
        wx.Dialog.__init__(self, parent, -1, "Add face to baseline")

        self._checks = []

        prompt = wx.StaticText(
            self, -1,
            "These face crops were found around this moment. Untick any that\n"
            "look poor (blurry, tiny, wrong person) — only ticked crops are\n"
            "added to the recognition baseline.")

        cropSizer = wx.BoxSizer(wx.HORIZONTAL)
        for cand in candidates:
            cell = wx.BoxSizer(wx.VERTICAL)
            bmp = self._makeThumb(cand.get("jpegB64", ""))
            if bmp is not None:
                cell.Add(wx.StaticBitmap(self, -1, bmp), 0,
                         wx.ALIGN_CENTER_HORIZONTAL)
            check = wx.CheckBox(self, -1, "Add this crop")
            check.SetValue(True)
            check.Bind(wx.EVT_CHECKBOX, self._onUiChange)
            self._checks.append(check)
            cell.Add(check, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 4)
            quality = wx.StaticText(
                self, -1, "det %.0f%%  ·  %d×%d px" %
                (cand.get("det", 0) * 100, cand.get("w", 0), cand.get("h", 0)))
            quality.SetForegroundColour(wx.Colour(110, 110, 110))
            cell.Add(quality, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
            cropSizer.Add(cell, 0, wx.RIGHT, 12)

        nameLabel = wx.StaticText(self, -1, "Name:")
        self._nameCtrl = wx.ComboBox(
            self, -1, prefillName, choices=sorted(existingNames),
            style=wx.CB_DROPDOWN, size=(220, -1))
        self._nameCtrl.Bind(wx.EVT_TEXT, self._onUiChange)

        genderLabel = wx.StaticText(self, -1, "Gender (new person):")
        self._genderCtrl = wx.Choice(self, -1,
                                     choices=["—", "Male", "Female"])
        self._genderCtrl.SetSelection(
            {"M": 1, "F": 2}.get((prefillGender or "").upper(), 0))

        buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self._okButton = self.FindWindow(self.GetAffirmativeId())

        gridSizer = wx.FlexGridSizer(2, 2, 8, 8)
        gridSizer.Add(nameLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        gridSizer.Add(self._nameCtrl, 0, wx.EXPAND)
        gridSizer.Add(genderLabel, 0, wx.ALIGN_CENTER_VERTICAL)
        gridSizer.Add(self._genderCtrl, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(prompt, 0, wx.ALL, 12)
        sizer.Add(cropSizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(gridSizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        sizer.Add(buttonSizer, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)

        self._nameCtrl.SetFocus()
        self._nameCtrl.SetInsertionPointEnd()
        self._onUiChange()

    ###########################################################
    def _makeThumb(self, jpegB64):
        """Decode a base64 JPEG into a thumbnail wx.Bitmap (or None)."""
        try:
            img = wx.Image(io.BytesIO(base64.b64decode(jpegB64)))
            if not img.IsOk():
                return None
            w, h = img.GetWidth(), img.GetHeight()
            scale = min(1.0, float(_kThumbMaxPx) / max(w, h, 1))
            if scale < 1.0:
                img = img.Scale(max(1, int(w * scale)),
                                max(1, int(h * scale)),
                                wx.IMAGE_QUALITY_HIGH)
            return wx.Bitmap(img)
        except Exception:
            return None

    ###########################################################
    def _onUiChange(self, event=None):
        """OK requires a name and at least one ticked crop."""
        if self._okButton is not None:
            self._okButton.Enable(bool(self.getName()) and
                                  bool(self.getSelectedIndices()))

    ###########################################################
    def getSelectedIndices(self):
        return [i for i, c in enumerate(self._checks) if c.GetValue()]

    ###########################################################
    def getName(self):
        return self._nameCtrl.GetValue().strip()

    ###########################################################
    def getGender(self):
        return {0: "", 1: "M", 2: "F"}.get(self._genderCtrl.GetSelection(), "")
