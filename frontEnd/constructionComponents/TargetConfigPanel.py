#!/usr/bin/env python

#*****************************************************************************
#
# TargetConfigPanel.py
#
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

# Python imports...
import operator
import sys

# Common 3rd-party imports...
from PIL import Image, ImageDraw
import wx

# Local imports...
from .ConfigPanel import ConfigPanel
from appCommon.CommonStrings import kTargetSettingToLabel, kTargetLabelToSetting, kTargetLabels
from appCommon.CommonStrings import kAnyCameraStr

# Constants...

_kTargetLabelStr = "Look for:"

_kFaceListLabelStr = "Which faces:"
_kFaceListHintStr = "None checked = any face.  \"Unknown\" = face seen but not recognized."
_kUnknownFaceLabel = "Unknown"

_kWantMinSizeStr = "Ignore objects smaller than:"
_kMinSizeValues = [ 30, 40, 50, 60, 70 ]
_kMinSizeStrs = [
    "%d pixels" % (val) for val in _kMinSizeValues
]

_kShowMinSizeLabelStr = "Show how tall this is in the image above"

# Travel filter: how far the object's centre moved over its life, in pixels at
# 1280x720 (see backEnd/triggers/MinTravelTrigger.py).  This catches what the
# size filter cannot -- something flickering in place at the edge of frame is the
# same SIZE as a real subject at distance, so only movement separates them.
#
# The slider runs to 500 rather than stopping low, because the useful range is
# wider than it looks: measured over 16,215 recorded detections, 40 still shows
# 66% of them and 80 shows 57%.  The count label beside it is what makes the
# number meaningful -- a bare pixel figure tells you nothing about what it hides.
_kWantMinTravelStr = "Ignore detections that barely move:"
_kMinTravelMax = 500
_kMinTravelCountFmt = "showing %s of %s detections on %s"
_kMinTravelCountAllCams = "all cameras"
_kMinTravelCountUnknown = "(count unavailable)"
_kMinTravelHintStr = \
    "How far the object moved, not how big it was.  Higher hides more."

_kOverlayImageWidth = 25
_kOverlayBackgroundColor = (255, 255, 255, 127)
_kOverlayLineColor = (0, 204, 0, 127)
_kOverlayLineWidth = 3


##############################################################################
class TargetConfigPanel(ConfigPanel):
    """The block configuration panel for a camera."""

    ###########################################################
    def __init__(self, parent, videoWindow, targetBlockDataModel,
                 backEndClient=None, videoSourceBlockDataModel=None):
        """TargetConfigPanel constructor.

        @param  parent                Our parent UI element.
        @param  videoWindow           The videoWindow.
        @param  targetBlockDataModel  The data model for this target block.
        @param  backEndClient         Client to the back end (used to list the
                                      enrolled face names for the "Faces"
                                      target); may be None.
        @param  videoSourceBlockDataModel
                                      The query's camera block, so the movement
                                      filter's detection count can be scoped to
                                      the camera the rule is actually about; may
                                      be None, in which case it counts them all.
        """
        # Call our super
        super(TargetConfigPanel, self).__init__(parent)

        # Keep track of params...
        self._videoWindow = videoWindow
        self._targetBlockDataModel = targetBlockDataModel
        self._backEndClient = backEndClient
        self._videoSourceBlockDataModel = videoSourceBlockDataModel
        self._faceNamesLoaded = False

        # Create our UI elements...

        # Create the target choice.  Curr select will be set from model later.
        targetLabel = wx.StaticText(self, -1, _kTargetLabelStr)
        self._targetChoice = wx.Choice(self, -1, choices=kTargetLabels)
        self._targetChoice.Bind(wx.EVT_CHOICE, self.OnTargetChoice)

        # Face-name picker, only shown when the "Faces" target is selected.
        self._faceListLabel = wx.StaticText(self, -1, _kFaceListLabelStr)
        self._faceListBox = wx.CheckListBox(self, -1, choices=[])
        self._faceListBox.SetMinSize((-1, 90))
        self._faceListBox.Bind(wx.EVT_CHECKLISTBOX, self.OnFaceNamesCheck)
        self._faceListHint = wx.StaticText(self, -1, _kFaceListHintStr)
        self._faceListHint.SetForegroundColour(wx.Colour(120, 120, 120))

        self._wantMinSizeCheckbox = wx.CheckBox(self, -1, _kWantMinSizeStr)
        self._wantMinSizeCheckbox.Bind(wx.EVT_CHECKBOX,
                                       self.OnWantMinSizeCheckbox)
        self._minSizeChoice = wx.Choice(self, -1, choices=_kMinSizeStrs)
        self._minSizeChoice.Bind(wx.EVT_CHOICE, self.OnMinSizeChoice)

        self._showMinSizeCheckbox = wx.CheckBox(self, -1, _kShowMinSizeLabelStr)
        self._showMinSizeCheckbox.Bind(wx.EVT_CHECKBOX, self.OnShowMinSizeCheckbox)

        self._wantMinTravelCheckbox = wx.CheckBox(self, -1, _kWantMinTravelStr)
        self._wantMinTravelCheckbox.Bind(wx.EVT_CHECKBOX,
                                         self.OnWantMinTravelCheckbox)
        self._minTravelSlider = wx.Slider(self, -1, 0, 0, _kMinTravelMax,
                                          style=wx.SL_HORIZONTAL)
        # Both events: EVT_SLIDER alone does not fire while dragging on all
        # platforms, and the whole point of the readout is to update as you drag.
        self._minTravelSlider.Bind(wx.EVT_SLIDER, self.OnMinTravelSlider)
        self._minTravelSlider.Bind(wx.EVT_SCROLL, self.OnMinTravelSlider)
        self._minTravelValue = wx.StaticText(self, -1, "")
        self._minTravelCount = wx.StaticText(self, -1, "")
        self._minTravelCount.SetForegroundColour(wx.Colour(120, 120, 120))
        self._minTravelHint = wx.StaticText(self, -1, _kMinTravelHintStr)
        self._minTravelHint.SetForegroundColour(wx.Colour(120, 120, 120))

        # Throw our stuff into our sizer...
        targetSizer = wx.BoxSizer(wx.HORIZONTAL)
        targetSizer.Add(targetLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        targetSizer.Add(self._targetChoice, 1, wx.ALIGN_CENTER_VERTICAL)

        minSizeSizer = wx.BoxSizer(wx.HORIZONTAL)
        minSizeSizer.Add(self._wantMinSizeCheckbox, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        minSizeSizer.Add(self._minSizeChoice, 1, wx.ALIGN_CENTER_VERTICAL)

        showMinSizeSizer = wx.BoxSizer()
        showMinSizeSizer.Add(self._showMinSizeCheckbox, 0, wx.LEFT, 15)

        minTravelSizer = wx.BoxSizer(wx.HORIZONTAL)
        minTravelSizer.Add(self._minTravelSlider, 1,
                           wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 15)
        minTravelSizer.Add(self._minTravelValue, 0,
                           wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)

        # Use a border sizer to give a little space
        borderSizer = wx.BoxSizer(wx.VERTICAL)
        borderSizer.Add(targetSizer, 0, wx.EXPAND | wx.TOP, 15)
        borderSizer.Add(self._faceListLabel, 0, wx.TOP, 8)
        borderSizer.Add(self._faceListBox, 0, wx.EXPAND | wx.TOP, 2)
        borderSizer.Add(self._faceListHint, 0, wx.TOP, 2)
        borderSizer.Add(minSizeSizer, 0, wx.EXPAND | wx.TOP, 10)
        borderSizer.Add(showMinSizeSizer, 0, wx.EXPAND | wx.TOP, 5)
        borderSizer.Add(self._wantMinTravelCheckbox, 0, wx.TOP, 10)
        borderSizer.Add(minTravelSizer, 0, wx.EXPAND | wx.TOP, 2)
        borderSizer.Add(self._minTravelCount, 0, wx.LEFT | wx.TOP, 15)
        borderSizer.Add(self._minTravelHint, 0, wx.LEFT | wx.TOP, 15)
        self.SetSizer(borderSizer)

        # Listen for changes.
        self._targetBlockDataModel.addListener(self._updateFromModels)
        # Follow the camera too: the detection count beside the movement slider
        # is scoped to it, so changing camera in this dialog has to re-count.
        if self._videoSourceBlockDataModel is not None:
            self._videoSourceBlockDataModel.addListener(
                self._handleVideoSourceChange)

        # Update everything...
        self._updateFromModels()


    ###########################################################
    def getIcon(self):
        """Return the path to the bitmap associated with this panel.

        @return bmpPath  The path to the bitmap.
        """
        return "frontEnd/bmps/Block_Icon_Look_For.png"


    ###########################################################
    def getTitle(self):
        """Return the title associated with this panel.

        @return title  The title
        """
        return "Look for"


    ###########################################################
    def activate(self):
        """Set this panel as the active one."""
        self._updateFromModels(None)


    ###########################################################
    def deactivate(self):
        """Called before another panel gets activate."""
        self._videoWindow.setOverlayImage(None)


    ###########################################################
    def _updateFromModels(self, modelThatChanged=None):
        """Update all of our settings based on our data models.

        @param  modelThatChanged  The model that changed (ignored).
        """
        _ = modelThatChanged

        targetSetting = self._targetBlockDataModel.getTargetName()
        target = kTargetSettingToLabel[targetSetting]
        self._targetChoice.SetStringSelection(target)

        # Show the face-name picker only for the "Faces" target...
        isFace = (targetSetting == 'face')
        if isFace:
            self._ensureFaceNamesLoaded()
            wanted = self._targetBlockDataModel.getFaceNames()
            # Make sure every stored name is present in the list (a person may
            # have been un-enrolled since the rule was saved) so the checks
            # reflect the model truthfully.
            items = list(self._faceListBox.GetStrings())
            missing = [n for n in wanted if n not in items]
            if missing:
                self._faceListBox.Set(items + missing)
            self._faceListBox.SetCheckedStrings(wanted)
        if self._faceListLabel.IsShown() != isFace:
            self._faceListLabel.Show(isFace)
            self._faceListBox.Show(isFace)
            self._faceListHint.Show(isFace)
            self.Layout()

        minSize = self._targetBlockDataModel.getMinSize()
        try:
            minSizeSelection = _kMinSizeValues.index(minSize)
            self._minSizeChoice.SetSelection(minSizeSelection)
        except ValueError:
            pass

        wantMinSize = self._targetBlockDataModel.getWantMinSize()
        if not wantMinSize:
            self._wantMinSizeCheckbox.SetValue(0)
            self._minSizeChoice.Enable(False)
            self._showMinSizeCheckbox.SetValue(0)
            self._showMinSizeCheckbox.Enable(False)
            self._videoWindow.setOverlayImage(None)
        else:
            self._wantMinSizeCheckbox.SetValue(1)
            self._minSizeChoice.Enable(True)

            self._showMinSizeCheckbox.Enable(True)
            showMinSize = self._targetBlockDataModel.getShowMinSize()
            self._showMinSizeCheckbox.SetValue(int(showMinSize))
            if showMinSize:
                overlayImage = self._makeOverlayImage(minSize)
                self._videoWindow.setOverlayImage(overlayImage)
            else:
                self._videoWindow.setOverlayImage(None)

        wantMinTravel = self._targetBlockDataModel.getWantMinTravel()
        self._wantMinTravelCheckbox.SetValue(int(bool(wantMinTravel)))
        self._minTravelSlider.SetValue(
            min(_kMinTravelMax, max(0, self._targetBlockDataModel.getMinTravel())))
        self._minTravelSlider.Enable(bool(wantMinTravel))
        self._updateMinTravelLabels()


    ###########################################################
    def _handleVideoSourceChange(self, videoSourceBlockDataModel=None):
        """The rule's camera changed; re-scope the movement count to it."""
        self._updateMinTravelLabels()


    ###########################################################
    def _updateMinTravelLabels(self):
        """Refresh the travel value and the "showing N of M" readout."""
        value = self._minTravelSlider.GetValue()
        self._minTravelValue.SetLabel("%d px" % value)

        if not self._wantMinTravelCheckbox.GetValue():
            self._minTravelCount.SetLabel("")
            self.Layout()
            return

        matching, total = self._countForTravel(value)
        if matching is None:
            # Pre-migration database, or no data manager to ask.  Say so rather
            # than showing a number that would be wrong.
            self._minTravelCount.SetLabel(_kMinTravelCountUnknown)
        else:
            # Name the scope: the same threshold hides wildly different
            # proportions on a quiet indoor camera and a windy hedge, so a
            # bare count would invite reading it as fleet-wide.
            cams = self._countCameras()
            self._minTravelCount.SetLabel(
                _kMinTravelCountFmt % ("{:,}".format(matching),
                                       "{:,}".format(total),
                                       cams[0] if cams else _kMinTravelCountAllCams))
        self.Layout()


    ###########################################################
    def _countForTravel(self, value):
        """-> (matching, total) recorded detections at this threshold.

        The pixel figure on its own says nothing about what it hides; this is
        what turns the slider into something you can set deliberately.  Scoped to
        the camera this rule is about, since a fleet-wide number says little
        about a threshold you are choosing for one view -- a quiet indoor camera
        and a windy hedge have very different movement profiles.

        Counts every recorded detection on that camera rather than applying the
        query's other conditions, so the denominator stays stable as the slider
        moves and the ratio means "of everything this camera saw".
        """
        dataMgr = getattr(self.GetTopLevelParent(), '_dataMgr', None)
        if dataMgr is None:
            return (None, None)
        try:
            # The threshold is quoted at 1280x720 while the stored columns are in
            # analysis pixels, so halve it for the usual 640x360 camera.  This is
            # an indicator, not the filter itself -- MinTravelTrigger does the
            # real per-camera scaling at search time.
            return dataMgr.countObjectsByTravel(int(round(value / 2.0)),
                                                self._countCameras())
        except Exception:
            return (None, None)


    ###########################################################
    def _countCameras(self):
        """-> [camLoc] to count over, or None for every camera."""
        model = self._videoSourceBlockDataModel
        if model is None:
            return None
        try:
            name = model.getLocationName()
        except Exception:
            return None
        # "Any camera" is a placeholder, not a location, so count them all.
        if not name or name == kAnyCameraStr:
            return None
        return [name]


    ###########################################################
    def _makeOverlayImage(self, height):
        """Make an image showing how tall the "height" is.

        @param  height  The height to show.
        @return img     A PIL Image.
        """
        img = Image.new('RGBA', (_kOverlayImageWidth, height), 0)
        imgDraw = ImageDraw.Draw(img)

        imgDraw.rectangle((_kOverlayLineWidth, _kOverlayLineWidth,
                           _kOverlayImageWidth - _kOverlayLineWidth,
                           height - _kOverlayLineWidth),
                          fill=_kOverlayBackgroundColor)
        for i in range(_kOverlayLineWidth):
            imgDraw.rectangle((i, i, _kOverlayImageWidth-i-1, height-i-1),
                              outline=_kOverlayLineColor)

        label = str(height)
        try:
            labelWidth, labelHeight = imgDraw.textsize(label)
        except AttributeError:
            bbox = imgDraw.textbbox((0, 0), label)
            labelWidth, labelHeight = bbox[2] - bbox[0], bbox[3] - bbox[1]
        imgDraw.text(((_kOverlayImageWidth-labelWidth)//2,
                      (height-labelHeight)//2), label, fill="black")

        return img


    ###########################################################
    def _ensureFaceNamesLoaded(self):
        """Populate the face checklist from the enrolled people (once)."""
        if self._faceNamesLoaded:
            return
        self._faceNamesLoaded = True

        names = []
        try:
            if self._backEndClient is not None:
                people = self._backEndClient.getBaselinePeople()
                names = [p.get('name', '') for p in people if p.get('name')]
        except Exception:
            names = []

        # "Unknown" first, then enrolled names, de-duplicated case-insensitively.
        seen = set()
        items = []
        for name in [_kUnknownFaceLabel] + names:
            if name.lower() not in seen:
                seen.add(name.lower())
                items.append(name)
        self._faceListBox.Set(items)


    ###########################################################
    def OnFaceNamesCheck(self, event=None):
        """Handle the user (un)checking face names.

        @param  event  The checklist event (ignored)
        """
        self._targetBlockDataModel.setFaceNames(
            list(self._faceListBox.GetCheckedStrings()))


    ###########################################################
    def OnTargetChoice(self, event=None):
        """Handle when the user changes targets.

        @param  event  The choice event (ignored)
        """
        target = self._targetChoice.GetStringSelection()
        target = kTargetLabelToSetting[target]
        self._targetBlockDataModel.setTargetName(target)


    ###########################################################
    def OnWantMinTravelCheckbox(self, event=None):
        """Handle when the user toggles the travel filter.

        @param  event  The checkbox event (ignored)
        """
        wantMinTravel = bool(self._wantMinTravelCheckbox.GetValue())
        self._targetBlockDataModel.setWantMinTravel(wantMinTravel)
        self._minTravelSlider.Enable(wantMinTravel)
        self._updateMinTravelLabels()


    ###########################################################
    def OnMinTravelSlider(self, event=None):
        """Handle when the user drags the travel slider.

        @param  event  The slider event (ignored)
        """
        value = self._minTravelSlider.GetValue()
        self._targetBlockDataModel.setMinTravel(value)
        self._updateMinTravelLabels()
        if event is not None:
            event.Skip()


    ###########################################################
    def OnWantMinSizeCheckbox(self, event=None):
        """Handle when the user changes "want min size" checkbox.

        @param  event  The choice event (ignored)
        """
        wantMinSize = self._wantMinSizeCheckbox.GetValue()
        self._targetBlockDataModel.setWantMinSize(bool(wantMinSize))


    ###########################################################
    def OnMinSizeChoice(self, event=None):
        """Handle when the user changes min size.

        @param  event  The choice event (ignored)
        """
        minSizeIndex = self._minSizeChoice.GetSelection()
        if minSizeIndex != -1:
            self._targetBlockDataModel.setMinSize(_kMinSizeValues[minSizeIndex])
        else:
            assert False, "Bad min size choice"


    ###########################################################
    def OnShowMinSizeCheckbox(self, event=None):
        """Handle when the user changes "show min size" checkbox.

        @param  event  The choice event (ignored)
        """
        showMinSize = self._showMinSizeCheckbox.GetValue()
        self._targetBlockDataModel.setShowMinSize(bool(showMinSize))


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    print("NO TESTS")


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_main()
    else:
        print("Try calling with 'test' as the argument.")
