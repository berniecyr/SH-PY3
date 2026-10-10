#! /usr/local/bin/python

#*****************************************************************************
#
# ImageDetailPanel.py
#     Everything we know about one of the user's files.
#
#
#*****************************************************************************
#
#
# Copyright 2013-2022 Sighthound, Inc.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
# Alternative licensing available from Sighthound, Inc.
# by emailing opensource@sighthound.com
#
# This file is part of the Sighthound Video project which can be found at
# https://github.com/sighthoundinc/SighthoundVideo
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; using version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02111, USA.
#
#
#*****************************************************************************

"""
## @file
The right-hand pane of the Image view: one file, in as much detail as we have.

Three things here are deliberate rather than incidental:

  * The preview decodes AT the size it will be shown.  A photo library is full
    of 12 megapixel files, and holding even a handful of those decoded at full
    resolution is how a browser like this runs a machine out of memory.  PIL's
    draft() lets the JPEG decoder skip most of the work outright -- measured on
    a 4032x3024 fixture, 0.19 MB retained instead of 37 MB.

  * Explicit-content classes are displayed inline, once per class with the
    highest confidence across all detections and sampled video frames.

  * Metadata is read on a DEBOUNCE, not on every selection.  With ExifTool
    installed a read is a subprocess spawn, and holding an arrow key down
    through a folder would otherwise start one per file.
"""

# Python imports...
import os
import time

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.wx.FontUtils import makeFontBold, makeFontDefault
from vitaToolbox.wx.TranslucentStaticText import TranslucentStaticText

# Local imports...
from appCommon import ExifData


# Constants...

_kBorder = 12
_kCtrlPadding = 4

# The preview box.  Fixed height so the fields below it do not jump as the
# user moves between a portrait and a landscape photo.
_kPreviewHeight = 200

# Rows of the info table, in display order.
_kInfoRows = ["Name", "Folder", "Type", "Size", "Dimensions", "Taken",
              "Modified"]

# How long to wait after a selection before reading metadata.  Reading is a
# subprocess spawn when ExifTool is present, and holding an arrow key down
# through a folder would otherwise start one per file.
_kMetadataDebounceMs = 150

# Colours, matching the Search view's detection panel.
_kColGray = (110, 110, 110)
_kColBlue = (0, 102, 204)


##############################################################################
def _formatSize(numBytes):
    """Render a file size the way a file manager would.

    @param  numBytes  Size in bytes.
    @return str       e.g. "1.4 MB".
    """
    size = float(numBytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "bytes":
                return "%d bytes" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0


##############################################################################
def summarizeDetections(rows):
    """Turn stored detection rows into lines a person can read.

    @param  rows  sqlite3.Row objects from UserMediaDb.getDetections.
    @return list  Lines of text, most interesting first.
    """
    if not rows:
        return []

    counts = {}
    names = {}
    faceCount = 0
    firstSeen = {}

    for row in rows:
        kind = row["type"] or "object"
        counts[kind] = counts.get(kind, 0) + 1
        at = row["atMs"] or 0
        if kind not in firstSeen or at < firstSeen[kind]:
            firstSeen[kind] = at
        if row["faceName"]:
            names[row["faceName"]] = max(names.get(row["faceName"], 0.0),
                                         row["faceConf"] or 0.0)
        if row["faceDetConf"] is not None:
            faceCount += 1

    label = {"person": "people", "animal": "animals", "vehicle": "vehicles"}
    lines = []
    for kind in ("person", "animal", "vehicle"):
        if kind in counts:
            n = counts[kind]
            word = label[kind] if n != 1 else kind
            lines.append("%d %s" % (n, word))

    if names:
        best = sorted(names.items(), key=lambda kv: -kv[1])
        lines.append("Recognized: " + ", ".join(
            "%s (%.0f%%)" % (n, c * 100) for n, c in best))
    elif faceCount:
        lines.append("%d face%s detected, none recognized"
                     % (faceCount, "" if faceCount == 1 else "s"))

    return lines


##############################################################################
def summarizeNudity(details):
    """Format stored CLASS=score lists, keeping the highest score per class."""
    best = {}
    for detail in details:
        for entry in detail.split(','):
            name, sep, rawScore = entry.partition('=')
            name = name.strip()
            if not sep or not name:
                continue
            try:
                score = float(rawScore)
            except ValueError:
                continue
            if not 0 <= score <= 1:
                continue
            best[name] = max(best.get(name, 0), score)
    return ["%s: %.0f%%" % (name.replace('_', ' ').capitalize(), score * 100)
            for name, score in sorted(best.items(), key=lambda item: (-item[1], item[0]))]


##############################################################################
class ImageDetailPanel(wx.Panel):
    """Shows one user file: preview, file facts, and our detections."""

    ###########################################################
    def __init__(self, parent, logger, analyzeCallback=None,
                 loadDescriptions=None, saveDescriptions=None,
                 recordCallback=None, selectionCount=None):
        """Initializer for ImageDetailPanel.

        @param  parent           The parent window.
        @param  logger           A logger, for decode failures.
        @param  analyzeCallback  f(path) -- called when Analyze is clicked.
        @param  recordCallback   f(path) -- called when View record is clicked.
        @param  selectionCount   f() -- how many files are selected; View
                                 record is offered only for exactly one.
        """
        super(ImageDetailPanel, self).__init__(
            parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize,
            wx.TAB_TRAVERSAL | wx.BORDER_NONE | wx.TRANSPARENT_WINDOW
        )
        self._logger = logger
        self._analyzeCallback = analyzeCallback
        self._loadDescriptions = loadDescriptions
        self._saveDescriptions = saveDescriptions
        self._recordCallback = recordCallback
        self._selectionCount = selectionCount
        self._descriptionDrafts = {}
        self._path = None
        self._isVideo = False
        self._nudityDetail = []
        self._tags = {}
        self._faceRows = []
        self._faceAnalyzedMs = None

        # Debounced so that arrowing through a folder does not spawn an
        # ExifTool process per file.  The Destroy override below is not
        # optional: a wx.Timer that outlives its window is a documented crash
        # in this codebase (HANDOFF.md 5.12).
        self._metaTimer = wx.Timer(self, -1)
        self.Bind(wx.EVT_TIMER, self._onMetadataTimer, self._metaTimer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)

        self._initUiWidgets()
        self.clear()


    ###########################################################
    def _initUiWidgets(self):
        """Build the layout."""
        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = TranslucentStaticText(self, -1, "Details")
        makeFontBold(heading)
        sizer.Add(heading, 0, wx.BOTTOM, _kCtrlPadding)

        # A plain StaticBitmap: the bitmap is already scaled to fit by the
        # time it arrives, so a scaling control would have nothing to do, and
        # an unreadable file simply shows nothing rather than a stretched
        # placeholder.
        self._preview = wx.StaticBitmap(self, -1, wx.Bitmap(1, 1))
        self._preview.SetMinSize((-1, _kPreviewHeight))
        sizer.Add(self._preview, 0, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)

        self._previewNote = TranslucentStaticText(self, -1, "")
        makeFontDefault(self._previewNote)
        sizer.Add(self._previewNote, 0, wx.BOTTOM, _kCtrlPadding)

        # Info table: label / value pairs, value column growable so a long
        # path wraps rather than widening the pane.
        self._infoSizer = wx.FlexGridSizer(0, 2, _kCtrlPadding,
                                           2 * _kCtrlPadding)
        self._infoSizer.AddGrowableCol(1)
        self._infoValues = {}
        for name in _kInfoRows:
            label = TranslucentStaticText(self, -1, name)
            makeFontBold(label)
            value = TranslucentStaticText(self, -1, "")
            makeFontDefault(value)
            self._infoValues[name] = value
            self._infoSizer.Add(label, 0)
            self._infoSizer.Add(value, 1, wx.EXPAND)
        sizer.Add(self._infoSizer, 0, wx.EXPAND | wx.BOTTOM, _kBorder)

        detLabel = TranslucentStaticText(self, -1, "Detections")
        makeFontBold(detLabel)
        sizer.Add(detLabel, 0)

        self._detectionText = TranslucentStaticText(self, -1, "")
        makeFontDefault(self._detectionText)
        sizer.Add(self._detectionText, 0, wx.TOP, 2)

        self._faceChoice = wx.Choice(self, -1)
        self._faceChoice.SetToolTip("Choose the face detection to add to the baseline")
        sizer.Add(self._faceChoice, 0, wx.EXPAND | wx.TOP, _kCtrlPadding)
        self._addFaceButton = wx.Button(self, -1, "Add face to baseline...")
        self._addFaceButton.Bind(wx.EVT_BUTTON, self._onAddFace)
        sizer.Add(self._addFaceButton, 0, wx.TOP, 2)

        # This list is multiline; TranslucentStaticText cannot paint multiline text.
        self._nudityText = wx.StaticText(self, -1, "")
        makeFontDefault(self._nudityText)
        sizer.Add(self._nudityText, 0, wx.EXPAND | wx.TOP, _kBorder)

        buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
        self._analyzeButton = wx.Button(self, -1, "Analyze this file")
        self._analyzeButton.Bind(wx.EVT_BUTTON, self._onAnalyze)
        buttonSizer.Add(self._analyzeButton, 0, wx.RIGHT, _kCtrlPadding)
        # Enabled on UI update rather than on selection events: Ctrl- and
        # Shift-clicks change the grid's selection without firing one.
        self._recordButton = wx.Button(self, -1, "View record...")
        self._recordButton.SetToolTip("Show every stored field for the selected file")
        self._recordButton.Bind(wx.EVT_BUTTON, self._onViewRecord)
        self._recordButton.Bind(wx.EVT_UPDATE_UI, self._onUpdateRecordButton)
        self._recordButton.Show(self._recordCallback is not None)
        buttonSizer.Add(self._recordButton, 0)
        sizer.Add(buttonSizer, 0, wx.TOP | wx.BOTTOM, _kBorder)

        self._statusText = TranslucentStaticText(self, -1, "")
        makeFontDefault(self._statusText)
        self._statusText.SetForegroundColour(_kColGray)
        sizer.Add(self._statusText, 0)

        self._metadataArea = wx.ScrolledWindow(self, -1)
        self._metadataArea.SetScrollRate(0, 10)
        self._metadataArea.SetMinSize((-1, 150))
        accordion = wx.BoxSizer(wx.VERTICAL)
        self._descriptionPanes = []
        for label in ("Description tags", "AI description", "EXIF metadata",
                      "Duplicates found in folders"):
            pane = wx.CollapsiblePane(self._metadataArea, label=label,
                                      style=wx.CP_DEFAULT_STYLE | wx.CP_NO_TLW_RESIZE)
            pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self._onDescriptionPane)
            self._descriptionPanes.append(pane)
            accordion.Add(pane, 0, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)
        tagsPane, aiPane, exifPane, duplicatesPane = [p.GetPane() for p in self._descriptionPanes]
        self._duplicateFolders = wx.TextCtrl(duplicatesPane, -1, size=(-1, 100),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self._duplicateFolders.svKeepOwnBackground = True
        duplicateSizer = wx.BoxSizer(wx.VERTICAL)
        duplicateSizer.Add(self._duplicateFolders, 1, wx.EXPAND | wx.ALL, 2)
        duplicatesPane.SetSizer(duplicateSizer)
        self._descriptionTags = wx.TextCtrl(tagsPane, -1)
        self._descriptionAi = wx.TextCtrl(aiPane, -1, style=wx.TE_MULTILINE,
                                          size=(-1, 120))
        for pane, editor in ((tagsPane, self._descriptionTags),
                             (aiPane, self._descriptionAi)):
            editor.svKeepOwnBackground = True
            editor.Bind(wx.EVT_TEXT, self._onDescriptionEdited)
            inner = wx.BoxSizer(wx.VERTICAL)
            inner.Add(editor, 1, wx.EXPAND | wx.ALL, 2)
            pane.SetSizer(inner)

        exifSizer = wx.BoxSizer(wx.VERTICAL)

        self._metaList = wx.ListCtrl(
            exifPane, -1, size=(-1, 180),
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        # Keeps its own colours: applyToTree repaints backgrounds but repairs
        # contrast only on wx.StaticText.
        self._metaList.svKeepOwnBackground = True
        self._metaList.InsertColumn(0, "Tag", width=150)
        self._metaList.InsertColumn(1, "Value", width=200)
        self._metaList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._onEditTag)
        exifSizer.Add(self._metaList, 1, wx.EXPAND | wx.TOP, 2)

        self._metaNote = TranslucentStaticText(exifPane, -1, "")
        makeFontDefault(self._metaNote)
        self._metaNote.SetForegroundColour(_kColGray)
        exifSizer.Add(self._metaNote, 0, wx.TOP | wx.BOTTOM, 2)
        exifPane.SetSizer(exifSizer)
        self._descriptionStatus = wx.StaticText(self._metadataArea, -1, "")
        accordion.Add(self._descriptionStatus, 0, wx.TOP, 2)
        self._descriptionRetry = wx.Button(self._metadataArea, -1, "Retry save")
        self._descriptionRetry.Bind(wx.EVT_BUTTON, self._onDescriptionEdited)
        accordion.Add(self._descriptionRetry, 0, wx.TOP, 2)
        self._metadataArea.SetSizer(accordion)
        self._descriptionPanes[0].Expand()
        sizer.Add(self._metadataArea, 1, wx.EXPAND | wx.TOP, _kBorder)

        self.SetSizer(sizer)


    ###########################################################
    def clear(self):
        """Show the no-selection state."""
        self._path = None
        self._showDescriptions()
        self._isVideo = False
        self._nudityDetail = []
        self._preview.SetBitmap(wx.Bitmap(1, 1))
        self._previewNote.SetLabel("")
        for value in self._infoValues.values():
            value.SetLabel("")
        self._setFaceDetections([], None)
        self._detectionText.SetLabel("Select a file.")
        self._nudityText.Hide()
        self._analyzeButton.Enable(False)
        self._statusText.SetLabel("")
        self._stopMetadataTimer()
        self._tags = {}
        self._metaList.DeleteAllItems()
        self._metaNote.SetLabel("")
        self.Layout()


    ###########################################################
    def setFile(self, path, isVideo):
        """Show a file's facts and preview.  Detections arrive separately.

        @param  path     Absolute path of the file.
        @param  isVideo  True if this is a video rather than a still.
        """
        self._setFaceDetections([], None)
        self._path = path
        self._isVideo = isVideo
        self._nudityDetail = []
        self._nudityText.Hide()
        self._statusText.SetLabel("")

        try:
            stat = os.stat(path)
        except OSError as e:
            self._logger.info("ImageDetailPanel: cannot stat %s: %s"
                              % (path, e))
            self.clear()
            self._detectionText.SetLabel("This file is no longer readable.")
            return

        self._infoValues["Name"].SetLabel(os.path.basename(path))
        self._infoValues["Folder"].SetLabel(os.path.dirname(path))
        self._infoValues["Type"].SetLabel("Video" if isVideo else "Image")
        self._infoValues["Size"].SetLabel(_formatSize(stat.st_size))
        self._infoValues["Modified"].SetLabel(
            time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(stat.st_mtime)))
        self._infoValues["Taken"].SetLabel("")

        if isVideo:
            # ImageView supplies the grid's cached thumbnail immediately,
            # or when its existing worker finishes. No second video decode.
            self._infoValues["Dimensions"].SetLabel("--")
            self._preview.SetBitmap(wx.Bitmap(1, 1))
            self._previewNote.SetLabel("Loading video thumbnail...")
        else:
            self._showImagePreview(path)

        self._analyzeButton.Enable(True)
        self._showDescriptions()

        self._tags = {}
        self._metaList.DeleteAllItems()
        self._metaNote.SetLabel("Reading...")
        self._stopMetadataTimer()
        self._metaTimer.Start(_kMetadataDebounceMs, wx.TIMER_ONE_SHOT)

        self.Layout()


    ###########################################################
    def _onDescriptionPane(self, event):
        """Sections expand independently and retain their state across files."""
        self._layoutDescriptions()


    def _layoutDescriptions(self):
        self._metadataArea.Layout()
        self._metadataArea.FitInside()
        self.Layout()


    def _showDescriptions(self):
        """Load without text events; never confuse programmatic loads with edits."""
        values = {}
        enabled = bool(self._path and self._loadDescriptions and self._saveDescriptions)
        status = "Changes save automatically." if enabled else ""
        if enabled:
            try:
                values = self._loadDescriptions(self._path)
            except Exception as exc:
                enabled = False
                status = "Could not load descriptions. Reselect file to retry."
                self._logger.warning("Description lookup failed: %s" % exc)
        draft = self._descriptionDrafts.get(self._path)
        if draft is not None:
            values.update(description_tags=draft[0], description_ai=draft[1])
            status = "Not saved. Retry before closing the application."
        self._descriptionTags.ChangeValue(values.get("description_tags", ""))
        self._descriptionAi.ChangeValue(values.get("description_ai", ""))
        folders = sorted({os.path.dirname(p) for p in values.get('locations', [])
                          if os.path.normcase(os.path.dirname(p)) !=
                          os.path.normcase(os.path.dirname(self._path or ''))}, key=str.casefold)
        self._duplicateFolders.ChangeValue('\n'.join(folders) if folders else
            ('No other indexed folders.' if enabled else ''))
        self._descriptionTags.Enable(enabled)
        self._descriptionAi.Enable(enabled)
        self._descriptionStatus.SetLabel(status)
        self._descriptionRetry.Show(draft is not None)
        self._layoutDescriptions()


    def remapDescriptionDrafts(self, changes):
        """Keep unsaved description edits attached to a renamed location."""
        for old, new in changes.items():
            if old in self._descriptionDrafts:
                self._descriptionDrafts[new] = self._descriptionDrafts.pop(old)


    def _onDescriptionEdited(self, event):
        """Persist each edit; keep failed drafts when navigating between files."""
        if not self._path or self._saveDescriptions is None:
            return
        values = (self._descriptionTags.GetValue(), self._descriptionAi.GetValue())
        self._descriptionDrafts[self._path] = values
        try:
            self._saveDescriptions(self._path, *values)
        except Exception as exc:
            self._logger.warning("Description save failed: %s" % exc)
            self._descriptionStatus.SetLabel(
                "Not saved. Retry before closing the application.")
            self._descriptionRetry.Show()
        else:
            self._descriptionDrafts.pop(self._path, None)
            self._descriptionStatus.SetLabel("Saved automatically.")
            self._descriptionRetry.Hide()
        self._layoutDescriptions()


    ###########################################################
    def setVideoPreview(self, path, bitmap, failed=False):
        """Display the selected video's cached grid thumbnail and play marker."""
        if path != self._path or not self._isVideo:
            return
        if bitmap is None or not bitmap.IsOk():
            self._preview.SetBitmap(wx.Bitmap(1, 1))
            self._previewNote.SetLabel("Cannot preview this video." if failed
                                       else "Loading video thumbnail...")
        else:
            from frontEnd.ImageThumbGrid import drawVideoBadge
            # Copy before drawing: the grid owns its bitmap. Preserve aspect
            # ratio and only shrink thumbnails larger than the detail preview.
            image = bitmap.ConvertToImage()
            width = max(1, self.GetClientSize().width - 2 * _kBorder)
            scale = min(1.0, width / float(image.GetWidth()),
                        _kPreviewHeight / float(image.GetHeight()))
            if scale < 1:
                image = image.Scale(max(1, int(image.GetWidth() * scale)),
                                    max(1, int(image.GetHeight() * scale)),
                                    wx.IMAGE_QUALITY_HIGH)
            preview = wx.Bitmap(image)
            dc = wx.MemoryDC(preview)
            try:
                drawVideoBadge(dc, 3, max(0, preview.GetHeight() - 17))
            finally:
                dc.SelectObject(wx.NullBitmap)
            self._preview.SetBitmap(preview)
            self._previewNote.SetLabel("")
        self.Layout()


    ###########################################################
    def setDetections(self, rows, fileRow=None, busy=False):
        """Show what we have stored for the current file.

        @param  rows     Detection rows, or None if never analysed.
        @param  fileRow  The `files` row, when there is one.
        @param  busy     True while analysis is running.
        """
        self._setFaceDetections([], None)
        self._nudityDetail = [r["nudityDetail"] for r in (rows or [])
                              if r["nudity"] and r["nudityDetail"]]

        if busy:
            self._detectionText.SetLabel("Analyzing...")
            self._nudityText.Hide()
            self._analyzeButton.Enable(False)
            self.Layout()
            return

        self._analyzeButton.Enable(self._path is not None)

        if fileRow is not None and fileRow["error"]:
            self._detectionText.SetLabel(fileRow["error"])
            self._nudityText.Hide()
            self.Layout()
            return

        if rows is None:
            self._detectionText.SetLabel("Not analysed yet.")
            self._nudityText.Hide()
            self.Layout()
            return

        if fileRow is not None and fileRow["captureMs"]:
            self._infoValues["Taken"].SetLabel(
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(fileRow["captureMs"] / 1000.0)))

        self._setFaceDetections(rows, fileRow)
        hasNudity = any(r["nudity"] for r in rows)
        lines = summarizeDetections(rows)
        if not lines:
            # "Analysed and found nothing" and "not analysed" must not look
            # the same -- that distinction is the whole value of storing a
            # result.
            self._detectionText.SetLabel("" if hasNudity else "Analysed -- nothing detected.")
        else:
            self._detectionText.SetLabel("\n".join(lines))

        nudityLines = summarizeNudity(self._nudityDetail)
        if hasNudity:
            nudityLines.insert(0, "⚠ Explicit content flagged")
        self._nudityText.SetLabel("\n".join(nudityLines))
        self._nudityText.Wrap(max(120, self.GetClientSize().width - 2 * _kBorder))
        self._nudityText.Show(bool(nudityLines))
        self.Layout()


    ###########################################################
    def setStatus(self, text):
        """Set the small status line under the button.

        @param  text  Status text, or "" to clear it.
        """
        self._statusText.SetLabel(text or "")
        self.Layout()


    ###########################################################
    def setBusy(self, busy):
        """Enable/disable the Analyze button while work is in flight.

        @param  busy  True while this file is being analysed.
        """
        self._analyzeButton.Enable(bool(self._path) and not busy)
        self._faceChoice.Enable(not busy)
        self._addFaceButton.Enable(bool(self._faceRows) and not busy)


    ###########################################################
    def _setFaceDetections(self, rows, fileRow):
        """Show named and unknown faces; bind each choice to its stored detection."""
        self._faceRows = [dict(r) for r in (rows or [])
                          if r["type"] == "person" and r["faceDetConf"] is not None]
        self._faceAnalyzedMs = fileRow["analyzedMs"] if fileRow is not None else None
        labels = []
        for index, row in enumerate(self._faceRows, 1):
            label = "%d. %s (det %.0f%%)" % (
                index, row.get("faceName") or "Unknown face", row["faceDetConf"] * 100)
            if self._isVideo:
                seconds = int(row.get("atMs") or 0) // 1000
                label += " at %02d:%02d:%02d" % (seconds // 3600,
                                                (seconds // 60) % 60, seconds % 60)
            labels.append(label)
        self._faceChoice.Set(labels)
        available = bool(labels) and self._faceAnalyzedMs is not None
        if available:
            self._faceChoice.SetSelection(0)
        self._faceChoice.Show(available)
        self._faceChoice.Enable(available)
        self._addFaceButton.Show(available)
        self._addFaceButton.Enable(available)


    ###########################################################
    def _onAddFace(self, event):
        """Use the same confirmed baseline workflow as Search detections."""
        index = self._faceChoice.GetSelection()
        if not self._path or not 0 <= index < len(self._faceRows):
            return
        from frontEnd.ImageFaceEnrollment import enrollFace
        if enrollFace(self.GetTopLevelParent(), self._path, self._faceRows[index],
                      self._faceAnalyzedMs, self._logger):
            self.setStatus("Face added. Analyze this file again to update recognition.")


    ###########################################################
    def _onAnalyze(self, event):
        """Analyze was clicked.

        @param  event  The EVT_BUTTON event.
        """
        event.Skip()
        if self._path and self._analyzeCallback is not None:
            self._analyzeCallback(self._path)


    ###########################################################
    def _onUpdateRecordButton(self, event):
        """Offer View record only for a single selected file.

        @param  event  The EVT_UPDATE_UI event.
        """
        count = 1
        if self._selectionCount is not None:
            try:
                count = self._selectionCount()
            except Exception:
                count = 0
        event.Enable(bool(self._path) and count == 1)


    ###########################################################
    def _onViewRecord(self, event):
        """View record was clicked.

        @param  event  The EVT_BUTTON event.
        """
        event.Skip()
        if self._path and self._recordCallback is not None:
            self._recordCallback(self._path)


    ###########################################################
    def _stopMetadataTimer(self):
        """Stop the debounce timer, tolerating a dead window."""
        try:
            if self._metaTimer is not None:
                self._metaTimer.Stop()
        except Exception:
            pass


    ###########################################################
    def _onMetadataTimer(self, event):
        """The selection settled; read this file's metadata.

        @param  event  The EVT_TIMER event.
        """
        # Both halves of the documented guard: the timer may have been left
        # armed by a window that is now gone.
        if self._metaTimer is None or not self:
            return
        if not self._path:
            return
        self._populateMetadata(self._path)


    ###########################################################
    def _populateMetadata(self, path):
        """Fill the metadata list for one file.

        @param  path  Absolute path.
        """
        try:
            self._tags = ExifData.readTags(path)
        except Exception as e:
            self._logger.info("ImageDetailPanel: metadata read failed for "
                              "%s: %s" % (path, e))
            self._tags = {}

        self._metaList.DeleteAllItems()
        for name, value in ExifData.sortedTags(self._tags):
            row = self._metaList.InsertItem(self._metaList.GetItemCount(),
                                            ExifData.shortName(name))
            text = "" if value is None else str(value)
            # One line per row; a multi-line value would silently show only
            # its first line at a random height.
            self._metaList.SetItem(row, 1, text.replace("\n", " ")[:200])
            self._metaList.SetItemData(row, 0)

        # "Taken" is otherwise filled only from a stored analysis, so an
        # unanalysed photo showed a blank next to a metadata table that had
        # the date in it.  The file is the authority either way.
        if not self._infoValues["Taken"].GetLabel():
            for key in ("EXIF:DateTimeOriginal", "ExifIFD:DateTimeOriginal",
                        "EXIF:CreateDate", "QuickTime:CreateDate"):
                if self._tags.get(key):
                    self._infoValues["Taken"].SetLabel(str(self._tags[key]))
                    break

        if not self._tags:
            self._metaNote.SetLabel("No metadata in this file.")
        elif ExifData.canWrite():
            self._metaNote.SetLabel("Double-click a tag to edit it.")
        else:
            self._metaNote.SetLabel(ExifData.unavailableReason())
        self.Layout()


    ###########################################################
    def _onEditTag(self, event):
        """Edit one metadata tag.

        @param  event  The EVT_LIST_ITEM_ACTIVATED event.
        """
        event.Skip()
        if not self._path:
            return

        if not ExifData.canWrite():
            wx.MessageBox(ExifData.unavailableReason(), "Cannot edit",
                          wx.OK | wx.ICON_INFORMATION, self)
            return

        index = event.GetIndex()
        ordered = ExifData.sortedTags(self._tags)
        if not (0 <= index < len(ordered)):
            return
        name, value = ordered[index]

        if not ExifData.isEditable(name):
            wx.MessageBox(
                "%s is derived from other tags, so editing it here would "
                "change nothing in the file." % ExifData.shortName(name),
                "Read-only tag", wx.OK | wx.ICON_INFORMATION, self)
            return

        dialog = wx.TextEntryDialog(
            self, "New value for %s:" % name, "Edit metadata",
            "" if value is None else str(value))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            newValue = dialog.GetValue()
        finally:
            dialog.Destroy()

        if newValue == ("" if value is None else str(value)):
            return

        ok, message = ExifData.writeTags(self._path, {name: newValue})
        if not ok:
            wx.MessageBox(message, "Could not save",
                          wx.OK | wx.ICON_ERROR, self)
            return
        # Re-read rather than patching the row: ExifTool may normalise what
        # it stored, and showing the value we asked for rather than the one
        # on disk would be a small lie that compounds.
        self._populateMetadata(self._path)


    ###########################################################
    def _onDestroy(self, event):
        """Stop the timer so it cannot fire into a dead window.

        The guard matters: EVT_WINDOW_DESTROY propagates up from every child,
        so without it this runs once per control in the panel.
        """
        if event.GetEventObject() == self:
            self._stopMetadataTimer()
            self._metaTimer = None
        event.Skip()


    ###########################################################
    def Destroy(self):
        """Stop the timer before the window goes away.

        Overriding Destroy as well as handling EVT_WINDOW_DESTROY is the
        pattern HANDOFF.md 5.12 prescribes for every timer in this codebase.
        """
        self._stopMetadataTimer()
        self._metaTimer = None
        return super(ImageDetailPanel, self).Destroy()


    ###########################################################
    def _showImagePreview(self, path):
        """Decode a still at preview size and show it.

        @param  path  Absolute path of an image file.
        """
        width = max(1, self.GetClientSize().width - 2 * _kCtrlPadding)

        try:
            from PIL import Image
        except ImportError:
            self._previewNote.SetLabel("Pillow is not available.")
            return

        try:
            with Image.open(path) as img:
                origW, origH = img.size
                self._infoValues["Dimensions"].SetLabel(
                    "%d x %d" % (origW, origH))

                # draft() asks the JPEG decoder to downscale WHILE decoding,
                # so a 12 MP photo never becomes a 36 MB buffer just to draw a
                # 200 px preview.  It is a no-op for formats that cannot do
                # it, which is why thumbnail() still follows.
                img.draft('RGB', (width, _kPreviewHeight))
                img = img.convert('RGB')
                img.thumbnail((width, _kPreviewHeight),
                              Image.Resampling.LANCZOS)
                shownW, shownH = img.size
                bmp = wx.Bitmap.FromBuffer(shownW, shownH, img.tobytes())
        except Exception as e:
            # A file we cannot decode is ordinary in a folder of user files (a
            # truncated download, an unsupported variant), so it is a note,
            # not a dialog.
            self._logger.info("ImageDetailPanel: cannot preview %s: %s"
                              % (path, e))
            self._preview.SetBitmap(wx.Bitmap(1, 1))
            self._previewNote.SetLabel("Cannot preview this file.")
            self._infoValues["Dimensions"].SetLabel("--")
            return

        self._preview.SetBitmap(bmp)
        self._previewNote.SetLabel("")


    ###########################################################
    def getPath(self):
        """@return  The path currently shown, or None."""
        return self._path
