#!/usr/bin/env python

#*****************************************************************************
#
# ClipRecordDialog.py
#
#*****************************************************************************

import datetime
import json
import os

import wx

from appCommon.CommonStrings import kFrontEndLogName
from vitaToolbox.loggingUtils.LoggingUtils import getLogger


# Columns that exist in the objects table but are never written by the
# detection pipeline -- listing them would only ever print blanks.
_kDeadObjectColumns = ('fileName', 'rX1', 'rY1', 'rX2', 'rY2',
                       'confidence', 'thumbnail')

# minWidth/maxWidth/minHeight/maxHeight are seeded with sentinels (min = the
# full frame, max = 0) and only narrowed once frames arrive, so an object with
# no motion rows keeps nonsense values.  maxWidth > 0 means real data.
_kSizeQuery = ("SELECT minWidth, maxWidth, minHeight, maxHeight "
               "FROM objects WHERE uid=%d")


def _fmtMs(ms):
    if ms is None:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000.0).strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:-3]


def _fmtRel(ms, base):
    if ms is None or base is None:
        return "-"
    s = (ms - base) / 1000.0
    sign = "-" if s < 0 else ""
    s = abs(s)
    return "%s%d:%05.2f" % (sign, int(s // 60), s % 60)


def _edge(box, procSize):
    """Which frame edge a box touches -- 'entered from the left' etc."""
    if not box or not procSize or not procSize[0]:
        return "?"
    w, h = procSize
    x1, y1, x2, y2 = box
    tol = max(4, int(w * 0.01))
    parts = []
    if x1 <= tol:
        parts.append("left")
    if x2 >= w - tol:
        parts.append("right")
    if y1 <= tol:
        parts.append("top")
    if y2 >= h - tol:
        parts.append("bottom")
    return "/".join(parts) if parts else "mid-frame"


class ClipRecordDialog(wx.Dialog):
    """Everything stored about one clip's detections, with a JSON export.

    The search panel deliberately shows a short summary; this is the full
    record behind it -- every attribute the pipeline wrote, plus the motion
    track summary that never had anywhere to go.
    """

    def __init__(self, parent, dataMgr, objList, camLoc=None,
                 clipStartMs=None, clipStopMs=None):
        super(ClipRecordDialog, self).__init__(
            parent, title="Detection record",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self._logger = getLogger(kFrontEndLogName)
        self._dataMgr = dataMgr
        self._objList = list(objList or [])
        self._camLoc = camLoc
        self._clipStartMs = clipStartMs
        self._clipStopMs = clipStopMs

        self._record = self._buildRecord()

        sizer = wx.BoxSizer(wx.VERTICAL)

        self._text = wx.TextCtrl(
            self, value=self._renderText(self._record),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP |
                  wx.TE_RICH2)
        mono = wx.Font(9 if wx.Platform == '__WXMSW__' else 11,
                       wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                       wx.FONTWEIGHT_NORMAL)
        self._text.SetFont(mono)
        sizer.Add(self._text, 1, wx.EXPAND | wx.ALL, 8)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        exportBtn = wx.Button(self, label="Export JSON...")
        copyBtn = wx.Button(self, label="Copy")
        closeBtn = wx.Button(self, wx.ID_CANCEL, label="Close")
        exportBtn.Bind(wx.EVT_BUTTON, self.OnExport)
        copyBtn.Bind(wx.EVT_BUTTON, self.OnCopy)
        btnSizer.Add(exportBtn, 0, wx.RIGHT, 6)
        btnSizer.Add(copyBtn, 0, wx.RIGHT, 6)
        btnSizer.AddStretchSpacer(1)
        btnSizer.Add(closeBtn, 0)
        sizer.Add(btnSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(sizer)
        self.SetSize((820, 560))
        self.SetMinSize((520, 320))
        closeBtn.SetDefault()
        self.CenterOnParent()

    # ── Record assembly ───────────────────────────────────────────────────

    def _buildRecord(self):
        """Collect everything stored about these objects into a plain dict."""
        rec = {
            'camera': self._camLoc,
            'clipStartMs': self._clipStartMs,
            'clipStopMs': self._clipStopMs,
            'clipStart': _fmtMs(self._clipStartMs),
            'clipStop': _fmtMs(self._clipStopMs),
            'objectCount': len(self._objList),
            'objects': [],
        }
        if not self._objList:
            return rec

        try:
            types = self._dataMgr.getObjectTypes(self._objList)
            attrs = self._dataMgr.getObjectAttributes(self._objList)
        except Exception:
            self._logger.warning("ClipRecordDialog: attribute query failed",
                                 exc_info=True)
            types, attrs = {}, {}

        procSize = None
        if self._camLoc:
            try:
                procSize = self._dataMgr.getProcSize(self._camLoc)
            except Exception:
                procSize = None
        rec['analysisSize'] = ("%dx%d" % tuple(procSize)) if procSize else None

        # One batched query for every track, then split it per object.
        tracks = {}
        try:
            for row in self._dataMgr.getObjectBboxesBetweenTimes(
                    self._objList):
                x1, y1, x2, y2, frame, ms, objId = row
                tracks.setdefault(objId, []).append((ms, frame,
                                                     (x1, y1, x2, y2)))
        except Exception:
            self._logger.warning("ClipRecordDialog: track query failed",
                                 exc_info=True)

        for objId in self._objList:
            info = None
            try:
                info = self._dataMgr.getObjectInfo(objId)
            except Exception:
                pass
            camLoc, tStart, tStop = info if info else (None, None, None)

            obj = {
                'uid': objId,
                'type': types.get(objId, 'unknown'),
                'camera': camLoc,
                'timeStartMs': tStart,
                'timeStopMs': tStop,
                'timeStart': _fmtMs(tStart),
                'timeStop': _fmtMs(tStop),
                'clipOffsetStart': _fmtRel(tStart, self._clipStartMs),
                'clipOffsetStop': _fmtRel(tStop, self._clipStartMs),
                'durationSecs': (round((tStop - tStart) / 1000.0, 2)
                                 if (tStart is not None and tStop is not None)
                                 else None),
                'attributes': attrs.get(objId),
            }

            track = tracks.get(objId, [])
            if track:
                boxes = [b for (_ms, _f, b) in track]
                obj['track'] = {
                    'frames': len(track),
                    'firstMs': track[0][0],
                    'lastMs': track[-1][0],
                    'firstFrame': track[0][1],
                    'lastFrame': track[-1][1],
                    'firstBox': list(track[0][2]),
                    'lastBox': list(track[-1][2]),
                    'enteredAt': _edge(track[0][2], procSize),
                    'exitedAt': _edge(track[-1][2], procSize),
                    'envelope': [min(b[0] for b in boxes),
                                 min(b[1] for b in boxes),
                                 max(b[2] for b in boxes),
                                 max(b[3] for b in boxes)],
                }
            else:
                obj['track'] = None

            size = self._objectSize(objId)
            if size:
                obj['sizeRange'] = size

            rec['objects'].append(obj)

        return rec

    def _objectSize(self, objId):
        """min/max box size, or None when the sentinels were never narrowed."""
        try:
            rows = self._dataMgr.doCustomSearch(_kSizeQuery % int(objId))
        except Exception:
            return None
        if not rows:
            return None
        minW, maxW, minH, maxH = rows[0]
        if not maxW or not maxH:
            return None          # object never got a frame; sentinels intact
        return {'minWidth': minW, 'maxWidth': maxW,
                'minHeight': minH, 'maxHeight': maxH}

    # ── Text rendering ────────────────────────────────────────────────────

    def _renderText(self, rec):
        L = []
        L.append("Camera        : %s" % (rec.get('camera') or "-"))
        L.append("Clip          : %s  ->  %s" % (rec['clipStart'],
                                                 rec['clipStop']))
        if rec.get('analysisSize'):
            L.append("Analysis size : %s px" % rec['analysisSize'])
        L.append("Objects       : %d" % rec['objectCount'])
        L.append("")

        if not rec['objects']:
            L.append("No detection records stored for this clip.")
            return "\n".join(L)

        for obj in rec['objects']:
            L.append("-" * 72)
            L.append("uid %-6s %-8s  %s  ->  %s   (%s s)"
                     % (obj['uid'], obj['type'], obj['timeStart'],
                        obj['timeStop'],
                        obj['durationSecs'] if obj['durationSecs'] is not None
                        else "-"))
            L.append("   in clip   : %s -> %s"
                     % (obj['clipOffsetStart'], obj['clipOffsetStop']))

            a = obj.get('attributes')
            if a:
                L.append("   attributes:")
                for key in sorted(a.keys()):
                    val = a[key]
                    L.append("      %-13s %s" %
                             (key, "-" if val is None else val))
            else:
                L.append("   attributes: none stored "
                         "(unclassified motion object)")

            t = obj.get('track')
            if t:
                L.append("   track     : %d frames, %s -> %s"
                         % (t['frames'], _fmtMs(t['firstMs']),
                            _fmtMs(t['lastMs'])))
                L.append("      entered at %s, exited at %s"
                         % (t['enteredAt'], t['exitedAt']))
                L.append("      first box  %s" % (t['firstBox'],))
                L.append("      last box   %s" % (t['lastBox'],))
                L.append("      envelope   %s" % (t['envelope'],))
            else:
                L.append("   track     : no motion rows")

            s = obj.get('sizeRange')
            if s:
                L.append("   size      : %dx%d -> %dx%d px"
                         % (s['minWidth'], s['minHeight'],
                            s['maxWidth'], s['maxHeight']))
            L.append("")

        return "\n".join(L)

    # ── Buttons ───────────────────────────────────────────────────────────

    def OnCopy(self, event):
        if not wx.TheClipboard.Open():
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(self._text.GetValue()))
        finally:
            wx.TheClipboard.Close()

    def OnExport(self, event):
        stamp = "unknown"
        if self._clipStartMs:
            stamp = datetime.datetime.fromtimestamp(
                self._clipStartMs / 1000.0).strftime("%Y-%m-%d-%H%M%S")
        cam = (self._camLoc or "clip").replace(" ", "_")
        default = "detections-%s-%s.json" % (cam, stamp)

        dlg = wx.FileDialog(self, "Export detection record",
                            wildcard="JSON files (*.json)|*.json",
                            defaultFile=default,
                            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        finally:
            dlg.Destroy()

        if not path.lower().endswith(".json"):
            path += ".json"

        try:
            with open(path, "w") as f:
                json.dump(self._record, f, indent=2, default=str)
        except Exception as e:
            self._logger.error("JSON export failed", exc_info=True)
            wx.MessageBox("Could not write the file:\n\n%s" % e,
                          "Export failed", wx.OK | wx.ICON_ERROR, self)
            return

        wx.MessageBox("Wrote %d object record%s to\n\n%s"
                      % (self._record['objectCount'],
                         "" if self._record['objectCount'] == 1 else "s",
                         os.path.basename(path)),
                      "Export complete", wx.OK | wx.ICON_INFORMATION, self)
