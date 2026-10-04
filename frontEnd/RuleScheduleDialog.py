#!/usr/bin/env python

#*****************************************************************************
#
# RuleScheduleDialog.py
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
import copy

# Common 3rd-party imports...
import wx

# Local imports...
from backEnd.RealTimeRule import _kDefaultRuleSchedule
from vitaToolbox.dictUtils.OrderedDict import OrderedDict
from vitaToolbox.wx.FixedTimeCtrl import FixedTimeCtrl, EVT_TIMEUPDATE

# Globals...
_kPaddingSize = 8


###############################################################
class RuleScheduleDialog(wx.Dialog):
    """A dialog for editing real time rule schedules."""
    ###########################################################
    def __init__(self, parent, ruleName, backEndClient, use24Hour):
        """Initializer for RuleScheduleDialog.

        @param  parent         The parent window.
        @param  ruleName       The identifier for the rule.
        @param  backEndClient  An object for communicating with the back end.
        @param  use24Hour      Kept for API compatibility; time is shown in 24hr.
        """
        wx.Dialog.__init__(self, parent, -1, "Schedule for Rules")

        try:
            self._doInit(ruleName, backEndClient)
        except: # All exceptions, not just Exception subclasses
            self.Destroy()
            raise


    ###########################################################
    def _doInit(self, ruleName, backEndClient):
        """Actual init code; see __init__() for details."""
        self._backEndClient = backEndClient
        self._ruleName = ruleName
        _kPad = _kPaddingSize

        # Load existing schedule or fall back to defaults.
        self._schedData = copy.deepcopy(_kDefaultRuleSchedule)
        try:
            rule = backEndClient.getRule(ruleName)
            if rule is not None:
                self._schedData = rule.getSchedule()
        except Exception:
            pass

        mainSizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(mainSizer)

        schedBox = wx.StaticBox(self, -1, "Action Schedule")
        outerSizer = wx.StaticBoxSizer(schedBox, wx.VERTICAL)

        # ---- Days: 7 inline checkboxes ----
        dayType = self._schedData.get('dayType', 'Every day')
        if dayType == 'Every day':
            activeDays = {'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'}
        elif dayType == 'Weekdays':
            activeDays = {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}
        elif dayType == 'Weekends':
            activeDays = {'Sat', 'Sun'}
        else:
            activeDays = set(self._schedData.get('customDays', []))

        daysRow = wx.BoxSizer(wx.HORIZONTAL)
        daysRow.Add(wx.StaticText(schedBox, -1, "Days:"), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        self._schedDayChecks = OrderedDict()
        for day in ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']:
            cb = wx.CheckBox(schedBox, -1, day)
            cb.SetValue(day in activeDays)
            self._schedDayChecks[day] = cb
            daysRow.Add(cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)

        quickRow = wx.BoxSizer(wx.HORIZONTAL)
        quickRow.AddSpacer(44)
        for label in ['All', 'Weekdays', 'Weekends', 'None']:
            btn = wx.Button(schedBox, -1, label, style=wx.BU_EXACTFIT)
            btn.Bind(wx.EVT_BUTTON, self._schedOnQuickSelect)
            quickRow.Add(btn, 0, wx.RIGHT, 4)

        # ---- Time type: three radio options ----
        self._schedAllDayRadio = wx.RadioButton(schedBox, -1, "All day",
                                                style=wx.RB_GROUP)
        self._schedAllDayRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        self._schedFixedRadio = wx.RadioButton(schedBox, -1, "")
        self._schedFixedRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        self._schedStartTime = FixedTimeCtrl(schedBox, -1, value='08:00:00',
                                             size=wx.DefaultSize, format='24HHMM')
        _, timeH = self._schedStartTime.GetSize()
        self._schedStartSpin = wx.SpinButton(schedBox, -1, size=(-1, timeH),
                                             style=wx.SP_VERTICAL | wx.SP_WRAP)
        self._schedStartTime.BindSpinButton(self._schedStartSpin)
        self._schedStartTime.Bind(EVT_TIMEUPDATE,
                                  lambda e: self._schedFixedRadio.SetValue(True))

        self._schedStopTime = FixedTimeCtrl(schedBox, -1, value='18:00:00',
                                            size=wx.DefaultSize, format='24HHMM')
        self._schedStopSpin = wx.SpinButton(schedBox, -1, size=(-1, timeH),
                                            style=wx.SP_VERTICAL | wx.SP_WRAP)
        self._schedStopTime.BindSpinButton(self._schedStopSpin)
        self._schedStopTime.Bind(EVT_TIMEUPDATE,
                                 lambda e: self._schedFixedRadio.SetValue(True))

        fixedRow = wx.BoxSizer(wx.HORIZONTAL)
        fixedRow.Add(self._schedFixedRadio, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        fixedRow.Add(self._schedStartTime, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(self._schedStartSpin, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(wx.StaticText(schedBox, -1, "to"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, _kPad)
        fixedRow.Add(self._schedStopTime, 0, wx.ALIGN_CENTER_VERTICAL)
        fixedRow.Add(self._schedStopSpin, 0, wx.ALIGN_CENTER_VERTICAL)

        # ---- Solar row ----
        self._schedSolarRadio = wx.RadioButton(schedBox, -1, "")
        self._schedSolarRadio.Bind(wx.EVT_RADIOBUTTON, self._schedOnTimeType)

        _solarChoices = ["Sunrise", "Sunset"]
        self._schedSolarStartType = wx.Choice(schedBox, -1, choices=_solarChoices)
        self._schedSolarStartOffset = wx.SpinCtrl(schedBox, -1, value="0",
                                                  min=-240, max=240, size=(58, -1))
        self._schedSolarStopType = wx.Choice(schedBox, -1, choices=_solarChoices)
        self._schedSolarStopOffset = wx.SpinCtrl(schedBox, -1, value="0",
                                                 min=-240, max=240, size=(58, -1))

        solarRow = wx.BoxSizer(wx.HORIZONTAL)
        solarRow.Add(self._schedSolarRadio, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        solarRow.Add(self._schedSolarStartType, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "+/-"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(self._schedSolarStartOffset, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "min  to"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        solarRow.Add(self._schedSolarStopType, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "+/-"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(self._schedSolarStopOffset, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
        solarRow.Add(wx.StaticText(schedBox, -1, "min"), 0,
                     wx.ALIGN_CENTER_VERTICAL)

        # ---- Location row (enabled only for solar) ----
        self._schedLatCtrl = wx.TextCtrl(schedBox, -1, "", size=(80, -1))
        self._schedLonCtrl = wx.TextCtrl(schedBox, -1, "", size=(80, -1))
        self._schedPickCityBtn = wx.Button(schedBox, -1, "Pick city...",
                                           style=wx.BU_EXACTFIT)
        self._schedPickCityBtn.Bind(wx.EVT_BUTTON, self._schedOnPickCity)

        locationRow = wx.BoxSizer(wx.HORIZONTAL)
        locationRow.AddSpacer(20)
        locationRow.Add(wx.StaticText(schedBox, -1, "Lat:"), 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        locationRow.Add(self._schedLatCtrl, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        locationRow.Add(wx.StaticText(schedBox, -1, "Lon:"), 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        locationRow.Add(self._schedLonCtrl, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kPad)
        locationRow.Add(self._schedPickCityBtn, 0, wx.ALIGN_CENTER_VERTICAL)

        self._schedLocationHint = wx.StaticText(schedBox, -1,
            "(decimal degrees, e.g. 45.4, -75.7)")
        hintFont = self._schedLocationHint.GetFont()
        hintFont.MakeItalic()
        self._schedLocationHint.SetFont(hintFont)
        locationHintRow = wx.BoxSizer(wx.HORIZONTAL)
        locationHintRow.AddSpacer(20)
        locationHintRow.Add(self._schedLocationHint, 0, wx.ALIGN_CENTER_VERTICAL)

        outerSizer.Add(daysRow, 0, wx.EXPAND | wx.ALL, _kPad)
        outerSizer.Add(quickRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        outerSizer.Add(self._schedAllDayRadio, 0,
                       wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        outerSizer.Add(fixedRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        outerSizer.Add(solarRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        outerSizer.Add(locationRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)
        outerSizer.Add(locationHintRow, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kPad)

        buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK, self).Bind(wx.EVT_BUTTON, self.OnOK)
        self.FindWindowById(wx.ID_CANCEL, self).Bind(wx.EVT_BUTTON, self.OnCancel)

        mainSizer.Add(outerSizer, 0, wx.EXPAND | wx.ALL, _kPad * 2)
        mainSizer.Add(buttonSizer, 0, wx.EXPAND | wx.ALL, _kPad)

        self._schedPopulateFromData()

        self.Fit()
        self.CenterOnParent()


    ###########################################################
    def _schedPopulateFromData(self):
        """Populate all controls from self._schedData."""
        data = self._schedData
        startType = data.get('startType', 'fixed')
        stopType  = data.get('stopType',  'fixed')

        # Determine target radio before SetValue() calls: masked.TimeCtrl.SetValue()
        # fires EVT_TIMEUPDATE asynchronously which would switch radio to "Between
        # hours".  Re-apply the correct radio via wx.CallAfter after the queue drains.
        # _schedOnTimeType also triggers IP auto-detect if solar+empty.
        if data.get('is24Hours', True):
            targetRadio = self._schedAllDayRadio
        elif startType in ('sunrise', 'sunset') or stopType in ('sunrise', 'sunset'):
            targetRadio = self._schedSolarRadio
        else:
            targetRadio = self._schedFixedRadio

        self._schedStartTime.SetValue('%02d:%02d:00' % (
            data.get('startHour', 8), data.get('startMin', 0)))
        self._schedStopTime.SetValue('%02d:%02d:00' % (
            data.get('stopHour', 18), data.get('stopMin', 0)))

        _ch = ["Sunrise", "Sunset"]
        self._schedSolarStartType.SetSelection(
            _ch.index(startType.capitalize())
            if startType in ('sunrise', 'sunset') else 0)
        self._schedSolarStopType.SetSelection(
            _ch.index(stopType.capitalize())
            if stopType in ('sunrise', 'sunset') else 1)
        self._schedSolarStartOffset.SetValue(data.get('startOffset', 0))
        self._schedSolarStopOffset.SetValue(data.get('stopOffset',  0))

        lat = data.get('latitude')
        lon = data.get('longitude')
        self._schedLatCtrl.SetValue(str(lat) if lat is not None else '')
        self._schedLonCtrl.SetValue(str(lon) if lon is not None else '')

        targetRadio.SetValue(True)
        self._schedOnTimeType()
        wx.CallAfter(targetRadio.SetValue, True)
        wx.CallAfter(self._schedOnTimeType)


    ###########################################################
    def _schedUpdateTimeControls(self):
        """Enable/disable time-related controls based on active radio."""
        isFixed = self._schedFixedRadio.GetValue()
        isSolar = self._schedSolarRadio.GetValue()
        for ctrl in (self._schedStartTime, self._schedStartSpin,
                     self._schedStopTime, self._schedStopSpin):
            ctrl.Enable(isFixed)
        for ctrl in (self._schedSolarStartType, self._schedSolarStartOffset,
                     self._schedSolarStopType, self._schedSolarStopOffset,
                     self._schedLatCtrl, self._schedLonCtrl,
                     self._schedPickCityBtn):
            ctrl.Enable(isSolar)


    ###########################################################
    def _schedOnTimeType(self, event=None):
        """Handle a time-type radio change."""
        self._schedUpdateTimeControls()
        if self._schedSolarRadio.GetValue() and \
                not self._schedLatCtrl.GetValue().strip() and \
                not self._schedLonCtrl.GetValue().strip():
            from .ScheduleLocationPicker import schedAutoDetectLocation
            schedAutoDetectLocation(self._schedLatCtrl, self._schedLonCtrl,
                                    self._schedLocationHint, self)


    ###########################################################
    def _schedOnPickCity(self, event=None):
        from .ScheduleLocationPicker import schedOnPickCity
        schedOnPickCity(self, self._schedLatCtrl, self._schedLonCtrl,
                        self._schedLocationHint)


    ###########################################################
    def _schedOnQuickSelect(self, event):
        """Handle a quick-select day button."""
        label = event.GetEventObject().GetLabel()
        wkdays = {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}
        wkends = {'Sat', 'Sun'}
        for day, cb in self._schedDayChecks.items():
            if label == 'All':
                cb.SetValue(True)
            elif label == 'Weekdays':
                cb.SetValue(day in wkdays)
            elif label == 'Weekends':
                cb.SetValue(day in wkends)
            else:
                cb.SetValue(False)


    ###########################################################
    def OnOK(self, event=None):
        """Save the schedule and close.

        @param  event  The button event (ignored).
        """
        schedule = {}

        activeDays = [d for d, cb in self._schedDayChecks.items()
                      if cb.GetValue()]
        activeSet = set(activeDays)
        if activeSet == {'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'}:
            schedule['dayType'] = 'Every day'
            schedule['customDays'] = []
        elif activeSet == {'Mon', 'Tue', 'Wed', 'Thu', 'Fri'}:
            schedule['dayType'] = 'Weekdays'
            schedule['customDays'] = []
        elif activeSet == {'Sat', 'Sun'}:
            schedule['dayType'] = 'Weekends'
            schedule['customDays'] = []
        else:
            schedule['dayType'] = 'Custom...'
            schedule['customDays'] = activeDays

        if self._schedAllDayRadio.GetValue():
            schedule.update({'is24Hours': True,
                             'startHour': 0, 'startMin': 0,
                             'stopHour':  0, 'stopMin':  0,
                             'startType': 'fixed', 'startOffset': 0,
                             'stopType':  'fixed', 'stopOffset':  0,
                             'latitude': None, 'longitude': None})
        elif self._schedFixedRadio.GetValue():
            sp = self._schedStartTime.GetValue().split(':')
            ep = self._schedStopTime.GetValue().split(':')
            schedule.update({'is24Hours': False,
                             'startHour': int(sp[0]), 'startMin': int(sp[1][:2]),
                             'stopHour':  int(ep[0]), 'stopMin':  int(ep[1][:2]),
                             'startType': 'fixed', 'startOffset': 0,
                             'stopType':  'fixed', 'stopOffset':  0,
                             'latitude': None, 'longitude': None})
        else:
            _ch = ["Sunrise", "Sunset"]
            sType = _ch[self._schedSolarStartType.GetSelection()].lower()
            eType = _ch[self._schedSolarStopType.GetSelection()].lower()
            latStr = self._schedLatCtrl.GetValue().strip()
            lonStr = self._schedLonCtrl.GetValue().strip()
            try:
                lat = float(latStr) if latStr else None
            except ValueError:
                lat = None
            try:
                lon = float(lonStr) if lonStr else None
            except ValueError:
                lon = None
            schedule.update({'is24Hours': False,
                             'startHour': 0, 'startMin': 0,
                             'stopHour':  0, 'stopMin':  0,
                             'startType': sType,
                             'startOffset': self._schedSolarStartOffset.GetValue(),
                             'stopType':  eType,
                             'stopOffset':  self._schedSolarStopOffset.GetValue(),
                             'latitude': lat, 'longitude': lon})

        self._backEndClient.setRuleSchedule(self._ruleName, schedule)
        self.EndModal(wx.OK)


    ###########################################################
    def OnCancel(self, event=None):
        """Respond to canceling the dialog.

        @param  event  The button event (ignored).
        """
        self.EndModal(wx.CANCEL)
