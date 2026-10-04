#*****************************************************************************
#
# MenuIds.py
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
import wx

def _removeSpecialChars(item):
    res = item.replace("&", "")
    return res.split("\t")[0]


_kIsWin = wx.Platform == "__WXMSW__"
if _kIsWin:
    # For wxPython in Windows, the delete key is "Del".
    _kDeleteKey = "Del"
else:
    # For wxPython in OS X, the delete key is "Back".
    _kDeleteKey = "Back"






kViewMenuEx                        = "&View "
kViewMenu                          = _removeSpecialChars(kViewMenuEx)
kControlsMenuEx                    = "&Controls"
kControlsMenu                      = _removeSpecialChars(kControlsMenuEx)
kToolsMenuEx                       = "&Tools"
kToolsMenu                         = _removeSpecialChars(kToolsMenuEx)
kDeleteClipMenuEx                  = "&Delete Clip...\t%s" % _kDeleteKey
kDeleteClipMenu                    = _removeSpecialChars(kDeleteClipMenuEx)
kExportClipMenuEx                  = "E&xport Clip..."
kExportClipMenu                    = _removeSpecialChars(kExportClipMenuEx)
kExportClipForBugReportMenuEx      = "Export Clip for &Bug Report..."
kExportClipForBugReportMenu        = _removeSpecialChars(kExportClipForBugReportMenuEx)
kExportFrame                       = "Export Frame..."
kExportAllWithinTimeRangeMenu      = "Export All Clips Within Time Range..."
# Reveal a clip's source file in the OS file manager; named for each platform.
kShowSourceFileMenu                = "Show in Explorer" if _kIsWin else "Reveal in Finder"

# The Reports menu.  These run offline scans of the logs and databases and
# write a text file; they answer "what has happened since", where the System
# view answers "what is happening now".  Deliberately not in Tools: Tools is
# full of items the views bind and enable per selection, these are always live.
kReportsMenuEx                     = "&Reports"
kReportsMenu                       = _removeSpecialChars(kReportsMenuEx)
kCameraHealthReportMenu            = "&Camera Health..."
kDetectionsReportMenu              = "&Detections by Camera..."
# "and", not an ampersand -- wx would read a literal & as a mnemonic.
kLogErrorsReportMenu               = "&Log Errors and Warnings..."

# The Developer menu.  Tools for working ON the detector rather than with it:
# replay archived footage through the motion pipeline under settings you choose,
# so a tuning change can be measured in minutes instead of waiting a night.
kDeveloperMenuEx                   = "De&veloper"
kDeveloperMenu                     = _removeSpecialChars(kDeveloperMenuEx)
kDetectionTestMenu                 = "&Detection Test Suite..."

def getMenuItem(menu, submenu, item):
    return menu.FindItemById( menu.FindMenuItem(submenu, item) )

def getToolsMenuItem(menu, item):
    return getMenuItem ( menu, kToolsMenu, item )

def getControlsMenuItem(menu, item):
    return getMenuItem ( menu, kControlsMenu, item)

def getViewMenuItem(menu, item):
    return getMenuItem ( menu, kViewMenu, item)
