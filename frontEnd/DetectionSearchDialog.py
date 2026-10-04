#!/usr/bin/env python

#*****************************************************************************
#
# DetectionSearchDialog.py
#
#*****************************************************************************

import wx

from appCommon.CommonStrings import kFrontEndLogName
from vitaToolbox.loggingUtils.LoggingUtils import getLogger


# --- Column display metadata -------------------------------------------------

# Human-readable label for known columns; unknown columns fall back to col name
_kColLabels = {
    'faceName':    'Face Name',
    'faceConf':    'Face ID Confidence',
    'faceDetConf': 'Face Detection Score',
    'gender':      'Gender',
    'age':         'Age',
    'subType':     'Object Subtype',
    'detConf':     'Detection Confidence',
    'nudity':      'Nudity',
    'nudityDetail':'Nudity Class',
}

# INTEGER columns that act as boolean (no free-text value)
_kBoolCols = {'nudity'}

# --- Operator tables  (display_label, op_code) -------------------------------

_kTextOps = [
    ('contains',          'contains'),
    ('is exactly',        '='),
    ('is not',            '!='),
    ('starts with',       'starts_with'),
    ('does not contain',  'not_contains'),
]

_kIntOps = [
    ('=',  '='),   ('is not', '!='),
    ('greater than',  '>'),   ('at least', '>='),
    ('less than',     '<'),   ('at most',  '<='),
]

# REAL columns — user enters 0–100 %; stored as 0.0–1.0 in the DB
_kPctOps = [
    ('greater than (%)',  '>'),  ('at least (%)', '>='),
    ('less than (%)',     '<'),  ('at most (%)',  '<='),
    ('equals (%)',        '='),
]

_kBoolOps = [
    ('detected',     'is_true'),
    ('not detected', 'is_false'),
]

# Connectors joining one condition to the previous one.
_kConnectors = ['AND', 'OR', 'EXCEPT']


# =============================================================================
class _FilterRow:
    """One condition row:

        [conn v] [Field v] [Operator v] [Value ...............] [Remove]

    The leading connector dropdown (AND/OR/EXCEPT) is hidden on the first row.
    """

    def __init__(self, parent, schema, on_delete_cb, on_layout_cb):
        self._schema      = schema
        self._on_delete   = on_delete_cb
        self._on_layout   = on_layout_cb
        self._col_names   = []
        self._current_ops = []

        self.panel = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Connector (AND/OR/EXCEPT) — hidden for the first row.
        self._conn_choice = wx.Choice(self.panel, size=(85, -1),
                                      choices=_kConnectors)
        self._conn_choice.SetSelection(0)

        self._col_choice = wx.Choice(self.panel, size=(150, -1))
        for col_name, _ in schema:
            self._col_names.append(col_name)
            self._col_choice.Append(_kColLabels.get(col_name, col_name))
        if self._col_names:
            self._col_choice.SetSelection(0)

        self._op_choice = wx.Choice(self.panel, size=(140, -1))

        self._val_ctrl = wx.TextCtrl(self.panel, size=(220, -1),
                                     style=wx.TE_PROCESS_ENTER)

        del_btn = wx.Button(self.panel, label="Remove", size=(70, -1))

        sizer.Add(self._conn_choice, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 4)
        sizer.Add(self._col_choice,  0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 4)
        sizer.Add(self._op_choice,   0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 4)
        sizer.Add(self._val_ctrl,    1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 4)
        sizer.Add(del_btn,           0, wx.ALIGN_CENTER_VERTICAL)
        self.panel.SetSizer(sizer)

        self._refresh_ops()

        self._col_choice.Bind(wx.EVT_CHOICE, self._on_col_change)
        del_btn.Bind(wx.EVT_BUTTON, lambda e: self._on_delete(self))

    # ------------------------------------------------------------------

    def show_connector(self, show):
        """Show/hide the leading AND/OR/EXCEPT dropdown (hidden on row 1)."""
        self._conn_choice.Show(show)
        self.panel.Layout()

    def _col_type(self, col_name):
        for name, ctype in self._schema:
            if name == col_name:
                return ctype.upper()
        return 'TEXT'

    def current_col(self):
        sel = self._col_choice.GetSelection()
        if 0 <= sel < len(self._col_names):
            return self._col_names[sel]
        return ''

    def _refresh_ops(self):
        col_name = self.current_col()
        col_type = self._col_type(col_name)

        if col_name in _kBoolCols:
            ops = _kBoolOps
        elif col_type == 'REAL':
            ops = _kPctOps
        elif col_type in ('INTEGER', 'INT'):
            ops = _kIntOps
        else:
            ops = _kTextOps

        self._current_ops = ops
        self._op_choice.Clear()
        for label, _ in ops:
            self._op_choice.Append(label)
        if ops:
            self._op_choice.SetSelection(0)

        self._val_ctrl.Show(col_name not in _kBoolCols)
        self.panel.Layout()

    def _on_col_change(self, event):
        self._refresh_ops()
        self._on_layout()

    # ------------------------------------------------------------------

    def set_from_filter(self, f):
        """Pre-populate this row from a saved filter dict."""
        conn = f.get('conn', 'AND')
        if conn in _kConnectors:
            self._conn_choice.SetSelection(_kConnectors.index(conn))

        col = f.get('col')
        if col in self._col_names:
            self._col_choice.SetSelection(self._col_names.index(col))
        self._refresh_ops()

        op = f.get('op')
        for i, (_, code) in enumerate(self._current_ops):
            if code == op:
                self._op_choice.SetSelection(i)
                break

        val = f.get('val')
        if op not in ('is_true', 'is_false') and val is not None:
            col_type = self._col_type(col)
            if col_type == 'REAL':
                try:
                    self._val_ctrl.SetValue(str(int(round(float(val) * 100))))
                except (ValueError, TypeError):
                    self._val_ctrl.SetValue(str(val))
            else:
                self._val_ctrl.SetValue(str(val))

    def get_filter(self, include_conn):
        """Return a filter dict, or None if incomplete/invalid.

        @param  include_conn  If True, include the 'conn' key (used for every
                              row except the first).
        """
        col_name = self.current_col()
        if not col_name:
            return None

        sel_op = self._op_choice.GetSelection()
        if sel_op < 0 or sel_op >= len(self._current_ops):
            return None
        _, op_code = self._current_ops[sel_op]

        if op_code in ('is_true', 'is_false'):
            out = {'col': col_name, 'op': op_code, 'val': None}
        else:
            val_str = self._val_ctrl.GetValue().strip()
            if not val_str:
                return None
            col_type = self._col_type(col_name)
            if col_type == 'REAL':
                try:
                    val = float(val_str) / 100.0
                except ValueError:
                    return None
            elif col_type in ('INTEGER', 'INT'):
                try:
                    val = int(val_str)
                except ValueError:
                    return None
            else:
                val = val_str
            out = {'col': col_name, 'op': op_code, 'val': val}

        if include_conn:
            sel = self._conn_choice.GetSelection()
            out['conn'] = _kConnectors[sel] if sel >= 0 else 'AND'
        return out


# =============================================================================
class DetectionSearchDialog(wx.Dialog):
    """Modal visual builder for a detection-attribute search.

    After ShowModal() returns wx.ID_OK, read results via:
        getFilters()         -> list of filter dicts (2nd+ carry a 'conn' key)
        getSaveName()        -> str or None (name to save under)
        wasDeleteRequested() -> bool (edit mode only)
    """

    def __init__(self, parent, dataMgr, prefill=None, editName=None,
                 existingNames=None):
        title = "Edit Detection Search" if editName else "Custom Detection Search"
        super(DetectionSearchDialog, self).__init__(
            parent, title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )

        self._logger        = getLogger(kFrontEndLogName)
        self._dataMgr       = dataMgr
        self._editName      = editName
        self._existingNames = set(existingNames or [])
        self._filter_rows   = []

        # Results
        self._resultFilters   = []
        self._resultSaveName  = None
        self._deleteRequested = False

        # Discover schema (auto-includes future columns)
        try:
            self._schema = dataMgr.getAttributeSchema()
        except Exception:
            self._schema = []
        if not self._schema:
            self._schema = [
                ('faceName',    'TEXT'),
                ('faceConf',    'REAL'),
                ('faceDetConf', 'REAL'),
                ('gender',      'TEXT'),
                ('age',         'INTEGER'),
                ('subType',     'TEXT'),
                ('detConf',     'REAL'),
                ('nudity',      'INTEGER'),
                ('nudityDetail','TEXT'),
            ]

        self._build_ui()

        if prefill:
            if prefill.get('name'):
                self._name_ctrl.SetValue(prefill['name'])
            for f in prefill.get('filters', []):
                self._add_filter_row(f)
        if not self._filter_rows:
            self._add_filter_row()

        self._refresh_connectors()
        self._fit()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label="Find recorded objects by their detected attributes.\n"
                  "Add conditions and choose how each one joins the previous "
                  "(AND / OR / EXCEPT)."
        )
        outer.Add(intro, 0, wx.ALL, 10)

        # Filter-row container (a plain panel; the dialog grows to fit).
        self._rows_panel = wx.Panel(self)
        self._rows_sizer = wx.BoxSizer(wx.VERTICAL)
        self._rows_panel.SetSizer(self._rows_sizer)
        outer.Add(self._rows_panel, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # + Add condition
        add_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._add_btn = wx.Button(self, label="+ Add Condition")
        add_sizer.Add(self._add_btn, 0)
        outer.Add(add_sizer, 0, wx.ALL, 10)

        outer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Save name row
        save_sizer = wx.BoxSizer(wx.HORIZONTAL)
        save_sizer.Add(wx.StaticText(self, label="Save as:"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._name_ctrl = wx.TextCtrl(self, size=(240, -1))
        self._name_ctrl.SetHint("(optional name for the Look For list)")
        save_sizer.Add(self._name_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(save_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # Button row
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        if self._editName:
            self._delete_btn = wx.Button(self, label="Delete")
            btn_sizer.Add(self._delete_btn, 0, wx.RIGHT, 6)
            self._delete_btn.Bind(wx.EVT_BUTTON, self._on_delete_search)
        btn_sizer.AddStretchSpacer()
        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        save_label = "Update && Search" if self._editName else "Save && Search"
        self._save_btn   = wx.Button(self, label=save_label)
        self._search_btn = wx.Button(self, wx.ID_OK, "Search")
        self._search_btn.SetDefault()
        btn_sizer.Add(self._cancel_btn, 0, wx.RIGHT, 6)
        btn_sizer.Add(self._save_btn,   0, wx.RIGHT, 6)
        btn_sizer.Add(self._search_btn, 0)
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizer(outer)

        self._add_btn.Bind(wx.EVT_BUTTON, lambda e: self._add_filter_row())
        self._save_btn.Bind(wx.EVT_BUTTON, self._on_save_and_search)
        self._search_btn.Bind(wx.EVT_BUTTON, self._on_search)

    # ------------------------------------------------------------------ rows

    def _add_filter_row(self, prefill=None):
        row = _FilterRow(self._rows_panel, self._schema,
                         self._remove_filter_row, self._fit)
        if prefill:
            row.set_from_filter(prefill)
        self._filter_rows.append(row)
        self._rows_sizer.Add(row.panel, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)
        self._refresh_connectors()
        self._fit()

    def _remove_filter_row(self, row):
        try:
            idx = self._filter_rows.index(row)
        except ValueError:
            return
        self._filter_rows.pop(idx)
        self._rows_sizer.Detach(row.panel)
        row.panel.Destroy()
        if not self._filter_rows:
            self._add_filter_row()
        self._refresh_connectors()
        self._fit()

    def _refresh_connectors(self):
        """Hide the connector on the first row, show it on all others."""
        for i, row in enumerate(self._filter_rows):
            row.show_connector(i > 0)

    def _fit(self):
        """Resize the dialog to fit its contents (keeping a sensible width)."""
        self._rows_panel.Layout()
        self.GetSizer().Layout()
        self.Fit()
        w, h = self.GetSize()
        if w < 680:
            self.SetSize((680, h))
        self.SetMinSize((680, self.GetSize()[1]))

    # ------------------------------------------------------------------ collect

    def _collect_filters(self):
        filters = []
        for i, row in enumerate(self._filter_rows):
            f = row.get_filter(include_conn=(i > 0))
            if f is not None:
                filters.append(f)
        return filters

    def _validate(self):
        filters = self._collect_filters()
        if not filters:
            wx.MessageBox(
                "Add at least one complete condition (a value is required "
                "for non-checkbox fields).",
                "Detection Search", wx.OK | wx.ICON_INFORMATION, self
            )
            return None
        return filters

    # ------------------------------------------------------------------ handlers

    def _on_search(self, event):
        filters = self._validate()
        if filters is None:
            return
        self._resultFilters  = filters
        self._resultSaveName = None
        self.EndModal(wx.ID_OK)

    def _on_save_and_search(self, event):
        filters = self._validate()
        if filters is None:
            return

        name = self._name_ctrl.GetValue().strip()
        if not name:
            wx.MessageBox(
                "Enter a name in the \"Save as\" box to save this search.",
                "Detection Search", wx.OK | wx.ICON_INFORMATION, self
            )
            self._name_ctrl.SetFocus()
            return

        clash = name in self._existingNames and name != (self._editName or '')
        if clash:
            if wx.MessageBox(
                "A search named \"%s\" already exists. Replace it?" % name,
                "Detection Search",
                wx.YES_NO | wx.ICON_QUESTION, self
            ) != wx.YES:
                return

        self._resultFilters  = filters
        self._resultSaveName = name
        self.EndModal(wx.ID_OK)

    def _on_delete_search(self, event):
        if wx.MessageBox(
            "Delete the saved search \"%s\"?" % self._editName,
            "Detection Search",
            wx.YES_NO | wx.ICON_QUESTION, self
        ) != wx.YES:
            return
        self._deleteRequested = True
        self.EndModal(wx.ID_OK)

    # ------------------------------------------------------------------ results

    def getFilters(self):
        return self._resultFilters

    def getSaveName(self):
        return self._resultSaveName

    def wasDeleteRequested(self):
        return self._deleteRequested
