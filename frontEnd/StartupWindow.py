#!/usr/bin/env python

#*****************************************************************************
#
# StartupWindow.py
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
import sys
import time

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.wx.AppColors import getAppBackgroundColour
from vitaToolbox.wx.AppColors import pickForeground
from vitaToolbox.wx.FontUtils import makeFontDefault
from vitaToolbox.wx.FontUtils import growTitleText

# Local imports...
from appCommon.CommonStrings import kAppName
from appCommon.InstallPaths import getInstallRoot


# NOTE: nothing imported above may pull in the view modules.  This window
# exists to be on screen before FrontEndFrame is even imported, so its whole
# import graph has to stay at wx + os -- no cv2, no numpy, no OpenGL.


# Constants...

# Window size.  Wide enough for the longest status message we send, small
# enough to read as a splash rather than as a dialog.
_kWindowSize = (440, 200)

# How often the gauge advances on its own, in milliseconds.  Startup has long
# stretches with no Pulse() caller, and a bar that has stopped moving reads as
# a hung app -- which is the whole problem this window exists to solve.
_kPulseTimerMs = 120

# Padding around our contents.
_kBorder = 16

# What we say before anybody tells us anything more specific.
_kDefaultMessage = "Starting up..."


##############################################################################
class StartupWindow(wx.Frame):
    """The window the user sees the instant the app starts.

    This deliberately implements the same Pulse() / Update() / Destroy()
    contract as DelayedProgressDialog and ProgressFrameWithLog, so it drops
    into FrontEndApp wherever the old delayed dialog was used.

    Two things make it different from a wx.ProgressDialog, and both matter:

    - It is shown IMMEDIATELY, not after a delay.  The old dialog waited five
      seconds and then only materialised if somebody happened to call Pulse(),
      so on a fast back-end connect it never appeared at all.

    - It is a real top-level frame, so it gets a taskbar button and the app
      icon.  That is what tells the user the program is launching.  It also
      gives LookForOtherInstances something to find: it is titled with the app
      name, so a second launch can raise it instead of exiting invisibly.

    Because we are created before MainLoop() runs, nothing pumps the event
    queue for us -- a plain frame would never paint.  So we yield explicitly
    on every update.  (wx.ProgressDialog gets away without this because it
    pumps internally.)
    """

    ###########################################################
    def __init__(self, message=_kDefaultMessage):
        """StartupWindow constructor.

        @param  message  The first status message to show.
        """
        # No close box: PostInit owns this window's lifetime, and a user who
        # closed it mid-startup would leave the app running with no UI.
        super(StartupWindow, self).__init__(
            None, -1, kAppName,
            style=wx.CAPTION | wx.SYSTEM_MENU | wx.MINIMIZE_BOX |
                  wx.CLIP_CHILDREN
        )

        self._isDone = False
        self._wantYields = True

        self._initUiWidgets(message)

        self.SetClientSize(_kWindowSize)
        self._setIcons()

        self.Bind(wx.EVT_CLOSE, self.OnClose)

        # Keep the gauge moving through stretches that never call Pulse().
        # Timer events reach us through our own yields, so this only animates
        # while somebody is giving us the chance to.
        self._pulseTimer = wx.Timer(self, -1)
        self.Bind(wx.EVT_TIMER, self.OnPulseTimer, self._pulseTimer)
        self._pulseTimer.Start(_kPulseTimerMs)

        self.Centre()


    ###########################################################
    def _initUiWidgets(self, message):
        """Init the widgets that go in our sizer.

        @param  message  The first status message to show.
        """
        self._panel = wx.Panel(self)

        background = getAppBackgroundColour()
        self._panel.SetBackgroundColour(background)
        foreground = pickForeground(background)

        self._title = wx.StaticText(self._panel, -1, kAppName)
        growTitleText(self._title)
        self._title.SetForegroundColour(foreground)

        self._label = wx.StaticText(self._panel, -1, message,
                                    style=wx.ST_NO_AUTORESIZE |
                                          wx.ST_ELLIPSIZE_END)
        makeFontDefault(self._label)
        self._label.SetForegroundColour(foreground)

        self._gauge = wx.Gauge(self._panel, -1, 100)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._title, 0, wx.EXPAND | wx.ALL, _kBorder)
        sizer.AddStretchSpacer(1)
        sizer.Add(self._label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorder)
        sizer.Add(self._gauge, 0, wx.EXPAND | wx.ALL, _kBorder)
        self._panel.SetSizer(sizer)

        frameSizer = wx.BoxSizer(wx.VERTICAL)
        frameSizer.Add(self._panel, 1, wx.EXPAND)
        self.SetSizer(frameSizer)


    ###########################################################
    def _setIcons(self):
        """Give ourselves the app icon, so the taskbar button is recognisable.

        Resolved against the install root rather than the working directory,
        for the same reason FrontEndFrame does it that way: a taskbar pin
        supplies its own cwd, and a relative path silently yields a blank icon
        exactly when the icon matters most.
        """
        try:
            if wx.Platform != "__WXMSW__":
                return
            iconPath = os.path.join(getInstallRoot(), "icons",
                                    "SmartVideoApp.ico")
            if os.path.isfile(iconPath):
                icons = wx.IconBundle()
                icons.AddIcon(iconPath, wx.BITMAP_TYPE_ICO)
                self.SetIcons(icons)
        except Exception:
            # A missing icon must never be the reason the app fails to start.
            pass


    ###########################################################
    def showAndPaint(self):
        """Show the window and force it to actually paint.

        Show() alone only queues the paint.  MainLoop() is not running yet, so
        without pumping the queue ourselves the user would see nothing, which
        would defeat the entire point of this class.
        """
        self.Show()
        self.Raise()
        self._yield()


    ###########################################################
    def setMessage(self, message):
        """Set the status text.

        @param  message  The text to show; "" leaves it alone.
        """
        if message and not self._isDone:
            self._label.SetLabel(message)


    ###########################################################
    def Pulse(self, message=""):
        """Advance the indeterminate gauge; optionally change the message.

        Signature matches DelayedProgressDialog.Pulse() and
        ProgressFrameWithLog.Pulse(), so this is a drop-in for either.

        @param  message   The new message, or "" to leave it alone.
        @return continue  True normally.
        @return skip      False normally; skipping isn't supported.
        """
        if self._isDone:
            return True, False

        self.setMessage(message)
        self._gauge.Pulse()
        self._yield()

        return True, False


    ###########################################################
    def Update(self, newValue, message=""):
        """Set a determinate value; optionally change the message.

        @param  newValue  The new gauge value, 0..100.
        @param  message   The new message, or "" to leave it alone.
        @return continue  True normally.
        @return skip      False normally; skipping isn't supported.
        """
        if self._isDone:
            return True, False

        self.setMessage(message)
        self._gauge.SetValue(max(0, min(100, int(newValue))))
        self._yield()

        return True, False


    ###########################################################
    def _yield(self):
        """Let wx paint and deliver timer events.

        Guarded, because a yield that raised during startup would take the
        whole launch down with it.
        """
        if not self._wantYields:
            return
        try:
            wx.YieldIfNeeded()
        except Exception:
            pass


    ###########################################################
    def OnPulseTimer(self, event):
        """Keep the gauge alive between explicit Pulse() calls.

        @param  event  The timer event (ignored).
        """
        if not self._isDone:
            self._gauge.Pulse()


    ###########################################################
    def OnClose(self, event):
        """Refuse to be closed by the user.

        PostInit owns this window; closing it out from under the launch would
        leave the app running with no UI.

        @param  event  The close event.
        """
        if not self._isDone:
            event.Veto()
        else:
            event.Skip()


    ###########################################################
    def Destroy(self):
        """Stop our timer, then go away.

        @return  Whatever wx.Frame.Destroy() returns.
        """
        self._isDone = True
        self._wantYields = False
        try:
            if self._pulseTimer.IsRunning():
                self._pulseTimer.Stop()
        except Exception:
            pass
        return super(StartupWindow, self).Destroy()


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    results = []

    def check(description, condition):
        """Record one assertion.

        @param  description  What we're checking.
        @param  condition    Truthy if it passed.
        """
        results.append(bool(condition))
        print("%s - %s" % ("ok  " if condition else "FAIL", description))

    app = wx.App(False)
    _ = app

    win = StartupWindow("Starting up...")

    check("window is a real top-level frame",
          isinstance(win, wx.Frame) and win.GetParent() is None)
    check("window is titled with the app name, so it can be found",
          win.GetTitle() == kAppName)
    check("window is not shown until we say so", not win.IsShown())

    win.showAndPaint()
    check("showAndPaint() makes it visible", win.IsShown())

    # The Pulse/Update contract FrontEndApp relies on...
    check("Pulse() returns the (continue, skip) pair",
          win.Pulse() == (True, False))
    win.Pulse("Starting the video engine...")
    check("Pulse(msg) sets the message",
          win._label.GetLabel() == "Starting the video engine...")
    check("Update() returns the (continue, skip) pair",
          win.Update(50, "Loading the main window...") == (True, False))
    check("Update(msg) sets the message",
          win._label.GetLabel() == "Loading the main window...")
    win.Pulse("")
    check("Pulse('') leaves the message alone",
          win._label.GetLabel() == "Loading the main window...")

    # It has to survive being driven hard with no MainLoop running; that is
    # exactly how PostInit uses it.
    startTime = time.time()
    spins = 0
    while time.time() - startTime < 1.0:
        win.Pulse("Waiting for the back end... %d" % spins)
        spins += 1
    check("survives being pulsed without a MainLoop (%d spins)" % spins,
          spins > 0 and win.IsShown())

    check("the gauge timer runs while we are up", win._pulseTimer.IsRunning())

    win.Destroy()
    check("Destroy() stops the gauge timer", not win._pulseTimer.IsRunning())
    check("Pulse() after Destroy() is harmless", win.Pulse() == (True, False))

    print("\nHeadless %d/%d" % (results.count(True), len(results)))
    return 0 if all(results) else 1


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(test_main())
    else:
        print("Try calling with 'test' as the argument.")
