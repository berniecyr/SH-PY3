#!/usr/bin/env python

#*****************************************************************************
#
# ExportClipsProgDialog.py
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
import os
import threading
import traceback
from copy import deepcopy

# Common 3rd-party imports...
import wx

# Toolbox imports...

# Local imports...
from .FrontEndUtils import getUserLocalDataDir, makeClipExportBaseName

_kPaddingSize = 4
_kBorderSize = 16
_kExportClipProgressText = "Exporting clip %i of %i."
_kTimerQuick = 100
_kTimerSlow = 500
_kExportProgressText = "Exporting..."
_kExportPercentText = "Exporting - %d%% done"
_kExportCancelText = "Cancelling..."
# Nothing downstream reports real progress, so the time-range
# export's gauge pulses instead; this is how often it steps.
_kPulseIntervalMs = 100


###############################################################
class ExportClipsProgDialog(wx.Dialog):
    """A progress dialog shown while clips are being exported."""
    ###########################################################
    def __init__(self, parent, dataManager, clipManager,
                 savePath, clipList, extras={}):
        """Initializer for _ExportClipsProgDialog.

        @param  parent            The parent window.
        @param  logger            An instance of a VitaLogger to use.
        @param  objDbPath         Path to the object database.
        @param  clipDbPath        Path to the clip manager.
        @param  videoStoragePath  Path to the directory videos are stored.
        @param  savePath          Path to save/export the clips.
        @param  clipList          A list of clips, each of which is
                                  a MatchingClipInfo object.
        @param  extras            A dictionary of extra options and settings
                                  that further specify how clips should be
                                  saved/exported.
        """
        wx.Dialog.__init__(self, parent, -1, "Export clips",
                           style=wx.CAPTION | wx.SYSTEM_MENU)

        try:
            self._dataManager = dataManager
            self._clipManager = clipManager

            self._savePath = savePath

            self._clipList = clipList
            self._numClips = len(clipList)
            self._cancelled = False
            self._curIndex = -1

            self._extras = extras

            self._curExportComplete = False
            self._failedToExport = False

            # Names this run has already written, so two results
            # that share a camera and second can't overwrite each
            # other.
            self._usedNames = set()

            # Referenced (and guarded against) by _exportNext(), which runs
            # once below before the timer itself is created.
            self._timer = None

            # Create the main sizer.
            sizer = wx.BoxSizer(wx.VERTICAL)
            self.SetSizer(sizer)

            # Create the controls.
            self._label = wx.StaticText(self, -1, _kExportClipProgressText %
                                        (1, self._numClips))
            self._gauge = wx.Gauge(self, -1, self._numClips)
            self._gauge.SetMinSize((300, -1))

            sizer.Add(self._label, 0, wx.LEFT | wx.RIGHT | wx.TOP, _kBorderSize)
            sizer.AddSpacer(8)
            sizer.Add(self._gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT |
                      wx.BOTTOM, _kBorderSize)

            buttonSizer = self.CreateStdDialogButtonSizer(wx.CANCEL)
            sizer.Add(buttonSizer, 0, wx.BOTTOM | wx.EXPAND, 16)

            self.FindWindowById(wx.ID_CANCEL, self).Bind(wx.EVT_BUTTON, self.OnCancel)

            self.Fit()
            self.CenterOnParent()

            # Begin the first export
            self._exportNext()

            # Start the update timer
            self._timer = wx.Timer(self, -1)
            self.Bind(wx.EVT_TIMER, self.OnUpdate, self._timer)

            self._timer.Start(_kTimerSlow)

        except: # All exceptions, not just Exception subclasses
            # Make absolutely sure that we are destroyed, even if we crash
            # in the above...
            self.Destroy()
            raise


    ###########################################################
    def Destroy(self):
        """Stop our timer before actually destroying.

        wx.Dialog.Destroy() defers the underlying C++ deletion to idle time
        rather than doing it synchronously, so EVT_WINDOW_DESTROY is NOT a
        reliable place to stop a still-running timer for a top-level window
        like this one -- verified by reproducing a real crash: a
        continuously-repeating timer can still fire (into freed memory)
        before that deferred deletion -- and its event handler -- actually
        run. Stopping the timer here, synchronously, before calling the base
        Destroy(), is what actually prevents it.

        @see wx.Dialog.Destroy()
        """
        if self._timer is not None:
            self._timer.Stop()
        return super(ExportClipsProgDialog, self).Destroy()


    ###########################################################
    def _exportNext(self):
        """Exports the next clip in the given cliplist.

        NOTE: called once from __init__ before self._timer exists (the very
        first export kicks off before the update timer is created), so this
        must not guard on self._timer being set -- only on window liveness.
        """
        # Belt-and-suspenders: bail if we're gone (see Destroy()).
        if not self:
            return

        # If we're done or the user cancelled stop the timer and exit.
        if self._cancelled or (self._curIndex == self._numClips-1):
            self._timer.Stop()
            self.EndModal(wx.ID_CANCEL)
            return

        extras = deepcopy(self._extras)

        # Begin to export next clip.
        self._curIndex += 1
        curClip = self._clipList[self._curIndex]

        startTime, stopTime = self._dataManager.openMarkedVideo(
            curClip.camLoc, curClip.startTime, curClip.stopTime,
            curClip.playStart, curClip.objList, (0, 0)
        )

        # TODO: Support for names of imported clips if we ever release an
        #       analysis version.

        # Same name shape as a single-clip export (see
        # _getExportOptions in SearchResultsPlaybackPanel):
        # yyyy-mm-dd-hhmmss-cameraname.
        defName = self._uniqueName(
            makeClipExportBaseName(curClip.camLoc, startTime) + '.mp4')

        savePath = os.path.join(self._savePath, defName)
        success = self._dataManager.saveCurrentClip(
            savePath, startTime, stopTime, getUserLocalDataDir(), extras
        )

        if not success:
            wx.MessageBox("There was an error exporting the clip.",
                          "Error", wx.ICON_ERROR | wx.OK,
                          self.GetTopLevelParent())
            self._timer.Stop()
            self.EndModal(wx.ID_ABORT)
            return

        # Update the label and gauge.
        self._label.SetLabel(_kExportClipProgressText % (self._curIndex+1,
                                                         self._numClips))
        self._gauge.SetValue(self._curIndex)

        self._curExportComplete = True

    ###########################################################
    def _uniqueName(self, name):
        """Keep one export run from writing two clips to the same file.

        Results that aren't combined can share a camera and a start second,
        and every name here is generated rather than chosen by the user, so a
        collision would silently lose a clip.
        """
        if name not in self._usedNames:
            self._usedNames.add(name)
            return name

        stem, ext = os.path.splitext(name)
        for n in range(2, 1000):
            candidate = '%s-%d%s' % (stem, n, ext)
            if candidate not in self._usedNames:
                self._usedNames.add(candidate)
                return candidate
        return name

    ###########################################################
    def OnUpdate(self, event):
        """Update the progress dialog with the current status.

        @param  event  The Timer event, ignored.
        """
        # Check if the current export has completed.
        if self._curExportComplete:
            self._curExportComplete = False
            self._exportNext()


    ###########################################################
    def OnCancel(self, event=None):
        """Cancel the dialog.

        @param  event  The button event.
        """
        self._cancelled = True
        self._label.SetLabel("Canceling...")


###################################################################
class ExportProgressDialog(wx.Dialog):
    """Progress dialog shown while a time range is exported.

    Nothing downstream reports how far along the export is -- ClipUtils takes
    a progress function but never calls it -- so the only report that arrives
    is ExportRunner's "done".  The gauge therefore pulses rather than claiming
    a percentage it doesn't have, and the dialog closes itself when the export
    finishes.

    Progress used to travel as a custom wx.CommandEvent subclass.  wx.PostEvent
    CLONES the event, and a plain CommandEvent cannot carry Python attributes
    through that clone, so every single update died in OnProgress with
    "'CommandEvent' object has no attribute 'percentage'" -- which is why this
    dialog used to sit at "Exporting - 0% done" until the user pressed Cancel.
    wx.CallAfter hands the value to the UI thread intact and needs no event
    class at all.
    """
    ###########################################################
    def __init__(self, parent, fileList, savePath, startTime, endTime, dataDir, extras, logger, progressFn):
        wx.Dialog.__init__(self, parent, -1, "Exporting ...", style=wx.CAPTION | wx.SYSTEM_MENU)
        self._fileList = fileList
        self._savePath = savePath
        self._startTime = startTime
        self._endTime = endTime
        self._dataDir = dataDir
        self._extras = extras
        self._logger = logger
        self._progressFn = progressFn
        self._result = None
        self._cancelled = False

        # Set once the export has reported itself finished, so a late report
        # can't end the dialog twice.
        self._finished = False

        # Created in OnInit, but Destroy() can run before that ever happens.
        self._pulseTimer = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(sizer)

        # Create the controls.
        self._label = wx.StaticText(self, -1, _kExportProgressText)
        self._gauge = wx.Gauge(self, -1, 100)
        self._gauge.SetMinSize((300, -1))

        sizer.Add(self._label, 0, wx.LEFT | wx.RIGHT | wx.TOP, _kBorderSize)
        sizer.AddSpacer(8)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorderSize)

        buttonSizer = self.CreateStdDialogButtonSizer(wx.CANCEL)
        sizer.Add(buttonSizer, 0, wx.BOTTOM | wx.EXPAND, 16)

        self.FindWindowById(wx.ID_CANCEL, self).Bind(wx.EVT_BUTTON, self.OnCancel)
        self.Bind(wx.EVT_INIT_DIALOG, self.OnInit)

        self.Fit()
        self.CenterOnParent()

        self._thread = ExportRunner(self)

    ###########################################################
    def Destroy(self):
        """Stop the pulse timer before actually destroying.

        Same hazard as ExportClipsProgDialog.Destroy() above: wx defers the
        underlying C++ deletion of a top-level window to idle time, so a
        repeating timer can still fire into freed memory unless it is stopped
        here, synchronously.

        @see wx.Dialog.Destroy()
        """
        self._stopPulse()
        return super(ExportProgressDialog, self).Destroy()

    ###########################################################
    def _stopPulse(self):
        """Stop the indeterminate gauge; safe to call more than once."""
        if self._pulseTimer is not None:
            self._pulseTimer.Stop()
            self._pulseTimer = None

    ###########################################################
    def _endThread(self):
        if self._thread is not None:
            if self._thread.is_alive():
                self._result = -1

                # Definitely not normal, we expect 2 min files, and copying should not take this long
                self._logger.debug("Terminating clip creation thread...")
                self._thread.join()
                self._logger.debug("... done")
            self._thread = None

    ###########################################################
    def OnInit(self, event=None):
        self._thread.start()

        # No percentage exists to show, so show motion instead of a number.
        self._pulseTimer = wx.Timer(self, -1)
        self.Bind(wx.EVT_TIMER, self.OnPulse, self._pulseTimer)
        self._pulseTimer.Start(_kPulseIntervalMs)

    ###########################################################
    def OnPulse(self, event=None):
        """Advance the indeterminate gauge.

        @param  event  The Timer event, ignored.
        """
        self._gauge.Pulse()

    ###########################################################
    def OnCancel(self, event=None):
        """Cancel the dialog.

        NOTE: this waits for the running export rather than killing it, so it
        can take a moment -- hence the label change before the wait.

        @param  event  The button event.
        """
        self._cancelled = True
        self._stopPulse()
        self._label.SetLabel(_kExportCancelText)
        self.Update()
        self._endThread()
        self.EndModal(wx.OK)

    ###########################################################
    def _onProgress(self, percentage):
        """Handle a progress report, on the UI thread.

        Called via wx.CallAfter from SetPercentageDone (see the class comment).
        Anything over 100 means the export has finished, however it went; the
        result code is what says whether it worked.

        @param  percentage  Percent done, or >100 for "finished".
        """
        if not self or self._finished:
            return

        if percentage > 100:
            self._finished = True
            self._logger.info("Export completed -- closing the dialog. Res=" +
                              str(self._result))
            self._stopPulse()
            if self.IsModal():
                self.EndModal(wx.ID_OK)
        else:
            # Nothing reports real progress today, but if something ever
            # starts to, show it rather than the pulse.
            self._stopPulse()
            self._gauge.SetValue(percentage)
            self._label.SetLabel(_kExportPercentText % percentage)

    ###########################################################
    def Success(self):
        """Whether the export is considered to have succeeded.

        A cancel counts as success (the user asked for it).  A _result still
        None means the thread died before setting one, which is a failure --
        comparing that to 0 used to raise TypeError on Python 3.
        """
        if self._cancelled:
            return True
        return self._result is not None and self._result >= 0

    ###########################################################
    def SetPercentageDone(self, percentage):
        """Report progress from the export thread.

        Safe to call off the UI thread, which is the point: wx.CallAfter is
        how the value reaches the UI thread intact.

        @param  percentage  Percent done, or >100 to say the export finished.
        """
        wx.CallAfter(self._onProgress, percentage)
        return 0

###################################################################
class ExportRunner(threading.Thread):
    """ Internal class for running export processing, while UI is busy
        showing a progress bar
    """
    ###########################################################
    def __init__(self, owner):
        threading.Thread.__init__(self)
        self._owner = owner

    ###########################################################
    def run(self):
        try:
            from videoLib2.python.ClipUtils import remuxClip  # Lazy--loaded on first need
            self._owner._result = remuxClip(self._owner._fileList,
                                self._owner._savePath,
                                self._owner._startTime,
                                self._owner._endTime,
                                self._owner._dataDir,
                                self._owner._extras,
                                self._owner._logger.getCLogFn(),
                                self._owner._progressFn)
        except:
            self._owner._logger.error("remuxClip: exception " + traceback.format_exc())
            # On the OWNER, not on self: setting it here left the dialog's own
            # _result as None, and Success() then compared None >= 0.
            self._owner._result = -1
        self._owner.SetPercentageDone(101)

