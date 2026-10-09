"""Advanced media query builder. UI state is saved separately from media data."""
import copy
from datetime import datetime, timedelta
import wx
from wx.lib.scrolledpanel import ScrolledPanel
from frontEnd.FrontEndPrefs import getFrontEndPref, setFrontEndPref

TEXT_OPS = ['Contains', 'Whole word', 'Exactly equals', 'Is blank', 'Is not blank']
NUMBER_OPS = ['Equals number', 'At least', 'At most', 'Greater than', 'Less than', 'Between']
DATE_OPS = ['On date', 'On or after', 'On or before', 'After', 'Before', 'Between dates']
DATE_UNITS = {'mtime': 1, 'mtimeNs': 1000000000, 'captureMs': 1000, 'analyzedMs': 1000}
# Stored as YYYYMMDD / HHMMSS integers (EXIF date taken); see UserMediaDb.
DAY_FIELDS = {'exifDate'}
TIME_FIELDS = {'exifTime'}
TIME_OPS = ['At time', 'At or after time', 'At or before time', 'Between times']
# Yes/no fields stored as 1, 0, or NULL when never set; add a field name here
# to give it these choices.  NotSet is NULL, which eq:"" would never match.
FLAG_FIELDS = {'faceModelRan', 'nudityModelRan'}
FLAG_OPS = {'True': lambda field: field + ':eq:"1"',
            'False': lambda field: field + ':eq:"0"',
            'NotSet': lambda field: 'empty:' + field}
NO_VALUE_OPS = ('Is blank', 'Is not blank') + tuple(FLAG_OPS)
MODES = {'Contains': '', 'Whole word': 'word', 'Exactly equals': 'exact',
         'Has exact tag': 'tag', 'Equals number': 'eq', 'At least': 'ge',
         'At most': 'le', 'Greater than': 'gt', 'Less than': 'lt', 'Between': 'between'}
MODES.update(dict(zip(DATE_OPS, ['between', 'ge', 'le', 'gt', 'lt', 'between'])))


def quote(value):
    if value.endswith('\\'):
        raise ValueError('Omit a trailing backslash from the search value.')
    return '"' + value.replace('"', '\\"') + '"'


def dateValue(value, field, end=False):
    if field.split('.')[-1] in DAY_FIELDS:
        try:
            if len(value) != 10:
                raise ValueError()
            return datetime.strptime(value, '%Y-%m-%d').strftime('%Y%m%d')
        except ValueError:
            raise ValueError('Enter a date as YYYY-MM-DD.')
    unit = DATE_UNITS.get(field.split('.')[-1])
    if not unit:
        return value
    try:
        stamp = datetime.fromisoformat(value)
        isDate = len(value) == 10
        if isDate and end:
            stamp += timedelta(days=1)
        number = int(stamp.timestamp() * unit)
        if isDate and end:
            number -= 1
        return str(number)
    except (ValueError, OverflowError, OSError):
        raise ValueError('Enter a date as YYYY-MM-DD or a date/time as YYYY-MM-DD HH:MM:SS.')


def timeValue(value, end=False):
    """HH:MM or HH:MM:SS as an HHMMSS integer; a minute-only end takes the whole minute."""
    try:
        parts = [int(p) for p in value.strip().split(':')]
        if len(parts) not in (2, 3) or not (0 <= parts[0] < 24 and 0 <= parts[1] < 60):
            raise ValueError()
        seconds = parts[2] if len(parts) == 3 else (59 if end else 0)
        if not 0 <= seconds < 60:
            raise ValueError()
        return parts[0] * 10000 + parts[1] * 100 + seconds
    except ValueError:
        raise ValueError('Enter a time as HH:MM or HH:MM:SS (24-hour).')


def timeTerm(field, op, value, end):
    """A time-of-day condition; a range whose start is after its end spans midnight."""
    # Always six digits, so 06:00 reads as 060000 and never as 60000.
    if op == 'At or after time':
        return '%s:ge:"%06d"' % (field, timeValue(value))
    if op == 'At or before time':
        return '%s:le:"%06d"' % (field, timeValue(value, end=True))
    if op == 'At time':
        end = value
    if not end:
        raise ValueError('Enter a value for each condition, or remove the empty condition.')
    # One between condition even overnight; the search treats a start later
    # than the end as crossing midnight.
    return '%s:between:"%06d,%06d"' % (field, timeValue(value), timeValue(end, end=True))


def buildQuery(state):
    terms = []
    for rule in state.get('rules', []):
        field, op = rule['field'], rule['op']
        value = rule.get('value', '')
        if op in FLAG_OPS:
            term = FLAG_OPS[op](field)
        elif op in ('Is blank', 'Is not blank'):
            if field == 'all':
                raise ValueError('Choose a specific field for blank/not blank.')
            term = ('empty:' if op == 'Is blank' else 'has:') + field
        else:
            if not value:
                raise ValueError('Enter a value for each condition, or remove the empty condition.')
            if op in TIME_OPS:
                term = timeTerm(field, op, value, rule.get('end', ''))
                terms.append(('NOT (' + term + ')') if rule.get('exclude') else term)
                continue
            mode = MODES[op]
            if op in NUMBER_OPS + DATE_OPS:
                value = dateValue(value, field, end=op in ('At most', 'Greater than', 'On or before', 'After'))
            if op in ('Between', 'Between dates', 'On date'):
                upper = rule['value'] if op == 'On date' else rule.get('end', '')
                value += ',' + dateValue(upper, field, end=True)
            term = field + ':' + (mode + ':' if mode else '') + quote(value)
            if op == 'Contains':
                term = field + ' CONTAINS ' + quote(value)
        terms.append(('NOT (' + term + ')') if rule.get('exclude') else term)
    joined = (' AND ' if state.get('join', 'All') == 'All' else ' OR ').join(terms)
    base = state.get('base', '').strip()
    return ('(' + base + ') AND (' + joined + ')') if base and joined else base or joined


class AdvancedMediaSearchDialog(wx.Dialog):
    def __init__(self, parent, fields, validate, state=None, selectionPaths=()):
        super().__init__(parent, title='Advanced search', size=(1000, 780),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((850, 650))
        self._fields = ['all', 'filename'] + [r['field'] for r in fields]
        self._types = {r['field']: r['type'] for r in fields}
        self._validate = validate
        self._rows = []
        self._saved = copy.deepcopy(getFrontEndPref('advancedMediaSearches') or {})
        from frontEnd.MediaSelections import uniquePaths
        self.selectionPaths = uniquePaths(selectionPaths)
        self._selections = copy.deepcopy(getFrontEndPref('imageMediaSelections') or {})
        self.loadedSelection = None
        root = wx.BoxSizer(wx.VERTICAL)
        self.scope = wx.RadioBox(self, label='Apply search to',
            choices=['New search', 'Current selection (%d displayed files)' % len(self.selectionPaths)],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS)
        self.scope.SetSelection(0)
        root.Add(self.scope, 0, wx.EXPAND | wx.ALL, 10)
        selectionRow = wx.BoxSizer(wx.HORIZONTAL)
        selectionRow.Add(wx.StaticText(self, label='Saved selections:'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.selections = wx.Choice(self)
        selectionRow.Add(self.selections, 1)
        for label, handler in [('Load selection', self._loadSelection),
                               ('Save current selection...', self._saveSelection),
                               ('Delete', self._deleteSelection)]:
            button = wx.Button(self, label=label); button.Bind(wx.EVT_BUTTON, handler)
            selectionRow.Add(button, 0, wx.LEFT, 6)
        root.Add(selectionRow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self._updateSelections()
        root.Add(wx.StaticText(self, label='Selections are snapshots of displayed files. Load restores the list and closes this dialog;\n'
            'reopen Advanced search and choose Current selection to narrow it.'), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        savedRow = wx.BoxSizer(wx.HORIZONTAL)
        savedRow.Add(wx.StaticText(self, label='Saved searches:'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.saved = wx.Choice(self)
        savedRow.Add(self.saved, 1)
        for label, handler in [('Load', self._load), ('Save as...', self._save), ('Delete', self._delete)]:
            button = wx.Button(self, label=label); button.Bind(wx.EVT_BUTTON, handler)
            savedRow.Add(button, 0, wx.LEFT, 6)
        root.Add(savedRow, 0, wx.EXPAND | wx.ALL, 10)
        root.Add(wx.StaticText(self, label='Existing search expression (optional; combined with the conditions below using AND):'), 0, wx.LEFT, 10)
        self.base = wx.TextCtrl(self)
        root.Add(self.base, 0, wx.EXPAND | wx.ALL, 10)
        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(wx.StaticText(self, label='Match'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.join = wx.Choice(self, choices=['All', 'Any']); self.join.SetSelection(0)
        options.Add(self.join)
        options.Add(wx.StaticText(self, label='of these conditions. Exclude means NOT.'), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        add = wx.Button(self, label='Add condition'); add.Bind(wx.EVT_BUTTON, lambda e: self._add())
        options.AddStretchSpacer(); options.Add(add)
        root.Add(options, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.rows = ScrolledPanel(self)
        self.rowSizer = wx.BoxSizer(wx.VERTICAL); self.rows.SetSizer(self.rowSizer)
        self.rows.SetupScrolling(scroll_x=False)
        root.Add(self.rows, 1, wx.EXPAND | wx.ALL, 10)
        note = wx.StaticText(self, label='Dates: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS (local time); date-only range ends include the full day.\n'
            'exifDate and exifTime are a photo\'s EXIF date taken. Times are HH:MM (24-hour); 19:00 to 06:00 spans midnight.\n'
            'Size is bytes; durationMs and atMs are milliseconds; confidence is 0–1.\n'
            'All fields includes all three database tables. Separate conditions may match different detection rows.\n'
            'Between applies both bounds to the same value. New search uses folder scope and Show only filters.\n'
            'Current selection uses only the displayed snapshot, across its folders, plus Show only filters.')
        root.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.preview = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        root.Add(self.preview, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.error = wx.StaticText(self, label=''); self.error.SetForegroundColour(wx.RED)
        root.Add(self.error, 0, wx.EXPAND | wx.ALL, 10)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindow(wx.ID_OK).SetLabel('Search')
        root.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        self.SetSizer(root)
        self.Bind(wx.EVT_BUTTON, self._search, id=wx.ID_OK)
        self.base.Bind(wx.EVT_TEXT, self._refresh)
        self.join.Bind(wx.EVT_CHOICE, self._refresh)
        self._updateSaved()
        self.setState(state or {'base': '', 'rules': [], 'join': 'All'})

    def _updateSelections(self):
        self.selections.Set(sorted(self._selections, key=str.casefold))
        if self.selections.GetCount(): self.selections.SetSelection(0)

    def _saveSelection(self, event):
        dialog = wx.TextEntryDialog(self, 'Name for the %d displayed files:' % len(self.selectionPaths), 'Save selection')
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.GetValue().strip(): return
            name = dialog.GetValue().strip()
        finally: dialog.Destroy()
        if name in self._selections and wx.MessageBox('Replace selection "' + name + '"?',
                'Replace selection', wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES: return
        self._selections[name] = list(self.selectionPaths)
        setFrontEndPref('imageMediaSelections', copy.deepcopy(self._selections))
        self._updateSelections(); self.selections.SetStringSelection(name)

    def _loadSelection(self, event):
        name = self.selections.GetStringSelection()
        if name in self._selections:
            self.loadedSelection = (name, list(self._selections[name]))
            self.EndModal(wx.ID_APPLY)

    def _deleteSelection(self, event):
        name = self.selections.GetStringSelection()
        if name in self._selections and wx.MessageBox('Delete saved selection "' + name + '"?\nFiles will remain on disk.',
                'Delete selection', wx.YES_NO | wx.NO_DEFAULT, self) == wx.YES:
            del self._selections[name]
            setFrontEndPref('imageMediaSelections', copy.deepcopy(self._selections)); self._updateSelections()

    def _updateSaved(self):
        self.saved.Set(sorted(self._saved, key=str.casefold))
        if self.saved.GetCount(): self.saved.SetSelection(0)

    def _add(self, rule=None):
        rule = rule or {}
        panel = wx.Panel(self.rows); sizer = wx.BoxSizer(wx.HORIZONTAL)
        field = wx.Choice(panel, choices=['All fields', 'Filename (any copy)'] + self._fields[2:])
        field.SetSelection(self._fields.index(rule.get('field', 'all')) if rule.get('field', 'all') in self._fields else 0)
        op = wx.Choice(panel)
        value = wx.TextCtrl(panel, value=rule.get('value', ''))
        end = wx.TextCtrl(panel, value=rule.get('end', ''))
        exclude = wx.CheckBox(panel, label='Exclude'); exclude.SetValue(rule.get('exclude', False))
        remove = wx.Button(panel, label='Remove')
        for ctrl, proportion in [(field, 2), (op, 1), (value, 2), (end, 2), (exclude, 0), (remove, 0)]:
            sizer.Add(ctrl, proportion, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        panel.SetSizer(sizer); self.rowSizer.Add(panel, 0, wx.EXPAND | wx.BOTTOM, 8)
        row = (panel, field, op, value, end, exclude); self._rows.append(row)
        def configure(event=None):
            key = self._fields[field.GetSelection()]
            choices = list(TEXT_OPS)
            if key == 'all': choices = choices[:3]
            name = key.split('.')[-1]
            if name in FLAG_FIELDS:
                choices = list(FLAG_OPS)
            elif self._types.get(key) in ('INTEGER', 'REAL', 'NUMERIC'):
                if name in TIME_FIELDS:
                    choices += TIME_OPS
                else:
                    choices += DATE_OPS if name in DATE_UNITS or name in DAY_FIELDS else NUMBER_OPS
            if key == 'files.description_tags': choices.append('Has exact tag')
            old = op.GetStringSelection(); op.Set(choices)
            op.SetStringSelection(old if old in choices else choices[0])
            value.SetHint('YYYY-MM-DD' if name in DATE_UNITS or name in DAY_FIELDS
                          else 'HH:MM' if name in TIME_FIELDS else 'Value')
            end.SetHint('Range end')
            self._refresh()
        field.Bind(wx.EVT_CHOICE, configure)
        for ctrl, event in [(op, wx.EVT_CHOICE), (value, wx.EVT_TEXT), (end, wx.EVT_TEXT), (exclude, wx.EVT_CHECKBOX)]:
            ctrl.Bind(event, self._refresh)
        def delete(event):
            self._rows.remove(row); self.rowSizer.Detach(panel); panel.Destroy(); self._refresh()
        remove.Bind(wx.EVT_BUTTON, delete)
        configure(); op.SetStringSelection(rule.get('op', 'Contains')); self._refresh()

    def getState(self):
        return dict(base=self.base.GetValue(), join=self.join.GetStringSelection(), rules=[
            dict(field=self._fields[f.GetSelection()], op=o.GetStringSelection(), value=v.GetValue(),
                 end=e.GetValue(), exclude=n.GetValue()) for _, f, o, v, e, n in self._rows])

    def setState(self, state):
        for row in self._rows: row[0].Destroy()
        self._rows = []; self.rowSizer.Clear()
        self.base.ChangeValue(state.get('base', '')); self.join.SetStringSelection(state.get('join', 'All'))
        for rule in state.get('rules', []): self._add(rule)
        self._refresh()

    def _refresh(self, event=None):
        for _, _, op, value, end, _ in self._rows:
            value.Enable(op.GetStringSelection() not in NO_VALUE_OPS)
            end.Show(op.GetStringSelection() in ('Between', 'Between dates', 'Between times'))
        self.rows.Layout(); self.rows.FitInside()
        try:
            query = buildQuery(self.getState()); self._validate(query)
            self.preview.ChangeValue(query); self.error.SetLabel('')
        except ValueError as exc:
            self.preview.ChangeValue(''); self.error.SetLabel(str(exc))

    def _search(self, event):
        self._refresh()
        if not self.error.GetLabel(): self.EndModal(wx.ID_OK)

    def _save(self, event):
        self._refresh()
        if self.error.GetLabel(): return
        dialog = wx.TextEntryDialog(self, 'Name for this search:', 'Save search')
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.GetValue().strip(): return
            name = dialog.GetValue().strip()
        finally: dialog.Destroy()
        if name in self._saved and wx.MessageBox('Replace the saved search "' + name + '"?',
                'Replace saved search', wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES: return
        self._saved[name] = self.getState()
        setFrontEndPref('advancedMediaSearches', self._saved); self._updateSaved(); self.saved.SetStringSelection(name)

    def _load(self, event):
        name = self.saved.GetStringSelection()
        if name in self._saved: self.setState(copy.deepcopy(self._saved[name]))

    def _delete(self, event):
        name = self.saved.GetStringSelection()
        if name in self._saved and wx.MessageBox('Delete saved search "' + name + '"?',
                'Delete saved search', wx.YES_NO | wx.NO_DEFAULT, self) == wx.YES:
            del self._saved[name]; setFrontEndPref('advancedMediaSearches', self._saved); self._updateSaved()


def matchingValues(query, values):
    """Positive-term evidence, not a second implementation of record selection."""
    from decimal import Decimal, InvalidOperation
    from backEnd.UserMediaSearch import parse, wholeWordMatch, TIME_FIELDS, _timeBound
    aliases = dict(tags='files.description_tags', ai='files.description_ai',
                   person='detections.faceName', name='filename', folder='file_locations.path',
                   path='file_locations.path', contains='all')
    available = {field.casefold(): field for field, value in values}
    # Preserve the compiler's unqualified precedence: file, detection, location.
    for table in ('file_locations', 'detections', 'files'):
        for field in list(available.values()):
            if field.startswith(table + '.'):
                aliases[field.split('.')[1].casefold()] = field
    aliases['path'] = 'file_locations.path'
    output = []
    def walk(node, negative=False):
        if node is None: return
        mode = node[0]
        if mode == 'not': walk(node[1], not negative); return
        if mode in ('and', 'or'):
            walk(node[1], negative); walk(node[2], negative); return
        if negative: return
        field, term = node[1:]
        if field in ('has', 'empty'): return
        field = aliases.get(field, available.get(field, field))
        for name, value in values:
            if field != 'all' and name != field: continue
            text = str(value) if value is not None else ''
            folded, needle = text.casefold(), term.casefold()
            if mode == 'term': matched = needle in folded
            elif mode == 'word': matched = wholeWordMatch(text, term)
            elif mode == 'exact': matched = folded == needle
            elif mode == 'tag': matched = needle in [t.strip() for t in folded.split(';')]
            else:
                try:
                    number = Decimal(text)
                    isTime = name.split('.')[-1] in TIME_FIELDS
                    if mode == 'between':
                        bounds = term.split(',')
                        lo, hi = (Decimal(_timeBound(b) if isTime else b) for b in bounds)
                        # A time range whose start is after its end spans midnight.
                        matched = (number >= lo or number <= hi) if isTime and lo > hi else lo <= number <= hi
                    else:
                        target = Decimal(_timeBound(term) if isTime else term)
                        matched = {'ge': number >= target, 'gt': number > target,
                                   'le': number <= target, 'lt': number < target, 'eq': number == target}[mode]
                except (InvalidOperation, ValueError): matched = False
            if matched and (name, text) not in output: output.append((name, text))
    walk(parse(query))
    return output


def showMatches(parent, query, values):
    import wx.richtext as rt
    dialog = wx.Dialog(parent, title='Matching field values', size=(800, 560),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    sizer = wx.BoxSizer(wx.VERTICAL)
    text = rt.RichTextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    text.WriteText('Applied search: ' + query + '\n\nPositive matches are highlighted below. '
                   'Blank/not-blank and NOT conditions have no matching text to highlight.\n\n')
    matches = matchingValues(query, values)
    for field, value in matches[:200]:
        text.WriteText(field + '\n')
        highlight = rt.RichTextAttr()
        highlight.SetBackgroundColour(wx.Colour(255, 245, 150))
        text.BeginStyle(highlight)
        text.WriteText(value[:3000] + ('…' if len(value) > 3000 else ''))
        text.EndStyle(); text.WriteText('\n\n')
    if not matches: text.WriteText('No positive text matches (the search may use only exclusions or blank checks).')
    if len(matches) > 200: text.WriteText('Showing the first 200 matching values.')
    text.ShowPosition(0)
    sizer.Add(text, 1, wx.EXPAND | wx.ALL, 10)
    sizer.Add(dialog.CreateStdDialogButtonSizer(wx.OK), 0, wx.ALIGN_RIGHT | wx.ALL, 10)
    dialog.SetSizer(sizer)
    try: dialog.ShowModal()
    finally: dialog.Destroy()


def showHelp(message, title, style, parent):
    """Scroll long help text instead of growing beyond the screen."""
    dialog = wx.Dialog(parent, title=title, size=(800, 600),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    sizer = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dialog, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY)
    sizer.Add(text, 1, wx.EXPAND | wx.ALL, 10)
    sizer.Add(dialog.CreateStdDialogButtonSizer(wx.OK), 0, wx.ALIGN_RIGHT | wx.ALL, 10)
    dialog.SetSizer(sizer)
    try: dialog.ShowModal()
    finally: dialog.Destroy()
