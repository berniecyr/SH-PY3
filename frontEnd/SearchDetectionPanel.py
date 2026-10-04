#!/usr/bin/env python

#*****************************************************************************
#
# SearchDetectionPanel.py
#
#*****************************************************************************

import wx
from wx.lib.stattext import GenStaticText

from appCommon.CommonStrings import kFrontEndLogName
from vitaToolbox.wx.AppColors import getAppBackgroundColour
from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from .EnrollFacePreviewDialog import EnrollFacePreviewDialog
from .ClipRecordDialog import ClipRecordDialog


_kPanelHeight = 110

_kColBlue   = wx.Colour(0, 100, 190)
_kColOrange = wx.Colour(190, 80, 0)
_kColGray   = wx.Colour(100, 100, 100)

if wx.Platform == '__WXMSW__':
    _kFontSize = 9
else:
    _kFontSize = 11


def mergeNudityDetail(detail, scores, extras):
    """Merge one 'CLASS=score,...' string, keeping the best score per class.

    A clip can hold several people, each carrying their own classes, and a
    track's own detail can name several classes at once.  We used to keep only
    the first object's string, which hid every other object's classes -- a
    genitalia hit on one person was masked by a breast hit on another.

    @param  detail  Stored nudityDetail string, or None.
    @param  scores  Dict of class -> best score; updated in place.
    @param  extras  List of unparseable fragments; updated in place.
    """
    for part in (detail or "").split(","):
        if "=" not in part:
            # Unparseable fragment; preserved verbatim, as it always was.
            part = part.strip()
            if part and part not in extras:
                extras.append(part)
            continue
        cls, score = part.split("=", 1)
        cls = cls.strip()
        try:
            score = float(score)
        except ValueError:
            continue
        if cls and score > scores.get(cls, -1.0):
            scores[cls] = score


def formatNudityDetail(scores, extras):
    """Render merged nudity classes for display, highest score first.

    @param  scores  Dict of class -> best score.
    @param  extras  List of unparseable fragments, appended verbatim.
    @return text    'CLASS (0.77), CLASS (0.30)', or '' if nothing to show.
    """
    parts = ["%s (%.2f)" % (cls, score) for (cls, score) in
             sorted(scores.items(), key=lambda kv: -kv[1])]
    parts.extend(extras)
    return ", ".join(parts)


class SearchDetectionPanel(wx.Panel):
    """Panel shown below the search playback area displaying detection results.

    Populates when the user selects a clip that has face recognition or
    nudity detection data stored in the objects table.
    """

    def __init__(self, parent, dataMgr, resultsModel, backEndClient=None):
        super(SearchDetectionPanel, self).__init__(
            parent, style=wx.BORDER_NONE
        )
        self._logger = getLogger(kFrontEndLogName)
        self._dataMgr = dataMgr
        self._resultsModel = resultsModel
        self._backEndClient = backEndClient
        self._smallFont = None      # set below; reused for clickable entries

        self.SetMinSize((-1, _kPanelHeight))
        # Follows the user's chosen colour (Options -> Colors); this used to be
        # a hardcoded near-white that ignored it.
        self.SetBackgroundColour(getAppBackgroundColour())

        # ── Layout ────────────────────────────────────────────────────────────
        outerSizer = wx.BoxSizer(wx.VERTICAL)
        outerSizer.Add(wx.StaticLine(self), 0, wx.EXPAND)

        innerSizer = wx.BoxSizer(wx.HORIZONTAL)
        outerSizer.Add(innerSizer, 1, wx.EXPAND | wx.ALL, 6)

        # Left column: header
        leftSizer = wx.BoxSizer(wx.VERTICAL)
        headerFont = wx.Font(
            _kFontSize, wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD
        )
        self._headerLabel = wx.StaticText(self, label="Detections")
        self._headerLabel.SetFont(headerFont)
        self._headerLabel.SetForegroundColour(_kColGray)
        leftSizer.Add(self._headerLabel, 0)

        # "View record": the panel stays a summary, everything the pipeline
        # stored (plus a JSON export) lives one click away.
        self._recordLink = GenStaticText(self, label="\U0001F453 View record")
        self._recordLink.SetForegroundColour(_kColBlue)
        self._recordLink.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._recordLink.SetToolTip(
            "Show everything stored about this clip's detections")
        self._recordLink.Bind(wx.EVT_LEFT_UP, self._onViewRecord)
        leftSizer.Add(self._recordLink, 0, wx.TOP, 6)

        innerSizer.Add(leftSizer, 0, wx.RIGHT, 12)

        # Right column: detail lines
        rightSizer = wx.BoxSizer(wx.VERTICAL)

        smallFont = wx.Font(
            _kFontSize, wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL
        )
        self._smallFont = smallFont

        self._facesLabel       = wx.StaticText(self, label="")
        self._facesDetailHeader = wx.StaticText(self, label="Faces:")
        self._objectsLabel     = wx.StaticText(self, label="")
        self._nudityLabel      = wx.StaticText(self, label="")
        self._emptyLabel       = wx.StaticText(self, label="Select a clip to see detection data.")

        # The explicit-content marker stays deliberately quiet: just the
        # triangle, with the classes and confidences behind a click.  Someone
        # scrubbing footage with other people around shouldn't have that
        # detail on screen unless they ask for it.
        self._nudityToggle = GenStaticText(self, label="⚠")
        self._nudityToggle.SetForegroundColour(_kColOrange)
        self._nudityToggle.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._nudityToggle.Bind(wx.EVT_LEFT_UP, self._onNudityToggle)
        self._nudityDetailText = ""     # revealed text for the current clip
        self._nudityExpanded   = False

        # Current clip context, for the record dialog.
        self._clipCamLoc  = None
        self._clipStartMs = None
        self._clipStopMs  = None
        self._clipObjList = []

        for lbl in (self._facesLabel, self._facesDetailHeader,
                    self._objectsLabel, self._nudityLabel, self._emptyLabel,
                    self._nudityToggle, self._recordLink):
            lbl.SetFont(smallFont)

        self._facesLabel.SetForegroundColour(_kColBlue)
        self._facesDetailHeader.SetForegroundColour(_kColGray)
        self._nudityLabel.SetForegroundColour(_kColOrange)
        self._objectsLabel.SetForegroundColour(_kColGray)
        self._emptyLabel.SetForegroundColour(_kColGray)

        # Clickable, per-detection face entries live here (rebuilt each clip).
        self._facesEntriesSizer = wx.BoxSizer(wx.VERTICAL)

        # Triangle and its (hidden) detail sit on one row so the text appears
        # beside the marker rather than jumping to its own line.
        self._nudityRow = wx.BoxSizer(wx.HORIZONTAL)
        self._nudityRow.Add(self._nudityToggle, 0, wx.ALIGN_CENTER_VERTICAL)
        self._nudityRow.Add(self._nudityLabel, 0,
                            wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

        rightSizer.Add(self._facesLabel,        0, wx.BOTTOM, 2)
        rightSizer.Add(self._facesDetailHeader, 0, wx.BOTTOM, 1)
        rightSizer.Add(self._facesEntriesSizer, 0, wx.LEFT | wx.BOTTOM, 12)
        rightSizer.Add(self._nudityRow,         0, wx.BOTTOM, 2)
        rightSizer.Add(self._objectsLabel,      0, wx.BOTTOM, 2)
        rightSizer.Add(self._emptyLabel,        0)
        innerSizer.Add(rightSizer, 1, wx.EXPAND)

        self.SetSizer(outerSizer)

        # ── Model listeners ───────────────────────────────────────────────────
        self._resultsModel.addListener(self._handleClipChange, False, 'videoSegment')
        self._resultsModel.addListener(self._handleResults,    False, 'results')
        self._resultsModel.addListener(self._handleSearching,  False, 'searching')

        self._showEmpty()

    # ── Model handlers ────────────────────────────────────────────────────────

    def _handleSearching(self, resultsModel):
        self._showEmpty()

    def _handleResults(self, resultsModel):
        # Results just arrived.  SearchResultsList selects the auto-chosen clip
        # with a bare SetSelection() call (no model update), so 'videoSegment'
        # never fires.  Any valid integer clipNum >= 0 means a clip was chosen;
        # update the panel directly.
        clipNum = resultsModel.getCurrentClipNum()
        if clipNum >= 0 and clipNum == int(clipNum):
            self._handleClipChange(resultsModel)
        else:
            self._showEmpty()

    def _handleClipChange(self, resultsModel):
        clipNum = resultsModel.getCurrentClipNum()

        # Float means "between clips" — clear the panel
        if clipNum != int(clipNum) or clipNum < 0:
            self._showEmpty()
            return

        clips = resultsModel.getMatchingClips()
        idx = int(clipNum)
        if not clips or idx >= len(clips):
            self._showEmpty()
            return

        clip = clips[idx]
        # Kept for the record dialog, which needs the clip's own context and
        # not just the object ids.
        self._clipCamLoc = getattr(clip, 'camLoc', None)
        self._clipStartMs = getattr(clip, 'startTime', None)
        self._clipStopMs = getattr(clip, 'stopTime', None)
        self._clipObjList = list(clip.objList or [])
        self._populate(clip.objList)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _populate(self, objList):
        """Query object types and refresh the display labels."""
        faceNames    = []
        faceEntries  = []     # (objId, text, faceName, gender) — clickable
        numPeople    = 0
        numPets      = 0
        numVehicles  = 0
        hasNudity    = False
        nudityScores = {}     # class -> best score across every object in clip
        nudityExtras = []     # detail fragments with no '=' (shown verbatim)

        try:
            objTypes = self._dataMgr.getObjectTypes(objList) if objList else {}
            objAttrs = self._dataMgr.getObjectAttributes(objList) if objList else {}
        except Exception:
            self._logger.warning("SearchDetectionPanel: query failed",
                                 exc_info=True)
            self._showEmpty()
            return

        animalParts = []
        vehicleParts = []

        def _subLabel(attr):
            """'dog (76%)' from subtype + detConf, or '' if no subtype."""
            sub = attr.get('subType')
            if not sub:
                return None
            dc = attr.get('detConf')
            if dc is not None:
                return "%s (%d%%)" % (sub, int(round(dc * 100)))
            return sub

        def _demo(attr):
            """Build a 'M, ~34, id 78%, det 95%' demographics fragment."""
            demo = []
            if attr['gender']:
                demo.append(attr['gender'])
            if attr['age'] is not None:
                demo.append("~%d" % attr['age'])
            if attr['faceConf'] is not None:
                demo.append("id %d%%" % int(round(attr['faceConf'] * 100)))
            if attr['faceDetConf'] is not None:
                demo.append("det %d%%" % int(round(attr['faceDetConf'] * 100)))
            return demo

        for objId in objList:
            t = objTypes.get(objId, 'unknown')
            attr = objAttrs.get(objId)

            if t == "person":
                numPeople += 1
            elif t == "animal":
                numPets += 1
                sl = _subLabel(attr) if attr else None
                if sl and sl not in animalParts:
                    animalParts.append(sl)
            elif t == "vehicle":
                numVehicles += 1
                sl = _subLabel(attr) if attr else None
                if sl and sl not in vehicleParts:
                    vehicleParts.append(sl)

            if not attr:
                continue
            gender = attr.get('gender') or ""
            if attr['faceName'] and attr['faceName'] not in faceNames:
                faceNames.append(attr['faceName'])
                demo = _demo(attr)
                text = ("%s (%s)" % (attr['faceName'], ", ".join(demo))
                        if demo else attr['faceName'])
                faceEntries.append((objId, text, attr['faceName'], gender))
            elif not attr['faceName'] and (attr['gender'] or
                                           attr['age'] is not None):
                faceEntries.append((objId,
                                    "Unknown (%s)" % ", ".join(_demo(attr)),
                                    None, gender))
            if attr['nudity']:
                hasNudity = True
                mergeNudityDetail(attr['nudityDetail'],
                                  nudityScores, nudityExtras)

        hasFaces = bool(faceNames)
        hasObjects = numPeople or numPets or numVehicles

        # The record link stays available even when the summary has nothing
        # to show -- "nothing recognized" is itself worth being able to see.
        self._recordLink.Show(bool(self._clipObjList))

        if not hasFaces and not hasNudity and not hasObjects and not faceEntries:
            self._showEmpty("No detection data for this clip.",
                            keepRecordLink=True)
            return

        # ── Faces line ────────────────────────────────────────────────────────
        if hasFaces:
            self._facesLabel.SetLabel("Face ID: " + ", ".join(faceNames))
            self._facesLabel.Show()
        else:
            self._facesLabel.Hide()

        # ── Per-detection face entries (clickable → add to baseline) ────────────
        self._clearFaceEntries()
        if faceEntries:
            self._facesDetailHeader.Show()
            for (oid, text, fname, gender) in faceEntries:
                self._addFaceEntry(oid, text, fname, gender)
        else:
            self._facesDetailHeader.Hide()

        # ── Nudity line ───────────────────────────────────────────────────────
        if hasNudity:
            self._nudityDetailText = \
                formatNudityDetail(nudityScores, nudityExtras) or \
                "Nudity detected"
            # Always start collapsed: a new clip re-hides the detail.
            self._setNudityExpanded(False)
            self._nudityToggle.Show()
        else:
            self._nudityToggle.Hide()
            self._nudityLabel.Hide()

        # ── Objects summary line ───────────────────────────────────────────────
        objParts = []
        if numPeople:
            objParts.append("%d person%s" % (numPeople, "s" if numPeople > 1 else ""))
        if numPets:
            # Prefer specific subtypes (dog (76%)) over a generic count.
            if animalParts:
                objParts.append(", ".join(animalParts))
            else:
                objParts.append("%d animal%s" % (numPets, "s" if numPets > 1 else ""))
        if numVehicles:
            if vehicleParts:
                objParts.append(", ".join(vehicleParts))
            else:
                objParts.append("%d vehicle%s" % (numVehicles, "s" if numVehicles > 1 else ""))
        if objParts:
            self._objectsLabel.SetLabel("Objects: " + ", ".join(objParts))
            self._objectsLabel.Show()
        else:
            self._objectsLabel.Hide()

        self._emptyLabel.Hide()
        self.Layout()

    def _showEmpty(self, message=None, keepRecordLink=False):
        self._facesLabel.Hide()
        self._facesDetailHeader.Hide()
        self._clearFaceEntries()
        self._nudityToggle.Hide()
        self._nudityLabel.Hide()
        self._nudityExpanded = False
        if not keepRecordLink:
            self._recordLink.Hide()
        self._objectsLabel.Hide()
        self._emptyLabel.SetLabel(
            message if message else "Select a clip to see detection data."
        )
        self._emptyLabel.Show()
        self.Layout()

    # ── Explicit-content marker ─────────────────────────────────────────────

    def _setNudityExpanded(self, expanded):
        """Show or hide the class/confidence detail behind the triangle."""
        self._nudityExpanded = expanded
        if expanded:
            self._nudityLabel.SetLabel(self._nudityDetailText)
            self._nudityLabel.Show()
            self._nudityToggle.SetToolTip("Click to hide the details")
        else:
            self._nudityLabel.Hide()
            self._nudityToggle.SetToolTip(
                "Explicit content detected - click to show what and how sure")
        self.Layout()

    def _onNudityToggle(self, event):
        self._setNudityExpanded(not self._nudityExpanded)

    # ── Full record ─────────────────────────────────────────────────────────

    def _onViewRecord(self, event):
        """Open the full stored record for the selected clip."""
        if not self._clipObjList:
            return
        busy = wx.BusyCursor()
        try:
            dlg = ClipRecordDialog(self.GetTopLevelParent(), self._dataMgr,
                                   self._clipObjList, self._clipCamLoc,
                                   self._clipStartMs, self._clipStopMs)
        except Exception:
            self._logger.error("could not build the detection record",
                               exc_info=True)
            del busy
            wx.MessageBox("Could not read the detection record for this clip.",
                          "Detection record", wx.OK | wx.ICON_ERROR,
                          self.GetTopLevelParent())
            return
        del busy
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    # ── Clickable face entries ──────────────────────────────────────────────

    def _clearFaceEntries(self):
        """Remove any previously rendered clickable face entries."""
        self._facesEntriesSizer.Clear(delete_windows=True)

    def _addFaceEntry(self, objId, text, faceName, gender):
        """Add one clickable face entry that enrolls this detection on click."""
        # GenStaticText reliably receives mouse events on all platforms
        # (native wx.StaticText does not on MSW).
        lbl = GenStaticText(self, label=text)
        lbl.SetFont(self._smallFont)
        lbl.SetForegroundColour(_kColBlue)
        lbl.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        lbl.SetToolTip("Click to add this face to the recognition baseline")
        lbl.Bind(wx.EVT_LEFT_UP,
                 lambda evt, o=objId, n=faceName, g=gender:
                 self._onFaceClick(o, n, g))
        self._facesEntriesSizer.Add(lbl, 0, wx.BOTTOM, 1)

    def _onFaceClick(self, objId, faceName, gender):
        """Harvest, preview and enroll this detection's face."""
        if self._backEndClient is None:
            return

        busy = wx.BusyCursor()
        try:
            harvest = self._backEndClient.harvestFaceFromObject(objId)
        except Exception:
            self._logger.error("harvestFaceFromObject failed", exc_info=True)
            harvest = {"error": "could not reach the back end"}
        finally:
            del busy

        if not harvest or not harvest.get("ok") or \
                not harvest.get("candidates"):
            wx.MessageBox(
                "Could not find a face to add:\n\n%s" %
                (harvest or {}).get("error", "no usable face found"),
                "No face found", wx.OK | wx.ICON_INFORMATION,
                self.GetTopLevelParent())
            return

        try:
            people = self._backEndClient.getBaselinePeople() or []
        except Exception:
            people = []
        names = [p.get("name", "") for p in people if p.get("name")]

        dlg = EnrollFacePreviewDialog(self.GetTopLevelParent(),
                                      harvest["candidates"], names,
                                      prefillName=faceName or "",
                                      prefillGender=gender or "")
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.getName()
            newGender = dlg.getGender()
            selected = dlg.getSelectedIndices()
        finally:
            dlg.Destroy()

        if not name or not selected:
            return

        busy = wx.BusyCursor()
        try:
            result = self._backEndClient.commitFaceHarvest(
                harvest["token"], selected, name, newGender)
        except Exception:
            self._logger.error("commitFaceHarvest failed", exc_info=True)
            result = {"error": "could not reach the back end"}
        finally:
            del busy

        if result and result.get("ok"):
            wx.MessageBox(
                "Added %d face image%s for %s to the baseline.\n\n"
                "Cameras will start recognizing this face within a few "
                "seconds — no restart needed." % (
                    result.get("added", 0),
                    "" if result.get("added") == 1 else "s",
                    result.get("name", name)),
                "Face added", wx.OK | wx.ICON_INFORMATION,
                self.GetTopLevelParent())
        else:
            msg = (result or {}).get("error", "Enrollment failed.")
            wx.MessageBox(
                "Could not add this face:\n\n%s" % msg,
                "Face not added", wx.OK | wx.ICON_ERROR,
                self.GetTopLevelParent())
