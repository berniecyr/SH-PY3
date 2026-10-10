#!/usr/bin/env python

#*****************************************************************************
#
# ImageRecordDialog.py
#     Every stored field for one of the user's files, read-only.
#
#*****************************************************************************

"""
## @file
The Image view's "View record" dialog.

The detail pane shows a curated summary; this is the database record behind
it, one field per row, grouped by table: the file row, each location the same
content was found at, and each stored detection.  Nothing here writes.
"""

import datetime

import wx

from vitaToolbox.wx.FontUtils import makeFontBold


# Epoch timestamps below these are not dates (atMs and durationMs are offsets
# and lengths), so only larger values get a readable date beside them.
_kMinEpochMs = 100000000000      # 1973 in milliseconds
_kMinEpochSecs = 100000000       # 1973 in seconds

_kColGray = (110, 110, 110)


##############################################################################
def formatValue(field, value):
    """Render one stored value for display.

    @param  field  Column name.
    @param  value  The stored value.
    @return str    Display text; never empty, so a blank looks deliberate.
    """
    if value is None:
        return "(empty)"
    if isinstance(value, bytes):
        return "<%d bytes of binary data>" % len(value)
    text = str(value)
    if text == "":
        return "(blank)"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = None
        if field.endswith("Ms") and value >= _kMinEpochMs:
            stamp = value / 1000.0
        elif field == "mtime" and value >= _kMinEpochSecs:
            stamp = value
        elif field == "mtimeNs" and value >= _kMinEpochSecs * 10 ** 9:
            stamp = value / 1e9
        if stamp is not None:
            try:
                text += "   (%s)" % datetime.datetime.fromtimestamp(
                    stamp).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                pass
    return text


##############################################################################
class ImageRecordDialog(wx.Dialog):
    """Shows every field and value stored for one file."""

    ###########################################################
    def __init__(self, parent, path, sections):
        """Initializer for ImageRecordDialog.

        @param  parent    The parent window.
        @param  path      Absolute path of the file, for the heading.
        @param  sections  [(title, [(field, value), ...]), ...] as returned
                          by UserMediaDb.getRecord; empty when the file has
                          no stored record.
        """
        super(ImageRecordDialog, self).__init__(
            parent, title="Record - %s" % path.replace("\\", "/").split("/")[-1],
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self._path = path
        self._sections = sections or []
        # Full text of each list row, by row index; None for section rows.
        self._rowValues = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(self, -1, path)
        makeFontBold(heading)
        sizer.Add(heading, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._list = wx.ListCtrl(
            self, -1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self._list.svKeepOwnBackground = True
        self._list.InsertColumn(0, "Field", width=180)
        self._list.InsertColumn(1, "Value", width=520)
        self._list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._onRowSelected)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        # A list row is one line; long or multi-line values (the AI
        # description, a nudity breakdown) are shown whole here.
        valueLabel = wx.StaticText(self, -1, "Selected value")
        valueLabel.SetForegroundColour(_kColGray)
        sizer.Add(valueLabel, 0, wx.LEFT | wx.RIGHT, 8)
        self._valueText = wx.TextCtrl(
            self, -1, "", size=(-1, 70),
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._valueText.svKeepOwnBackground = True
        sizer.Add(self._valueText, 0, wx.EXPAND | wx.ALL, 8)

        btnSizer = wx.BoxSizer(wx.HORIZONTAL)
        copyBtn = wx.Button(self, -1, "Copy all")
        copyBtn.Bind(wx.EVT_BUTTON, self._onCopy)
        closeBtn = wx.Button(self, wx.ID_CANCEL, "Close")
        btnSizer.Add(copyBtn, 0)
        btnSizer.AddStretchSpacer(1)
        btnSizer.Add(closeBtn, 0)
        sizer.Add(btnSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._populate()

        self.SetSizer(sizer)
        self.SetSize((760, 600))
        self.SetMinSize((480, 360))
        closeBtn.SetDefault()
        self.CenterOnParent()


    ###########################################################
    def _populate(self):
        """Fill the list: a bold row per section, then its fields."""
        if not self._sections:
            self._list.InsertItem(0, "(no record)")
            self._list.SetItem(0, 1, "This file is not in the database yet. "
                                     "Analyze it or add its folder first.")
            self._rowValues.append(None)
            return

        boldFont = self._list.GetFont().Bold()
        sectionBg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
        for title, fields in self._sections:
            row = self._list.InsertItem(self._list.GetItemCount(), title)
            self._list.SetItemFont(row, boldFont)
            self._list.SetItemBackgroundColour(row, sectionBg)
            self._rowValues.append(None)
            for field, value in fields:
                text = formatValue(field, value)
                row = self._list.InsertItem(self._list.GetItemCount(),
                                            "    " + field)
                self._list.SetItem(row, 1, text.replace("\r", " ")
                                               .replace("\n", " ")[:300])
                if value is None:
                    self._list.SetItemTextColour(row, wx.Colour(*_kColGray))
                self._rowValues.append(text)


    ###########################################################
    def _onRowSelected(self, event):
        """Show the selected row's value in full."""
        index = event.GetIndex()
        value = self._rowValues[index] if 0 <= index < len(self._rowValues) \
            else None
        self._valueText.ChangeValue(value or "")
        event.Skip()


    ###########################################################
    def asText(self):
        """The whole record as plain text, one "field: value" per line."""
        lines = [self._path]
        for title, fields in self._sections:
            lines.append("")
            lines.append("[%s]" % title)
            for field, value in fields:
                lines.append("%s: %s" % (field, formatValue(field, value)))
        return "\n".join(lines)


    ###########################################################
    def _onCopy(self, event):
        """Copy the record to the clipboard."""
        if not wx.TheClipboard.Open():
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(self.asText()))
        finally:
            wx.TheClipboard.Close()
