#!/usr/bin/env python

#*****************************************************************************
#
# UIPrefsDataModel.py
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

from .FrontEndPrefs import getFrontEndPref, setFrontEndPref

from vitaToolbox.mvc.AbstractModel import AbstractModel
from vitaToolbox.wx.AppColors import setAppBackgroundColour

##############################################################################
class UIPrefsDataModel(AbstractModel):
    """A data model that represents UI preferences which may change at runtime.

    Putting this into a data model allows everyone to listen for changes
    and allows multiple controllers to make changes.

    Keys for updates are:
    - 'time'           - Called whenever time preferences change.
    - 'supportWarning' - Called when the support/upgrades warning pref changed
    - 'gridView'       - Called when the grid view settings got altered.
    - 'colors'         - Called when the UI background colour changed.
    """
    ###########################################################
    def __init__(self, backEndClient):
        """UIPrefsDataModel constructor.

        @param  backEndClient  A BackEndClient instance.
        """
        AbstractModel.__init__(self)

        self._use12Hour, self._useUSDate = backEndClient.getTimePreferences()

        self._gridViewSettings = (getFrontEndPref("gridViewRows"),
                                  getFrontEndPref("gridViewCols"),
                                  getFrontEndPref("gridViewOrder"),
                                  getFrontEndPref("gridViewFps"),
                                  getFrontEndPref("gridViewShowInactive"))

        self._backgroundColor = getFrontEndPref("uiBackgroundColor")


    ###########################################################
    def getBackgroundColor(self):
        """Retrieve the UI background colour.

        @return color  (r, g, b) tuple, or None for the system default.
        """
        return self._backgroundColor


    ###########################################################
    def setBackgroundColor(self, color):
        """Set the UI background colour and tell listeners.

        @param  color  (r, g, b) tuple, or None for the system default.
        """
        if color is not None:
            color = tuple(int(c) for c in color[:3])
        dirty = (self._backgroundColor != color)
        self._backgroundColor = color
        setFrontEndPref("uiBackgroundColor", color)
        # Push it to the shared holder BEFORE notifying, so anything that
        # rebuilds itself in response picks up the new colour on the way.
        setAppBackgroundColour(color)
        if dirty:
            self.update('colors')


    ###########################################################
    def getTimePreferences(self):
        """Retrieve time display preferences.

        Sourced once at construction from the back end, which fixes these at
        24-hour ISO.

        @return use12Hour  False -- 24-hour time.
        @return useUSDate  'iso'.  A TRI-STATE STRING ('us' / 'intl' / 'iso'),
                           NOT a bool: callers must branch on all three, as
                           VitaDatePickerCtrl.useUSDateFormat and
                           RuleListPanel._handleTimePrefChange do.  Testing it
                           for truthiness silently selects US format.
        """
        return self._use12Hour, self._useUSDate


    ###########################################################
    def setTimePreferences(self, use12Hour, useUSDate):
        """Set time display preferences.

        CURRENTLY UNUSED -- nothing calls this.  The back end fixes the format
        at 24-hour ISO and its setter has been removed, so there is no path by
        which these values change at runtime.  Kept because it is a harmless
        model setter that notifies listeners, should a format option ever come
        back.

        NOTE: This does not update the back end, it is assumed that was
              done separately.

        @param  use12Hour  True if 12 hour time should be used.
        @param  useUSDate  'us' / 'intl' / 'iso' -- a tri-state, not a bool.
        """
        self._use12Hour = use12Hour
        self._useUSDate = useUSDate
        self.update('time')


    ###########################################################
    def shouldShowSupportWarning(self):
        """Retrieve the support warning preference.

        @return warn  True if the user should be warned of pending expiration.
        """
        return getFrontEndPref('supportRenewalWarning')


    ###########################################################
    def enableSupportWarnings(self, enable):
        """Set the support warning preference.

        @param  enable  True if the user should be warned of pending expiration.
        """
        setFrontEndPref('supportRenewalWarning', enable)
        self.update('supportWarning')


    ###########################################################
    def updateGridViewSettings(self, rows, cols, order, fps, inactive):
        """ Push the new grid view settings.

        @param  rows        New number of rows.
        @param  cols        New number of columns.
        @param  order       New order.
        @param  fps         New frames per second.
        @param  inactive    New mode of showing inactive cameras
        """

        settings = (rows, cols, order, fps, inactive)

        dirty = self._gridViewSettings != settings
        self._gridViewSettings = settings

        if dirty:
            self.update('gridView')
