#! /usr/local/bin/python

#*****************************************************************************
#
# SystemHealthView.py
#     Read-only dashboard of camera and system health.
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
A read-only "System" view showing camera and system health at a glance.

Everything here was previously only discoverable by reading log files: which
cameras are live and how recently they delivered a frame, which ones keep
reconnecting, how much disk is left, how far the PC clock has drifted, and what
each process is costing in RAM/CPU.  The numbers are gathered by the back end
(see NetworkMessageServer._getSystemHealth) and polled while this view is
visible only, so it costs nothing when you're not looking at it.
"""

# Python imports...
import os
import time

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.wx.TranslucentStaticText import TranslucentStaticText
from vitaToolbox.wx.FontUtils import makeFontDefault, makeFontBold

# Local imports...
from frontEnd.BaseView import BaseView
from appCommon.CommonStrings import kCameraOn, kCameraOff, kCameraConnecting
from appCommon.CommonStrings import kCameraFailed


# Constants...

# How often to refresh while the view is showing.  Slow enough to be nearly
# free, fast enough that a camera dropping out is obvious.
_kRefreshIntervalMs = 2000

# A camera that hasn't delivered a frame in this long is called out, even if
# its status still claims it's on.
_kStaleFrameSecs = 30.0

# Frames can keep arriving while the ANALYSIS pass behind them stalls -- a
# backlogged detection service does exactly that, and it is what made healthy
# cameras read "No frames" (see _classifyCamera).  Deliberately far looser than
# _kStaleFrameSecs: the analysis clock is drained on BackEndApp's 10s liveness
# timer and real-time searches are batched, so it is lumpy by construction even
# on a healthy system -- measured as a 1-10s sawtooth, so this leaves ~12x
# headroom before anything is said about it.
_kStaleAnalysisSecs = 120.0

# Clock drift we consider worth flagging (matches DiskCleaner's alert level).
_kClockWarnSecs = 3.0

# Warn in the view when nothing has reached the clip database for this long.
# Mirrors _kClipRegistrationWarnSecs in BackEndApp (600s), which is the side
# that actually logs it; this is only the display threshold.
_kClipQuietWarnMins = 10.0

_kBorder = 12

_kOkColor    = wx.Colour(0x1B, 0x7F, 0x3B)
_kWarnColor  = wx.Colour(0xB8, 0x6E, 0x00)
_kBadColor   = wx.Colour(0xC0, 0x2A, 0x2A)
_kMutedColor = wx.Colour(0x77, 0x77, 0x77)

_kCameraColumns = [
    ("Camera",       180),
    ("Status",        95),
    ("Analysis Res",   100),
    ("FPS",           65),
    ("Decoder",       75),
    ("Frame age",     85),
    ("Dropped",       75),
    ("Reconnects",    80),
    ("Details",      220),
]

_kProcessColumns = [
    ("Process",     220),
    ("PID",          70),
    ("Memory",       90),
    ("CPU",          80),
]

##############################################################################
class SystemHealthView(BaseView):
    """A read-only dashboard of camera and system health."""

    ###########################################################
    def __init__(self, parent, backEndClient):
        """Initializer for SystemHealthView.

        @param  parent         The parent window.
        @param  backEndClient  A connection to the back end app.
        """
        super(SystemHealthView, self).__init__(parent, backEndClient)

        self._timer = wx.Timer(self, -1)
        # So a persistent RPC failure is logged once, not every 2 seconds.
        self._loggedRefreshError = False
        # Per-camera cumulative analysis counters from the last health poll.
        # Used to display a recent dropped-frame percentage rather than a
        # lifetime percentage that slowly loses diagnostic value.
        self._healthFrameCounters = {}
        self.Bind(wx.EVT_TIMER, self.OnRefreshTimer, self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)

        self._initUiWidgets()


    ###########################################################
    def _initUiWidgets(self):
        """Build the (static) layout; contents are filled in on refresh."""
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._summaryText = TranslucentStaticText(self, -1, "Loading…")
        makeFontBold(self._summaryText)
        sizer.Add(self._summaryText, 0, wx.ALL, _kBorder)

        self._systemText = TranslucentStaticText(self, -1, "")
        makeFontDefault(self._systemText)
        sizer.Add(self._systemText, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        camLabel = TranslucentStaticText(self, -1, "Cameras")
        makeFontBold(camLabel)
        sizer.Add(camLabel, 0, wx.LEFT | wx.RIGHT, _kBorder)

        self._cameraList = wx.ListCtrl(
            self, -1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        for i, (label, width) in enumerate(_kCameraColumns):
            self._cameraList.InsertColumn(i, label, width=width)
        sizer.Add(self._cameraList, 3,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        procLabel = TranslucentStaticText(self, -1, "Processes")
        makeFontBold(procLabel)
        sizer.Add(procLabel, 0, wx.LEFT | wx.RIGHT, _kBorder)

        self._processList = wx.ListCtrl(
            self, -1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        for i, (label, width) in enumerate(_kProcessColumns):
            self._processList.InsertColumn(i, label, width=width)
        sizer.Add(self._processList, 2,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        self.SetSizer(sizer)


    ###########################################################
    def _onDestroy(self, event):
        """Stop the timer so it can't fire into a dead window."""
        if event.GetEventObject() == self:
            try:
                self._timer.Stop()
            except Exception:
                pass
        event.Skip()


    ###########################################################
    def setActiveView(self, viewParams=None):
        """@see BaseView.setActiveView -- start polling."""
        super(SystemHealthView, self).setActiveView(viewParams)
        self._refresh()
        self._timer.Start(_kRefreshIntervalMs)


    ###########################################################
    def deactivateView(self):
        """@see BaseView.deactivateView -- stop polling when hidden."""
        self._timer.Stop()
        super(SystemHealthView, self).deactivateView()


    ###########################################################
    def prepareToClose(self):
        """@see BaseView.prepareToClose"""
        self._timer.Stop()


    ###########################################################
    def OnRefreshTimer(self, event=None):
        """Poll the back end for a fresh snapshot."""
        self._refresh()


    ###########################################################
    def _refresh(self):
        """Fetch health from the back end and repaint the lists."""
        try:
            health = self._backEndClient.getSystemHealth()
        except Exception as e:
            # Back end busy/restarting -- keep the last snapshot on screen
            # rather than blanking the view.  Log the reason (rate-limited):
            # swallowing it silently once cost real debugging time.
            self._summaryText.SetLabel("Back end not responding…")
            if not self._loggedRefreshError:
                self._loggedRefreshError = True
                self._logger.warning("getSystemHealth failed: %r" % (e,))
            return
        self._loggedRefreshError = False
        if not health:
            return

        self._updateCameras(health.get('cameras') or [])
        self._updateProcesses(health.get('processes') or [],
                              health.get('cpuCount') or 1)
        self._updateSystem(health)
        self.Layout()


    ###########################################################
    def _updateCameras(self, cameras):
        """Repaint the camera table."""
        ctrl = self._cameraList
        # Rebuilding only when the row count changes keeps selection/scroll
        # stable during the common case (same cameras, changing numbers).
        if ctrl.GetItemCount() != len(cameras):
            ctrl.DeleteAllItems()
            for row, cam in enumerate(cameras):
                ctrl.InsertItem(row, cam.get('name', ''))

        for row, cam in enumerate(cameras):
            statusLabel, color = self._classifyCamera(cam)
            age = self._cleanAge(cam.get('lastFrameAgeSecs'))
            ctrl.SetItem(row, 0, cam.get('name', ''))
            ctrl.SetItem(row, 1, statusLabel)
            h = cam.get('health') or {}

            # The health snapshot contains the camera/source dimensions.
            # Do not use the analysis processing size here.
            width = h.get('width')
            height = h.get('height')
            if width and height:
                resolution = "%dx%d" % (int(width), int(height))
            else:
                resolution = "—"
            ctrl.SetItem(row, 2, resolution)

            # Prefer the measured analysis FPS when available.
            fps = h.get('analysisFps') or h.get('fps')
            ctrl.SetItem(row, 3, ("%.1f" % float(fps)) if fps else "—")
            ctrl.SetItem(row, 4, str(h.get('decoder') or "—"))

            # This is actual frame age from StreamReader, NOT recording-segment
            # age.  The latter was the old misleading "Lag" column.
            frameAge = h.get('lastFrameAgeSecs')
            if frameAge is None:
                frameAgeText = "—"
            elif float(frameAge) < 1.0:
                frameAgeText = "%.0f ms" % (max(0.0, float(frameAge)) * 1000.0)
            elif float(frameAge) < 10.0:
                frameAgeText = "%.1f s" % max(0.0, float(frameAge))
            else:
                frameAgeText = "%.0f s" % max(0.0, float(frameAge))
            ctrl.SetItem(row, 5, frameAgeText)

            # analysisFrames/analysisDropped are cumulative counters.  Compare
            # them with the previous UI poll so this column shows the drop rate
            # over the most recent health interval.
            delivered = h.get('analysisFrames')
            dropped = h.get('analysisDropped')
            droppedText = "—"
            if delivered is not None and dropped is not None:
                try:
                    delivered = int(delivered)
                    dropped = int(dropped)
                    previous = self._healthFrameCounters.get(cam.get('name'))
                    if previous is not None:
                        dDelivered = max(0, delivered - previous[0])
                        dDropped = max(0, dropped - previous[1])
                        total = dDelivered + dDropped
                        if total:
                            droppedText = "%.2f%%" % (100.0 * dDropped / total)
                        else:
                            droppedText = "0.00%"
                    self._healthFrameCounters[cam.get('name')] = (delivered, dropped)
                except Exception:
                    pass
            ctrl.SetItem(row, 6, droppedText)

            reconnects = cam.get('reconnects') or h.get('cameraReconnects') or 0
            ctrl.SetItem(row, 7, str(reconnects) if reconnects else "")
            details = cam.get('reason') or ""
            if h.get('gapFillDisabled'):
                details = (details + "  gap-fill disabled").strip()
            if h.get('audioOk') is False:
                details = (details + "  audio failed").strip()
            ctrl.SetItem(row, 8, details[:120])
            ctrl.SetItemTextColour(row, color)


    ###########################################################
    @staticmethod
    def _cleanAge(age):
        """Clamp a last-frame age to >= 0.

        The age is (NMS clock - camera-process frame timestamp); the processes'
        clocks can disagree by fractions of a second, which showed up as small
        NEGATIVE ages.  A frame from "the future" simply means "just now".
        """
        if age is None:
            return None
        return max(0.0, age)


    ###########################################################
    def _classifyCamera(self, cam):
        """Classify a camera row for display.

        Frames flowing are the ground truth for "running": the NMS status can
        legitimately sit at 'connecting' for a camera that is happily
        delivering frames (observed live), so status alone badly undercounts.
        Status is only trusted for the states frames can't tell apart:
        off/disabled vs failed vs still-connecting.

        @param  cam    A camera dict from getSystemHealth.
        @return label  Display string.
        @return color  Row colour.
        """
        status = cam.get('status', kCameraOff)
        enabled = cam.get('enabled')
        h = cam.get('health') or {}

        # TWO different clocks arrive under the same key, and picking the wrong
        # one is what made this view lie.
        #
        #   health.lastFrameAgeSecs  -- real frame ARRIVAL, computed in the
        #                               camera process from
        #                               StreamReader._last_stamp_ms.
        #   cam.lastFrameAgeSecs     -- NMS _cameraUpdateTimes, i.e. how far the
        #                               ANALYSIS pass has got.  It freezes on any
        #                               detection backlog.
        #
        # Classifying on the analysis clock reported "No frames" for cameras
        # visibly delivering 19fps (05_Gate_lr, 2026-08-29).  Frame arrival is
        # the only honest answer to "is this camera delivering frames?"; the
        # analysis clock is a real signal too, but a different one -- reported
        # separately below rather than mislabelled as a dead stream.
        age = h.get('lastFrameAgeSecs')
        if age is None:
            # No health snapshot yet (freshly started camera).  A poor proxy,
            # but better than no age at all.
            age = self._cleanAge(cam.get('lastFrameAgeSecs'))
        analysisAge = self._cleanAge(cam.get('lastFrameAgeSecs'))

        if enabled is False or status == kCameraOff:
            return "Off", _kMutedColor
        if age is not None and age <= _kStaleFrameSecs:
            # recorderAlive is only meaningful when a recorder EXISTS: getHealth
            # initialises it False and only overwrites it when _remux is set, so
            # without recorderPresent this fired on every camera that simply has
            # no recorder configured.
            if (h.get('recorderPresent') and
                    h.get('recorderAlive') is False and
                    h.get('state') == 'running'):
                return "Recorder", _kWarnColor
            if analysisAge is not None and analysisAge > _kStaleAnalysisSecs:
                # Frames are arriving; the analysis behind them has stalled.
                return "Analysis lag", _kWarnColor
            return "Running", _kOkColor          # frames flowing = running
        if status == kCameraFailed:
            return "Failed", _kBadColor
        if status == kCameraConnecting and age is None:
            return "Connecting", _kWarnColor     # never delivered a frame yet
        # Claims on/connecting but frames stopped -- the wedged-stream case.
        return "No frames", _kBadColor


    ###########################################################
    def _updateProcesses(self, procs, ncpu):
        """Repaint the process table.

        CPU is shown as percent of the whole machine (Task Manager style);
        the raw counter from the back end is percent of a single core.
        """
        ctrl = self._processList
        if ctrl.GetItemCount() != len(procs):
            ctrl.DeleteAllItems()
            for row, proc in enumerate(procs):
                ctrl.InsertItem(row, proc.get('name', ''))

        for row, proc in enumerate(procs):
            memMB = proc.get('memMB')
            cpuPct = proc.get('cpuPct')
            ctrl.SetItem(row, 0, proc.get('name', ''))
            ctrl.SetItem(row, 1, str(proc.get('pid') or ""))
            ctrl.SetItem(row, 2, "%d MB" % memMB if memMB else "—")
            ctrl.SetItem(row, 3, ("%.1f%%" % (cpuPct / ncpu))
                                 if cpuPct is not None else "—")


    ###########################################################
    def _updateSystem(self, health):
        """Repaint the summary + system lines."""
        cameras = health.get('cameras') or []
        counts = {}
        problems = []
        for c in cameras:
            label, _ = self._classifyCamera(c)
            counts[label] = counts.get(label, 0) + 1
            if label in ("Failed", "No frames", "Recorder", "Analysis lag"):
                problems.append(c.get('name', '?'))

        enabled = len(cameras) - counts.get("Off", 0)
        parts = ["%d of %d enabled cameras running" %
                 (counts.get("Running", 0), enabled)]
        if counts.get("Off"):
            parts.append("%d off" % counts["Off"])
        if counts.get("Connecting"):
            parts.append("%d connecting" % counts["Connecting"])
        if problems:
            shown = ", ".join(problems[:4])
            if len(problems) > 4:
                shown += " (+%d more)" % (len(problems) - 4)
            parts.append("problems: %s" % shown)
        self._summaryText.SetLabel("  •  ".join(parts))
        self._summaryText.SetForegroundColour(
            _kBadColor if problems else _kOkColor)

        lines = []
        disk = health.get('disk')
        if disk:
            lines.append("Disk: %s%% free (%s GB of %s GB) on %s" % (
                disk.get('pctFree', '?'), disk.get('freeGB', '?'),
                disk.get('totalGB', '?'), disk.get('path', '')))

        # System-wide + Sighthound memory.  The Sighthound total is the sum of
        # the per-process numbers already in the payload.
        mem = health.get('memory')
        if mem and mem.get('totalMB'):
            svMB = sum(p.get('memMB') or 0
                       for p in (health.get('processes') or []))
            usedMB = mem['totalMB'] - (mem.get('availMB') or 0)
            line = "Memory: %.1f GB of %.1f GB used (%s%%)" % (
                usedMB / 1024.0, mem['totalMB'] / 1024.0,
                mem.get('loadPct', '?'))
            if svMB:
                line += "  •  Sighthound: %.1f GB" % (svMB / 1024.0)
            lines.append(line)

        # GPU (omitted entirely on machines without nvidia-smi).
        gpu = health.get('gpu')
        if gpu:
            parts = []
            if gpu.get('utilPct') is not None:
                parts.append("GPU: %d%%" % gpu['utilPct'])
            # The decode engine is separate from the GPU/compute figure above
            # and is what camera NVDEC decoding actually loads.
            if gpu.get('decoderPct') is not None:
                parts.append("decode %d%%" % gpu['decoderPct'])
            if gpu.get('memUsedMB') is not None and gpu.get('memTotalMB'):
                parts.append("VRAM %.1f of %.1f GB" %
                             (gpu['memUsedMB'] / 1024.0,
                              gpu['memTotalMB'] / 1024.0))
            if gpu.get('tempC') is not None:
                parts.append("%d°C" % gpu['tempC'])
            if gpu.get('powerW') is not None:
                parts.append("%.0f W" % gpu['powerW'])
            if parts:
                lines.append("  •  ".join(parts))

        # Database health.  A damaged database is silent from the user's side
        # -- on 2026-08-04 playback simply stopped working and nothing said
        # why, while recording carried on writing clips nobody could see.
        # Report it; repairing stays a deliberate, app-stopped decision.
        db = health.get('database') or {}
        corrupt = db.get('corrupt') or []
        if corrupt:
            for c in corrupt:
                lines.append(
                    "DATABASE DAMAGED: %s — %s (%s errors so far). Recording "
                    "and playback will misbehave. Stop the app and check it; "
                    "nothing is repaired automatically." % (
                        os.path.basename(c.get('path', '?')) or '?',
                        c.get('error', 'unknown error'),
                        c.get('errors', '?')))
        lastClip = db.get('lastClipAddedMs') or 0
        if lastClip:
            quietMin = (time.time() * 1000.0 - lastClip) / 60000.0
            if quietMin > _kClipQuietWarnMins:
                lines.append(
                    "No clip has reached the database in %.0f minutes — "
                    "footage is being recorded but not registered, and will "
                    "not appear in Search." % quietMin)
        if corrupt and not problems:
            # Keep the headline red even when every camera looks fine.
            self._summaryText.SetForegroundColour(_kBadColor)

        offset = health.get('clockOffsetSecs')
        if offset is not None:
            if abs(offset) >= _kClockWarnSecs:
                lines.append("Clock: %.1f s %s real time — recorded times will "
                             "not match your cameras (run w32tm /resync)" %
                             (abs(offset),
                              "behind" if offset > 0 else "ahead of"))
            else:
                lines.append("Clock: in sync (%.2f s from real time)" % offset)

        # Per-process cpuPct is percent OF ONE CORE (100 = one full core);
        # divide by the core count for a machine-wide percentage, matching
        # what Task Manager shows.
        ncpu = health.get('cpuCount') or 1
        totalCpu = sum(p.get('cpuPct') or 0.0
                       for p in (health.get('processes') or []))
        if totalCpu:
            lines.append("Sighthound CPU: %.0f%% of the machine (%d cores)" %
                         (totalCpu / ncpu, ncpu))

        lines.append(self._formatFooter(health))
        self._systemText.SetLabel("\n".join(lines))


    ###########################################################
    def _formatFooter(self, health):
        """Build the last line: back end uptime and the models it's running.

        The model list comes from the detection service itself, so it reflects
        what is actually loaded -- not what the config asked for, which can
        differ when a load fails or an uninstalled model name falls back to
        the shipped default.
        """
        startedMs = health.get('startedMs')
        if startedMs:
            footer = "Running since %s" % time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(startedMs / 1000.0))
        else:
            # Older back end, or the start time hasn't been pushed yet.
            footer = "Updated %s" % time.strftime("%H:%M:%S")

        models = health.get('models')
        if models:
            footer += " using models: %s" % " - ".join(models)
        elif models is not None:
            # The service answered and is running nothing: no detection at all
            # is happening, which is worth saying out loud.
            footer += " — no detection models loaded"
        return footer


    ###########################################################
    def _formatAge(self, age):
        """Human-readable 'time since last frame'."""
        if age is None:
            return "—"
        if age < 2:
            return "now"
        if age < 60:
            return "%ds ago" % int(age)
        if age < 3600:
            return "%dm ago" % int(age / 60)
        return "%dh ago" % int(age / 3600)
