#! /usr/bin/env python

#*****************************************************************************
#
# SystemHealthReports.py
#     The Reports menu: ask for a window, run a scan, save the result.
#
#*****************************************************************************

r"""The UI behind the Reports menu.

Three reports, one flow.  Each asks for a start moment, asks where to save,
runs, writes the file and offers to open it -- deliberately identical, because
the reports differ in what they read, not in how you ask for them.

The save location is asked for BEFORE the scan runs.  Camera health reads
~258 MB of rotated logs and takes about 80 seconds; throwing that away because
the save dialog was cancelled afterwards would be its own small tragedy.

Anything that takes long enough to notice runs on a worker thread with a
cancellable progress dialog.  The worker never touches wx: it writes into a
dict and asks an Event whether to stop, while the main thread owns the dialog.
The detections report is exempt -- it is two GROUP BYs and finishes in well
under a second, where a progress dialog would flash rather than inform.
"""

import os
import datetime
import threading
import time

import wx
import wx.adv

from vitaToolbox.wx.FontUtils import makeFontDefault
from vitaToolbox.wx.VitaDatePickerCtrl import VitaDatePickerCtrl
from vitaToolbox.sysUtils.TimeUtils import formatTime

from appCommon.CommonStrings import kClipDbFile, kObjDbFile

from frontEnd.FrontEndUtils import getUserLocalDataDir
from frontEnd import SystemHealthCamReport
from frontEnd import SystemHealthDetectionReport
from frontEnd import SystemHealthLogReport


# Constants...

_kBorder = 12

# Extra width for the report dialog.  Fitting to the sizer alone truncates the
# date picker's label; this is the slack that keeps it readable.
_kReportDialogExtraWidth = 90

# Slack added to the re-measured date label, so a slightly wider weekday or
# locale format still fits rather than being silently truncated.
_kDateLabelPad = 8

# How often the main thread repaints the progress dialog while a worker runs.
_kProgressPollMs = 120

kCamHealth = "camhealth"
kDetections = "detections"
kLogErrors = "logerrors"

# Per-report wording and file naming.  Kept together so the three reports stay
# recognisably the same dialog rather than drifting apart.
_kSpecs = {
    kCamHealth: {
        "title":  "Camera Health Report",
        "intro":  "Report on camera clock jumps, outages and recording"
                  " coverage\nfrom the chosen moment up to now.",
        "note":   "Scanning several days of logs takes a minute or so; you can"
                  " cancel while it runs.",
        "prefix": "CameraHealth",
        "levels": False,
    },
    kDetections: {
        "title":  "Detections by Camera",
        "intro":  "Count what each camera detected, by type, from the chosen"
                  " moment\nup to now.",
        "note":   "Reads the object database directly, so this is quick.",
        "prefix": "Detections",
        "levels": False,
    },
    kLogErrors: {
        "title":  "Log Errors and Warnings",
        "intro":  "List everything logged as an error or a warning, grouped by"
                  " message\nand then in full, from the chosen moment up to"
                  " now.",
        "note":   "Reads every system and camera log.  A wide window produces a"
                  " large file.",
        "prefix": "LogErrors",
        "levels": True,
    },
}

_kLevelChoices = ("Errors only", "Warnings only", "Both")
_kLevelValues = ((SystemHealthLogReport.kErrorLevel,),
                 (SystemHealthLogReport.kWarningLevel,),
                 SystemHealthLogReport.kAllLevels)
_kLevelDefault = 2


##############################################################################
class _ReportRangeDialog(wx.Dialog):
    """Asks for the moment the report should start from.

    The report always runs to "now" -- there is no end picker, because the
    question it answers is "how has the fleet behaved since X".  The default is
    the oldest moment still in the logs, and the picker is bounded by it so a
    window with no data cannot be chosen.
    """

    ###########################################################
    def __init__(self, parent, spec, oldestEpoch):
        super(_ReportRangeDialog, self).__init__(parent, -1, spec["title"])

        self._spec = spec
        oldestLocal = time.localtime(oldestEpoch)
        oldestDate = datetime.date(oldestLocal.tm_year, oldestLocal.tm_mon,
                                   oldestLocal.tm_mday)

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, -1, spec["intro"])
        sizer.Add(intro, 0, wx.ALL, _kBorder)

        rowSizer = wx.BoxSizer(wx.HORIZONTAL)
        rowSizer.Add(wx.StaticText(self, -1, "Start from:"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kBorder)

        # Bounded at BOTH ends: earlier than the oldest log reports on nothing,
        # and later than today reports on a window that has not happened.
        self._datePicker = VitaDatePickerCtrl(self, initialDate=oldestDate,
                                              earliestDate=oldestDate,
                                              latestDate="today")
        self._widenDateLabel(self._datePicker)
        rowSizer.Add(self._datePicker, 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kBorder)

        wxWhen = wx.DateTime(oldestLocal.tm_mday, oldestLocal.tm_mon - 1,
                             oldestLocal.tm_year, oldestLocal.tm_hour,
                             oldestLocal.tm_min, 0)
        self._timePicker = wx.adv.TimePickerCtrl(self, -1, wxWhen)
        rowSizer.Add(self._timePicker, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(rowSizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        # State the floor explicitly: the logs rotate, so "oldest available"
        # moves, and picking earlier would silently report on nothing.
        oldestText = wx.StaticText(
            self, -1, "Oldest available in the logs: %s"
            % time.strftime("%Y-%m-%d %H:%M", oldestLocal))
        makeFontDefault(oldestText)
        sizer.Add(oldestText, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        self._levelBox = None
        if spec["levels"]:
            self._levelBox = wx.RadioBox(self, -1, "Include",
                                         choices=list(_kLevelChoices))
            self._levelBox.SetSelection(_kLevelDefault)
            sizer.Add(self._levelBox, 0,
                      wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        note = wx.StaticText(self, -1, spec["note"])
        makeFontDefault(note)
        sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, _kBorder)

        btnSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btnSizer, 0, wx.EXPAND | wx.ALL, _kBorder)
        self.Bind(wx.EVT_BUTTON, self._onOk, id=wx.ID_OK)

        self.SetSizerAndFit(sizer)
        # Fit() alone leaves the date label clipped; give the row breathing
        # room rather than letting the widest child define the whole width.
        w, h = self.GetSize()
        self.SetMinSize((w + _kReportDialogExtraWidth, h))
        self.SetSize((w + _kReportDialogExtraWidth, h))
        self.CenterOnParent()


    ###########################################################
    def _onOk(self, event):
        """Refuse a start moment in the future.

        The date picker already bars future DATES; this catches today plus a
        time that has not arrived, which would report on an empty window.
        """
        if self.getSince() > time.time():
            wx.MessageBox(
                "That start time is in the future.  Choose a moment that has"
                " already passed.",
                self._spec["title"], wx.OK | wx.ICON_INFORMATION, self)
            return
        self.EndModal(wx.ID_OK)


    ###########################################################
    @staticmethod
    def _widenDateLabel(picker):
        """Give the date button room for the label it will actually draw.

        VitaDatePickerCtrl reserves a width measured during its own __init__,
        and HoverButton silently TRUNCATES its label to whatever width it has
        (see HoverButton.Draw -> truncateText).  Measured here: once a mid-week
        date is chosen, "Mon 08/17/26" needs 82px against the 77px reserved and
        comes out clipped, while "Fri 08/14/26" (70px), "Today" (41px) and
        "Yesterday" (59px) all fit -- which is why it only showed up after a
        date was picked.

        Re-measure with the button as it is now realized, across the same date
        formats the control itself uses, and keep the widest.  Locale-correct,
        rather than padding by a guessed number of pixels.
        """
        try:
            button = picker._dateButton
            restore = button._label
            widest = button.GetMinSize()[0]
            for i in range(7):
                day = datetime.date(2026, 12, 20 + i)
                for fmt in ("%a %m/%d/%y", "%a %d.%m.%Y", "%a %Y-%m-%d"):
                    button.SetLabel(formatTime(fmt, day))
                    widest = max(widest, button.DoGetBestSize()[0])
            button.SetLabel(restore)
            button.SetMinSize((widest + _kDateLabelPad, -1))
            picker.Layout()
            picker.SetMinSize(picker.GetBestSize())
        except Exception:
            pass            # cosmetic only -- never block the dialog over it


    ###########################################################
    def getSince(self):
        """-> epoch seconds for the chosen date and time."""
        d = self._datePicker.GetValue()
        t = self._timePicker.GetValue()
        return time.mktime((d.year, d.month, d.day,
                            t.GetHour(), t.GetMinute(), 0, 0, 0, -1))


    ###########################################################
    def getLevels(self):
        """-> the chosen log levels, or None if this report has no choice."""
        if self._levelBox is None:
            return None
        return _kLevelValues[self._levelBox.GetSelection()]


###########################################################
_kCancelledExceptions = (SystemHealthCamReport._Cancelled,
                         SystemHealthLogReport._Cancelled,
                         SystemHealthDetectionReport._Cancelled)


###########################################################
def _runThreaded(parent, title, buildFn, unitTotal, unitNoun):
    """Run buildFn on a worker thread; return its text, or None.

    `buildFn(progress)` returns the report text.  It runs off the main thread
    and must not touch wx -- it reports through `progress` and is stopped by
    returning False from it.  This thread owns the dialog and does the yielding.
    """
    unitTotal = max(int(unitTotal), 1)
    state = {"done": 0, "what": ""}
    result = {}
    cancelled = threading.Event()

    def progress(done, total, what):
        state["done"], state["what"] = done, what
        return not cancelled.is_set()

    def work():
        try:
            result["text"] = buildFn(progress)
        except _kCancelledExceptions:
            result["cancelled"] = True
        except Exception as e:              # noqa: BLE001 - surfaced below
            result["error"] = e

    idle = "Scanning %s\u2026" % unitNoun
    progDlg = wx.ProgressDialog(
        title, idle.ljust(60), maximum=unitTotal, parent=parent,
        style=wx.PD_CAN_ABORT | wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
    worker = threading.Thread(target=work, name="systemHealthReport")
    worker.daemon = True
    worker.start()
    try:
        while worker.is_alive():
            msg = ("Scanning %s\u2026" % state["what"]) if state["what"] else idle
            keepGoing, _ = progDlg.Update(min(state["done"], unitTotal),
                                          msg.ljust(60))
            if not keepGoing:
                cancelled.set()
            wx.MilliSleep(_kProgressPollMs)
            wx.SafeYield(progDlg, True)
        worker.join(5.0)
    finally:
        progDlg.Destroy()

    if result.get("cancelled") or cancelled.is_set():
        return None
    if "error" in result:
        wx.MessageBox("The report failed:\n%s" % result["error"],
                      title, wx.OK | wx.ICON_ERROR, parent)
        return None
    return result.get("text")


###########################################################
def _storageDir(backEndClient, dataDir):
    """-> where clipdb and objdb2 live.

    The back end is authoritative (the user can move storage), so ask it the
    same way FrontEndFrame does.  Fall back to the conventional location only
    when it cannot be asked.
    """
    if backEndClient is not None:
        try:
            storage = backEndClient.getStorageLocation()
            if storage:
                return storage
        except Exception:
            pass
    return os.path.join(dataDir, "videos") if dataDir else None


###########################################################
def showReport(parent, kind, backEndClient=None):
    """Run one of the Reports-menu items, end to end.

    @param  parent          Window to parent the dialogs on.
    @param  kind            kCamHealth, kDetections or kLogErrors.
    @param  backEndClient   Used to locate storage; None is tolerated.
    """
    spec = _kSpecs[kind]
    title = spec["title"]

    dataDir = getUserLocalDataDir()
    if not dataDir:
        wx.MessageBox(
            "The data directory could not be determined, which usually means"
            " the back end is not running.  Start it and try again.",
            title, wx.OK | wx.ICON_INFORMATION, parent)
        return

    logDir = os.path.join(dataDir, "logs")
    camLogDir = os.path.join(logDir, "cameras")
    if not os.path.isdir(camLogDir):
        wx.MessageBox("No camera logs found at:\n%s" % camLogDir,
                      title, wx.OK | wx.ICON_INFORMATION, parent)
        return

    # Default to the oldest moment the logs still hold -- cheap (~0.1s),
    # because logs_for() stops at the first parseable line of each file.  The
    # detections report reads a database rather than the logs, but the two are
    # trimmed on similar schedules and its header states its own real span.
    oldest = SystemHealthCamReport.oldestLogTime(camLogDir)
    if oldest is None:
        wx.MessageBox("The camera logs hold no timestamped entries yet.",
                      title, wx.OK | wx.ICON_INFORMATION, parent)
        return

    dlg = _ReportRangeDialog(parent, spec, oldest)
    try:
        if dlg.ShowModal() != wx.ID_OK:
            return
        since = dlg.getSince()
        levels = dlg.getLevels()
    finally:
        dlg.Destroy()

    # Ask where to save BEFORE scanning: cancelling afterwards would throw away
    # up to ~80s of work for nothing.  The name carries the time the report was
    # RUN, not the window it covers -- two reports on the same window are
    # different documents, and the newer one must not silently replace the old.
    defName = "%s_%s.txt" % (spec["prefix"],
                             time.strftime("%Y-%m-%d_%H%M%S"))
    fileDlg = wx.FileDialog(parent, "Save %s" % title.lower(), "", defName,
                            "Text files (*.txt)|*.txt|All files (*.*)|*.*",
                            wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
    try:
        if fileDlg.ShowModal() != wx.ID_OK:
            return
        savePath = fileDlg.GetPath()
    finally:
        fileDlg.Destroy()

    text = _build(parent, kind, spec, since, levels, dataDir, logDir,
                  camLogDir, backEndClient)
    if text is None:
        return                  # cancelled, or already reported an error

    try:
        with open(savePath, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        wx.MessageBox("Could not write the report:\n%s" % e,
                      title, wx.OK | wx.ICON_ERROR, parent)
        return

    answer = wx.MessageBox("Report saved to:\n%s\n\nOpen it now?" % savePath,
                           title, wx.YES_NO | wx.ICON_INFORMATION, parent)
    if answer == wx.YES:
        try:
            os.startfile(savePath)          # Windows-only, as is this app
        except Exception:
            pass


###########################################################
def _build(parent, kind, spec, since, levels, dataDir, logDir, camLogDir,
           backEndClient):
    """-> the report text, or None if it was cancelled or failed."""
    title = spec["title"]
    storage = _storageDir(backEndClient, dataDir)

    if kind == kCamHealth:
        clipDb = os.path.join(storage, kClipDbFile) if storage else None
        try:
            nCams = len(SystemHealthCamReport.camera_names(camLogDir))
        except Exception:
            nCams = 0
        return _runThreaded(
            parent, title,
            lambda p: SystemHealthCamReport.buildReport(
                since=since, logdir=camLogDir, clipdb=clipDb, progress=p),
            nCams, "camera logs")

    if kind == kLogErrors:
        try:
            nFiles = len(SystemHealthLogReport.logFiles(logDir))
        except Exception:
            nFiles = 0
        return _runThreaded(
            parent, title,
            lambda p: SystemHealthLogReport.buildReport(
                since=since, levels=levels, logdir=logDir, progress=p),
            nFiles, "logs")

    if kind == kDetections:
        # Sub-second; a progress dialog here would flash rather than inform.
        objDb = os.path.join(storage, kObjDbFile) if storage else None
        busy = wx.BusyCursor()
        try:
            return SystemHealthDetectionReport.buildReport(
                since=since, objdb=objDb)
        except Exception as e:              # noqa: BLE001 - surfaced below
            del busy
            wx.MessageBox("The report failed:\n%s" % e,
                          title, wx.OK | wx.ICON_ERROR, parent)
            return None

    raise ValueError("unknown report kind: %r" % (kind,))
