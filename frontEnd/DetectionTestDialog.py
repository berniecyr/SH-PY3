#!/usr/bin/env python

"""Developer > Detection Test Suite.

Replays archived clips from any camera through the real motion pipeline under
settings you choose, and shows what came out.  The question it exists to answer
is the one that otherwise costs a night per attempt: does turning this down calm
the false detections without losing the real ones?

Run one configuration to see every object it produced and its geometry, or sweep
every sensitivity level against the same footage to compare them side by side.

Nothing here writes to the live databases or changes any camera's settings --
"Load camera's settings" reads them in so you can start from what the camera is
actually doing, but saving is still the camera wizard's job.
"""

import datetime
import os
import threading
import time

import wx
import wx.adv

from vitaToolbox.wx.FontUtils import makeFontDefault

from frontEnd.FrontEndUtils import getUserLocalDataDir
from backEnd import DetectionReplay


# Constants...

_kBorder = 8
_kProgressPollMs = 120

# Mirrors CameraSetupWizard so the slider here reads the same as the one that
# actually sets the camera.
_kSensitivityNames = {1: "Very Low", 2: "Low", 3: "Medium",
                      4: "High", 5: "Very High"}
_kDefaultSensitivity = 3

_kDialogSize = (1000, 680)

# Columns of the results grid.  Span is first among the geometry because it is
# what separates real detections from noise here -- measured on 09_Jungle,
# median 251 px for real person/animal tracks against 14 px for night false
# positives, where blob AREA only separates them 3.8x.
_kColumns = (
    ("Time",       90),
    ("Dur (s)",    65),
    ("Frames",     60),
    # Travel is shown in the SAME 1280x720 reference units as the search filter
    # and the camera setting, so a number read here can be typed straight into
    # either.  The raw analysis-pixel value is half this on a 640x360 camera.
    ("Travel",     70),
    ("Net displ",  70),
    ("Area px2",   75),
    ("Area %",     60),
    ("Centre",     90),
    ("Filter",     70),
    ("YOLO",      150),
)

_kSweepColumns = (
    ("Sensitivity", 110),
    ("Shadows",      80),
    ("Objects",      70),
    ("Illum events", 90),
    ("Below thresh", 90),
    # A row is one configuration and can hold a dozen objects, so this counts
    # them by label ("person 1, unknown 12") rather than naming each.
    ("Classified",  190),
)

_kIntro = (
    "Replay archived clips through the motion pipeline with the settings below."
    "  Counts here are what the motion stage would produce; nothing is written"
    " to the database and no camera is changed.\n"
    "%s, so recent nights can be replayed but older footage generally cannot"
    " unless a rule saved those clips.  Decoding is the slow part -- start with"
    " a few minutes rather than a whole night."
)

# The retention window is a user setting (Options > Storage), not a constant --
# read it rather than quoting a number, which would be wrong for anyone who has
# changed it.  The fallback deliberately states no figure at all.
_kRetentionFmt = "Continuous footage is kept for %s"
_kRetentionHours = "%d hours (Options > Storage)"
_kRetentionUnknown = "Continuous footage is kept only for the cache duration set in Options"


##############################################################################
def _fmtMs(ms):
    """-> local "HH:MM:SS" for a message the user will read."""
    if ms is None:
        return "?"
    return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%H:%M:%S")


##############################################################################
class DetectionTestDialog(wx.Dialog):
    """Parameter form, run controls and results for a replay."""

    ###########################################################
    def __init__(self, parent, backEndClient):
        super(DetectionTestDialog, self).__init__(
            parent, -1, "Detection Test Suite",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self._backEndClient = backEndClient
        self._dataDir = None
        self._videoDir = None
        self._lastResults = None

        self._initPaths()
        self._initUi()

        self.SetSize(_kDialogSize)
        self.SetMinSize((820, 560))
        self.CenterOnParent()


    ###########################################################
    def _initPaths(self):
        """Ask the back end where storage lives; it is authoritative."""
        dataDir = getUserLocalDataDir()
        self._dataDir = os.path.join(dataDir, "videos") if dataDir else None
        if self._backEndClient is not None:
            try:
                storage = self._backEndClient.getStorageLocation()
                if storage:
                    self._dataDir = storage
            except Exception:
                pass
            try:
                video = self._backEndClient.getVideoLocation()
                if video:
                    self._videoDir = video
            except Exception:
                pass


    ###########################################################
    def _retentionPhrase(self):
        """-> how long continuous footage is kept, from the live setting.

        Asked rather than assumed: the shipped default is 48 hours but it is a
        user setting, so any figure hardcoded here is wrong for whoever changed
        it.  If the back end cannot be asked, name the setting without quoting a
        number at all -- a wrong number is worse than none, because it tells
        someone their footage should still be there when it is not.
        """
        hours = None
        if self._backEndClient is not None:
            try:
                hours = int(self._backEndClient.getCacheDuration())
            except Exception:
                hours = None
        if not hours or hours <= 0:
            return _kRetentionUnknown
        return _kRetentionFmt % (_kRetentionHours % hours)


    ###########################################################
    def _initUi(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, -1, _kIntro % self._retentionPhrase())
        intro.Wrap(_kDialogSize[0] - 4 * _kBorder)
        makeFontDefault(intro)
        sizer.Add(intro, 0, wx.ALL, _kBorder)

        sizer.Add(self._makeRangeSizer(), 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorder)
        sizer.Add(self._makeSettingsSizer(), 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _kBorder)
        sizer.Add(self._makeButtonSizer(), 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _kBorder)

        self._summary = wx.StaticText(self, -1, "")
        makeFontDefault(self._summary)
        sizer.Add(self._summary, 0, wx.ALL, _kBorder)

        self._resultList = wx.ListCtrl(
            self, -1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        for i, (label, width) in enumerate(_kColumns):
            self._resultList.InsertColumn(i, label, width=width)
        sizer.Add(self._resultList, 1,
                  wx.EXPAND | wx.LEFT | wx.RIGHT, _kBorder)

        closeSizer = self.CreateStdDialogButtonSizer(wx.CLOSE)
        sizer.Add(closeSizer, 0, wx.EXPAND | wx.ALL, _kBorder)
        self.SetEscapeId(wx.ID_CLOSE)
        self.Bind(wx.EVT_BUTTON, self._onClose, id=wx.ID_CLOSE)

        self.SetSizer(sizer)


    ###########################################################
    def _makeRangeSizer(self):
        box = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Footage")

        box.Add(wx.StaticText(self, -1, "Camera:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)
        self._camChoice = wx.Choice(self, -1, choices=self._cameraNames())
        if self._camChoice.GetCount():
            self._camChoice.SetSelection(0)
        box.Add(self._camChoice, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)

        box.Add(wx.StaticText(self, -1, "Date:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kBorder)
        self._datePicker = wx.adv.DatePickerCtrl(
            self, -1, style=wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY)
        box.Add(self._datePicker, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)

        now = wx.DateTime.Now()
        box.Add(wx.StaticText(self, -1, "From:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kBorder)
        start = wx.DateTime(now.GetDay(), now.GetMonth(), now.GetYear(), 0, 0, 0)
        self._startTime = wx.adv.TimePickerCtrl(self, -1, start)
        box.Add(self._startTime, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)

        box.Add(wx.StaticText(self, -1, "To:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kBorder)
        stop = wx.DateTime(now.GetDay(), now.GetMonth(), now.GetYear(), 1, 0, 0)
        self._stopTime = wx.adv.TimePickerCtrl(self, -1, stop)
        box.Add(self._stopTime, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)

        return box


    ###########################################################
    def _makeSettingsSizer(self):
        box = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Settings to test")

        box.Add(wx.StaticText(self, -1, "Motion sensitivity:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)
        self._sensitivityCtrl = wx.Slider(
            self, -1, _kDefaultSensitivity, 1, 5,
            style=wx.SL_HORIZONTAL | wx.SL_AUTOTICKS, size=(130, -1))
        self._sensitivityCtrl.SetTickFreq(1)
        self._sensitivityCtrl.Bind(wx.EVT_SLIDER, self._onSensitivityChange)
        box.Add(self._sensitivityCtrl, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)
        self._sensitivityValue = wx.StaticText(
            self, -1, _kSensitivityNames[_kDefaultSensitivity], size=(70, -1))
        box.Add(self._sensitivityValue, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kBorder)

        self._ignoreShadowsCtrl = wx.CheckBox(self, -1, "Ignore shadows")
        box.Add(self._ignoreShadowsCtrl, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kBorder)

        # Replays always record everything; this only marks which rows the search
        # filter would hide at a given threshold.  There is no record-time
        # suppression control here on purpose -- the filter made it redundant,
        # and having both invited setting the one that costs you the footage.
        box.Add(wx.StaticText(self, -1, "Filter at:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kBorder)
        self._filterTravelCtrl = wx.SpinCtrl(self, -1, min=0, max=500, initial=0,
                                             size=(70, -1))
        self._filterTravelCtrl.SetToolTip(
            "Search-filter preview: every detection is still recorded; rows this"
            " would hide are marked \"filtered\".  0 = show everything.")
        self._filterTravelCtrl.Bind(wx.EVT_SPINCTRL, self._onFilterTravelChange)
        box.Add(self._filterTravelCtrl, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL, _kBorder // 2)

        self._yoloCtrl = wx.CheckBox(self, -1, "Classify with YOLO")
        box.Add(self._yoloCtrl, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, _kBorder)

        return box


    ###########################################################
    def _makeButtonSizer(self):
        row = wx.BoxSizer(wx.HORIZONTAL)

        self._loadBtn = wx.Button(self, -1, "Load Camera's Settings")
        self._loadBtn.Bind(wx.EVT_BUTTON, self._onLoadSettings)
        row.Add(self._loadBtn, 0, wx.RIGHT, _kBorder)

        self._runBtn = wx.Button(self, -1, "Run")
        self._runBtn.Bind(wx.EVT_BUTTON, self._onRun)
        row.Add(self._runBtn, 0, wx.RIGHT, _kBorder)

        self._sweepBtn = wx.Button(self, -1, "Sweep All Levels")
        self._sweepBtn.Bind(wx.EVT_BUTTON, self._onSweep)
        row.Add(self._sweepBtn, 0, wx.RIGHT, _kBorder)

        hint = wx.StaticText(
            self, -1, "Sweep decodes once, then runs every level over the same"
                      " footage -- the first %d seconds of the range only, so"
                      " ten replays cannot exhaust memory."
                      % (DetectionReplay._kSweepWindowMs // 1000))
        makeFontDefault(hint)
        row.Add(hint, 0, wx.ALIGN_CENTER_VERTICAL)

        return row


    ###########################################################
    def _cameraNames(self):
        if self._backEndClient is None:
            return []
        try:
            return sorted(self._backEndClient.getCameraLocations())
        except Exception:
            return []


    ###########################################################
    def _onSensitivityChange(self, event=None):
        level = self._sensitivityCtrl.GetValue()
        self._sensitivityValue.SetLabel(_kSensitivityNames.get(level, ""))


    ###########################################################
    def _onLoadSettings(self, event=None):
        """Pull the selected camera's live settings into the form."""
        camLoc = self._selectedCamera()
        if not camLoc or self._backEndClient is None:
            return
        try:
            _type, _uri, _enabled, extra = \
                self._backEndClient.getCameraSettings(camLoc)
        except Exception as e:
            wx.MessageBox("Could not read that camera's settings:\n%s" % e,
                          "Detection Test Suite", wx.OK | wx.ICON_ERROR, self)
            return
        extra = extra or {}
        try:
            level = min(5, max(1, int(extra.get('sensitivity',
                                                _kDefaultSensitivity))))
        except (TypeError, ValueError):
            level = _kDefaultSensitivity
        self._sensitivityCtrl.SetValue(level)
        self._onSensitivityChange()
        self._ignoreShadowsCtrl.SetValue(bool(extra.get('ignoreShadows', False)))
        self._summary.SetLabel(
            "Loaded %s: sensitivity %d (%s), ignore shadows %s."
            % (camLoc, level, _kSensitivityNames.get(level, ""),
               "on" if extra.get('ignoreShadows') else "off"))


    ###########################################################
    def _onFilterTravelChange(self, event=None):
        """Re-mark the existing results at the new filter threshold.

        Re-renders from the last run rather than replaying, so the filter can be
        swept freely without paying for another decode.
        """
        r = self._lastResults
        if r and 'objects' in r and not r.get('error'):
            self._showRunResults(r)
        if event is not None:
            event.Skip()


    ###########################################################
    def _yoloThreshold(self):
        """The app's own YOLO floor, so a test classifies as production would.

        Hardcoding 0.25 here would quietly disagree with Options once that value
        is changed, and the run would look like a detector difference.
        """
        try:
            from backEnd import ImageCheckConfig
            return float(ImageCheckConfig.loadConfig().get(
                'YOLO_CONF_THRESHOLD',
                ImageCheckConfig.DEFAULTS['YOLO_CONF_THRESHOLD']))
        except Exception:
            return 0.25


    ###########################################################
    def _selectedCamera(self):
        idx = self._camChoice.GetSelection()
        if idx == wx.NOT_FOUND:
            return None
        return self._camChoice.GetString(idx)


    ###########################################################
    def _selectedRange(self):
        """-> (startMs, stopMs) or None after complaining."""
        wxDate = self._datePicker.GetValue()
        day = datetime.date(wxDate.GetYear(), wxDate.GetMonth() + 1,
                            wxDate.GetDay())
        startT = self._startTime.GetValue()
        stopT = self._stopTime.GetValue()
        start = datetime.datetime(day.year, day.month, day.day,
                                  startT.GetHour(), startT.GetMinute(),
                                  startT.GetSecond())
        stop = datetime.datetime(day.year, day.month, day.day,
                                 stopT.GetHour(), stopT.GetMinute(),
                                 stopT.GetSecond())
        if stop <= start:
            # A range that ends before it starts is almost always someone
            # meaning "overnight"; say so rather than silently returning zero.
            wx.MessageBox(
                "The end time is not after the start time.  For an overnight"
                " range, run it as two windows either side of midnight.",
                "Detection Test Suite", wx.OK | wx.ICON_INFORMATION, self)
            return None
        return (int(start.timestamp() * 1000), int(stop.timestamp() * 1000))


    ###########################################################
    def _checkPaths(self):
        if not self._dataDir or not os.path.exists(
                os.path.join(self._dataDir, "clipdb")):
            wx.MessageBox(
                "Could not find clipdb.  The back end must be reachable so the"
                " storage location can be read.",
                "Detection Test Suite", wx.OK | wx.ICON_ERROR, self)
            return False
        if not self._videoDir or not os.path.exists(self._videoDir):
            wx.MessageBox(
                "Could not find the video storage folder (%s)."
                % (self._videoDir or "unknown"),
                "Detection Test Suite", wx.OK | wx.ICON_ERROR, self)
            return False
        return True


    ###########################################################
    def _onRun(self, event=None):
        camLoc = self._selectedCamera()
        if not camLoc:
            return
        rng = self._selectedRange()
        if rng is None or not self._checkPaths():
            return
        startMs, stopMs = rng

        sensitivity = self._sensitivityCtrl.GetValue()
        shadows = self._ignoreShadowsCtrl.GetValue()
        yoloConf = self._yoloThreshold() if self._yoloCtrl.GetValue() else None

        def work(progress, cancelled, _stage):
            # minTravel is left at its default of 0: a replay records everything
            # and the filter is applied afterwards, so nothing is ever lost to a
            # threshold set here.
            return DetectionReplay.replayRange(
                self._dataDir, self._videoDir, camLoc, startMs, stopMs,
                sensitivity=sensitivity, ignoreShadows=shadows,
                yoloConf=yoloConf,
                progressFn=progress, cancelFn=cancelled)

        results = self._runThreaded("Replaying %s" % camLoc, work)
        if results is None:
            return
        if results.get('error'):
            wx.MessageBox(results['error'], "Detection Test Suite",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        self._showRunResults(results)


    ###########################################################
    def _onSweep(self, event=None):
        camLoc = self._selectedCamera()
        if not camLoc:
            return
        rng = self._selectedRange()
        if rng is None or not self._checkPaths():
            return
        startMs, stopMs = rng

        # The same threshold Run uses, so a sweep classifies as production
        # would.  Sweep used to drop this on the floor: the checkbox was read
        # nowhere and every row came back unlabelled.
        yoloConf = self._yoloThreshold() if self._yoloCtrl.GetValue() else None

        def work(progress, cancelled, stage):
            return DetectionReplay.sweep(
                self._dataDir, self._videoDir, camLoc, startMs, stopMs,
                yoloConf=yoloConf,
                progressFn=progress, cancelFn=cancelled, stageFn=stage)

        results = self._runThreaded("Sweeping %s" % camLoc, work)
        if results is None:
            return
        if results.get('error'):
            wx.MessageBox(results['error'], "Detection Test Suite",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        self._showSweepResults(results)


    ###########################################################
    def _runThreaded(self, title, workFn):
        """Run workFn off the main thread behind a cancellable progress dialog.

        workFn(progressFn, cancelFn, stageFn) must not touch wx.  Same shape as
        SystemHealthReports._runThreaded, which is the established idiom here.

        The stage matters on a sweep: decoding is a small fraction of the run and
        the ten replays that follow report no frames at all, so a label built only
        from the frame count sits still for most of an eight-minute run and reads
        as a hang.
        """
        state = {"frames": 0, "stage": ""}
        result = {}
        cancelled = threading.Event()

        def progress(nFrames):
            state["frames"] = nFrames

        def stage(text):
            state["stage"] = text

        def isCancelled():
            return cancelled.is_set()

        def run():
            try:
                result["value"] = workFn(progress, isCancelled, stage)
            except Exception as e:              # surfaced on the main thread
                result["error"] = e

        progDlg = wx.ProgressDialog(
            title, "Decoding footage…".ljust(60), maximum=100, parent=self,
            style=wx.PD_CAN_ABORT | wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
        worker = threading.Thread(target=run, name="detectionReplay")
        worker.daemon = True
        worker.start()
        try:
            while worker.is_alive():
                # Frame totals are not known until decoding finishes, so pulse
                # rather than pretending to a percentage.
                if state["stage"] and state["stage"] != "Decoding footage":
                    label = "%s…" % state["stage"]
                else:
                    label = "Decoded %d frames…" % state["frames"]
                keepGoing, _ = progDlg.Pulse(label.ljust(60))
                if not keepGoing:
                    cancelled.set()
                wx.MilliSleep(_kProgressPollMs)
                wx.SafeYield(progDlg, True)
            worker.join(10.0)
        finally:
            progDlg.Destroy()

        if cancelled.is_set():
            return None
        if "error" in result:
            wx.MessageBox("The replay failed:\n%s" % result["error"],
                          "Detection Test Suite", wx.OK | wx.ICON_ERROR, self)
            return None
        return result.get("value")


    ###########################################################
    def _resetColumns(self, columns):
        self._resultList.ClearAll()
        for i, (label, width) in enumerate(columns):
            self._resultList.InsertColumn(i, label, width=width)


    ###########################################################
    def _showRunResults(self, r):
        self._lastResults = r
        self._resetColumns(_kColumns)

        procW, procH = r['procSize']
        frameArea = float(procW * procH)
        # Thresholds are quoted at 1280x720 and scale linearly, matching
        # VideoPipeline and MinTravelTrigger.  toRef converts a measured
        # analysis-pixel value the other way, so the Travel column reads in the
        # same units as the search filter and the camera setting.
        linScale = ((procW * procH) / (1280.0 * 720.0)) ** 0.5
        toRef = (1.0 / linScale) if linScale else 1.0

        filterRef = self._filterTravelCtrl.GetValue()
        hidden = 0
        for o in r['objects']:
            travelRef = o['travel'] * toRef
            # Every object here was recorded; the mark only says which ones the
            # search filter would hide at the current threshold.
            if filterRef > 0 and travelRef < filterRef:
                note = "filtered"
                hidden += 1
            else:
                note = ""
            idx = self._resultList.InsertItem(
                self._resultList.GetItemCount(),
                datetime.datetime.fromtimestamp(
                    o['firstMs'] / 1000.0).strftime("%H:%M:%S"))
            self._resultList.SetItem(idx, 1, "%.1f" % o['durSec'])
            self._resultList.SetItem(idx, 2, "%d" % o['nFrames'])
            self._resultList.SetItem(idx, 3, "%.0f" % travelRef)
            self._resultList.SetItem(idx, 4, "%.0f" % (o['netDispl'] * toRef))
            self._resultList.SetItem(idx, 5, "%.0f" % o['meanArea'])
            self._resultList.SetItem(idx, 6,
                                     "%.2f" % (100.0 * o['meanArea'] / frameArea))
            self._resultList.SetItem(idx, 7, "%.0f,%.0f" % (o['cx'], o['cy']))
            self._resultList.SetItem(idx, 8, note)
            self._resultList.SetItem(idx, 9, str(o.get('yolo', "")))

        filterNote = ""
        if filterRef > 0:
            kept = r['nObjects'] - hidden
            filterNote = ("   Search filter at %d would show %d of %d (%.0f%%)."
                          % (filterRef, kept, r['nObjects'],
                             100.0 * kept / r['nObjects'] if r['nObjects'] else 0))
        classifyNote = ""
        if r.get('classifyTruncated'):
            # Objects past this point are marked "no frame" rather than
            # classified against distant footage; say why, or the column reads
            # like the detector looked and found nothing.
            classifyNote = ("   NOTE: only footage up to %s was kept for"
                            " classification, so later objects show \"no"
                            " frame\"." % _fmtMs(r['classifiedToMs']))
        self._summary.SetLabel(
            "%d objects from %d clips.  Analysis frame %dx%d at %s fps, %d frames"
            " scored (+%d primed to warm the background), %d illumination events,"
            " %.1fs.%s%s%s"
            % (r['nObjects'], r['nClips'], procW, procH,
               r.get('analysisFps') or "all", r['framesScored'],
               r['framesPrimed'], r['illumEvents'], r['elapsedSec'], filterNote,
               classifyNote,
               DetectionReplay.describeProblems(r.get('problems'))))
        self.Layout()


    ###########################################################
    def _showSweepResults(self, r):
        self._lastResults = r
        self._resetColumns(_kSweepColumns)

        procW, procH = r['procSize']
        linScale = ((procW * procH) / (1280.0 * 720.0)) ** 0.5
        # How many objects at each sensitivity level the search filter would hide.
        threshold = int(round(self._filterTravelCtrl.GetValue() * linScale))

        for row in r['rows']:
            level = row['sensitivity']
            under = sum(1 for o in row['objects']
                        if threshold > 0 and o['travel'] < threshold)
            idx = self._resultList.InsertItem(
                self._resultList.GetItemCount(),
                "%d  %s" % (level, _kSensitivityNames.get(level, "")))
            self._resultList.SetItem(idx, 1,
                                     "on" if row['ignoreShadows'] else "off")
            self._resultList.SetItem(idx, 2, "%d" % row['nObjects'])
            self._resultList.SetItem(idx, 3, "%d" % row['illumEvents'])
            self._resultList.SetItem(idx, 4, "%d" % under if threshold else "")
            self._resultList.SetItem(idx, 5, row.get('yoloSummary', ""))

        notes = []
        if r.get('windowClamped'):
            # The user asked for a longer range than a sweep will replay ten
            # times.  Name the window actually swept against the one requested,
            # so a low count is never read as "the camera was quiet".
            notes.append("   NOTE: swept %s-%s only -- the first %d seconds of"
                         " the %s-%s range selected."
                         % (_fmtMs(r['startMs']), _fmtMs(r['stopMs']),
                            DetectionReplay._kSweepWindowMs // 1000,
                            _fmtMs(r['startMs']),
                            _fmtMs(r['requestedStopMs'])))
        if r.get('truncated'):
            # Say it plainly: a silently shortened sweep would look like a
            # quieter camera rather than a shorter sample.
            notes.append("   NOTE: the decode cache filled before the window"
                         " ended, so these counts are a floor.")
        self._summary.SetLabel(
            "Swept %d configurations over the same %d frames at %dx%d, %s fps"
            " (%d inside the window), in %.1fs.%s%s"
            % (len(r['rows']), r['framesDecoded'], procW, procH,
               r.get('analysisFps') or "all", r.get('framesInWindow', 0),
               r['elapsedSec'], "".join(notes),
               DetectionReplay.describeProblems(r.get('problems'))))
        self.Layout()


    ###########################################################
    def _onClose(self, event=None):
        self.EndModal(wx.ID_CLOSE)


##############################################################################
def showDetectionTest(parent, backEndClient=None):
    """Open the Detection Test Suite dialog."""
    dlg = DetectionTestDialog(parent, backEndClient)
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()
