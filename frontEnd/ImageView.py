#! /usr/local/bin/python

#*****************************************************************************
#
# ImageView.py
#     Browse the user's own photos and videos, run them through the same
#     detectors the cameras use.
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

r"""
## @file
The "Image" view: the user's own photos and videos, through our detectors.

Every other view looks at footage WE recorded -- a clip has to come from a
StreamReader, be registered in clipdb and live under videos\archive before
anything can search it.  This view is the exception: it browses the local
filesystem and runs the user's own files through the same models, with the same
settings, that the cameras use.

Two rules define the design and are easy to break by accident:

  1. The detection models are NOT loaded here.  Inference is one RPC to the
     already-running DetectionService (backEnd/DetectionServiceClient.py),
     exactly as backEnd/DetectionReplay.py does from this same process.  No
     second model load, no second CUDA context.

  2. The user's files are not ours.  Nothing this view writes goes into clipdb
     or objdb2, and no user path is ever handed to anything DiskCleaner walks.
     Results live in their own database at <dataDir>\usermedia\usermedia.db.

Three wx conventions this view deliberately does NOT follow.  Each looks like an
oversight until you try the alternative:

  * It does not call bindChildren() for focus stealing.  BaseView.OnFocusChanged
    takes focus back from any child that is not a wx.Choice, which would leave
    the folder tree unable to accept an arrow key and the (later) EXIF fields
    unable to accept a character.  Search/Monitor/Grid opt in; SystemHealthView
    and this view do not.

  * Controls that draw their own background are marked svKeepOwnBackground, so
    AppColors.applyToTree leaves them alone.  applyToTree repairs contrast only
    on wx.StaticText, so a recoloured tree or list ends up dark-on-dark.

  * The three panes are TWO NESTED 2-PANE SPLITTERS, not one 3-pane splitter.
    FixedMultiSplitterWindow is written against sashes[0] throughout: its
    GetAllowableSashPos measures against the whole client width (correct only
    for sash 0), SetSashGravity only ever assigns _sashes[0], and _DrawSash
    draws the grab bitmap only at GetSashPosition(0) -- so a second sash would
    be draggable but invisible, and draggable past where the last pane has a
    positive width.  MonitorView nests two splitters for exactly this reason.
"""

# Python imports...
import os
import queue
import threading
import traceback

# Common 3rd-party imports...
import wx

# Toolbox imports...
from vitaToolbox.wx.BackgroundStyleUtils import kBackgroundStyle
from vitaToolbox.wx.FixedMultiSplitterWindow import FixedMultiSplitterWindow
from vitaToolbox.wx.FontUtils import makeFontBold, makeFontDefault
from vitaToolbox.wx.TranslucentStaticText import TranslucentStaticText

# Local imports...
from frontEnd.BaseView import BaseView
from frontEnd.ImageDetailPanel import ImageDetailPanel
from frontEnd.ImageThumbGrid import ImageThumbGrid, EVT_THUMB_SELECTED, EVT_THUMB_READY
from backEnd import UserMediaAnalysis
from backEnd import UserMediaFolderImport
from backEnd.UserMediaDb import UserMediaDb
from frontEnd.FrontEndPrefs import getFrontEndPref, setFrontEndPref
from appCommon.CommonStrings import kTargetLabels


# Constants...

# Matches the border every other view uses.
_kBorder = 12
_kCtrlPadding = 4

# What we will offer to analyse.  HEIC is deliberately absent: nothing in this
# tree can decode it (Pillow here has no HEIC support and there is no
# pillow-heif), so listing a file we cannot open would only produce a thumbnail
# that never loads and a detection that never runs.
_kImageExts = ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff',
               '.webp')
_kVideoExts = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.mpg', '.mpeg',
               '.wmv', '.webm')
_kMediaExts = _kImageExts + _kVideoExts

# The targets a file can be filtered by.  Taken from the same list the rule
# editor uses so the vocabulary matches the rest of the app.  "Any object" and
# "Unknown objects" are dropped: for a photo library "show me the ones with
# nothing in them" is a different question, answered later by the scan state.
_kFilterLabels = [label for label in kTargetLabels
                  if label not in ("Any object", "Unknown objects")]

# Default pane widths, used only when there is no saved sash position.
# Without these the splitter falls back to each pane's BEST size, and a
# GenericDirCtrl's best size is enormous -- measured 511 px for the tree and
# 59 px for the detail pane, i.e. exactly backwards.
_kDefaultTreeWidth = 240
_kDefaultDetailWidth = 320

# Floors, applied AFTER SplitVertically -- which copies each pane's effective
# min size into its min size, and so would otherwise lock in those best sizes.
_kMinTreeWidth = 160
_kMinTreeHeight = 120
_kMinListWidth = 200
_kMinDetailWidth = 220

# How long the analysis worker waits for a job before re-checking the stop
# flag.  Short enough that quitting is immediate, long enough to sit idle.
_kWorkerPollSecs = 0.25

# Wall-clock pause between files during a folder scan.  This is the real
# throttle, and it is NOT DetectionBusy: that exception comes from our own
# client's cooldown after OUR previous request already hit the 30 s socket
# timeout, so deferring on it means deferring after the damage.  Inference is
# serialised on one lock shared with every camera, so a scan has to leave gaps
# in it deliberately.
_kScanGapSecs = 0.1

# Maps a filter checkbox to what it means in the database.  Built from
# kTargetMapping so the labels can only ever be the ones the rule editor uses.
_kFilterToType = {"People": "person", "Animals": "animal",
                  "Vehicles": "vehicle"}

##############################################################################
def _isMediaFile(name):
    """Is this a file we could analyse?

    @param  name  A file name (not a full path).
    @return bool  True if the extension is one we can decode.
    """
    return name.lower().endswith(_kMediaExts)


##############################################################################
class ImageView(BaseView):
    """Browse local photos and videos and view our detections for them."""

    ###########################################################
    def __init__(self, parent, backEndClient):
        """Initializer for ImageView.

        @param  parent         The parent window.
        @param  backEndClient  A connection to the back end app.
        """
        super(ImageView, self).__init__(parent, backEndClient)

        # Absolute paths of what is currently listed, parallel to the rows of
        # self._fileList.
        self._files = []
        self._currentDir = ""

        # Analysis plumbing.  ONE worker, one file at a time: DetectionService
        # serialises all inference on a single lock, so a second client in
        # flight buys nothing and costs the live cameras latency.
        self._db = None
        self._dbLock = threading.Lock()
        self._workQueue = queue.Queue()
        self._stopEvent = threading.Event()
        self._worker = None
        self._busyPath = None

        # Folder-scan progress.  _scanning is what the Analyze/Stop button
        # reflects and what the worker checks between files.
        self._scanning = False
        self._folderCancel = None
        # All media in the current folder, before the detection filters are
        # applied.  The grid shows a subset of this.
        self._allFiles = []

        # Bound before anything can start: the teardown path is the thing this
        # view will get wrong first once it owns a worker thread, and wiring it
        # now means there is never a revision that lacks it.
        self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)

        self._initUiWidgets()

        topLevelParent = self.GetTopLevelParent()
        if hasattr(topLevelParent, 'registerExitNotification'):
            topLevelParent.registerExitNotification(self._savePrefs)
            topLevelParent.registerPrefsNotification(self._loadPrefs)
            # Thread shutdown goes HERE, not in prepareToClose().  The frame
            # calls prepareToClose() on the CURRENT view only, so quitting
            # from the Monitor tab would leave this thread running into
            # sys.exit.  The exit-notification list fires for every
            # registrant regardless of which view is on screen.
            topLevelParent.registerExitNotification(self.stopWorker)


    ###########################################################
    def _initUiWidgets(self):
        """Build the layout: folder tree | file list | details."""
        splitterStyle = (wx.SP_LIVE_UPDATE | wx.TAB_TRAVERSAL |
                         wx.BORDER_NONE | wx.TRANSPARENT_WINDOW |
                         wx.FULL_REPAINT_ON_RESIZE)

        # -- Outer splitter: folder tree on the left, everything else right --
        self._mainSplitterWindow = FixedMultiSplitterWindow(
            self, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize,
            splitterStyle, "SplitterWindowImageView", None, self._logger
        )
        # Gravity is the share of new space given to the LEFT pane, so 0 --
        # not 1 -- is what keeps the folder tree at the width the user chose
        # and hands a wider window to the content on the right.
        self._mainSplitterWindow.SetSashGravity(0)

        self._leftPanel = self._makePanel(self._mainSplitterWindow)
        self._rightPanel = self._makePanel(self._mainSplitterWindow)

        self._buildLeftPanel()

        # -- Inner splitter: file list | details -------------------------
        self._rightSplitterWindow = FixedMultiSplitterWindow(
            self._rightPanel, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize,
            splitterStyle, "SplitterWindowImageViewRight", None, self._logger
        )
        # Gravity 1 here, because the file list IS the left pane of this
        # inner splitter: extra width should grow the listing, not stretch the
        # detail pane.
        self._rightSplitterWindow.SetSashGravity(1)

        self._listPanel = self._makePanel(self._rightSplitterWindow)
        # Its own control rather than a panel filled in here: it grows a
        # detections list and an EXIF table in later steps, and that does not
        # belong in the view that owns the splitters.
        self._detailPanel = ImageDetailPanel(self._rightSplitterWindow,
                                             self._logger,
                                             self._onAnalyzeRequested,
                                             self._loadDescriptions,
                                             self._saveDescriptions)

        self._buildListPanel()

        self._rightSplitterWindow.SplitVertically(self._listPanel,
                                                  self._detailPanel)
        # SplitVertically has just copied each pane's EFFECTIVE min size into
        # its min size; for a pane holding a dir ctrl or a report list that is
        # its (large) best size, which then becomes a floor the sash can never
        # cross.  Override with real floors.
        self._listPanel.SetMinSize((_kMinListWidth, -1))
        self._detailPanel.SetMinSize((_kMinDetailWidth, -1))
        rightSizer = wx.BoxSizer(wx.VERTICAL)
        rightSizer.Add(self._rightSplitterWindow, 1, wx.EXPAND)
        self._rightPanel.SetSizer(rightSizer)

        self._mainSplitterWindow.SplitVertically(self._leftPanel,
                                                 self._rightPanel)
        self._leftPanel.SetMinSize((_kMinTreeWidth, -1))
        self._rightPanel.SetMinSize((_kMinListWidth + _kMinDetailWidth, -1))

        mainSizer = wx.BoxSizer(wx.VERTICAL)
        mainSizer.Add(self._mainSplitterWindow, 1, wx.EXPAND | wx.ALL,
                      _kBorder)
        self.SetSizer(mainSizer)

        self._bindEvents()
        self._setEmptyMessage(
            "Select a folder on the left to see the photos and videos in it.")


    ###########################################################
    def _makePanel(self, parent):
        """Make a transparent child panel in the house style.

        @param  parent  The parent window.
        @return panel   A wx.Panel.
        """
        panel = wx.Panel(
            parent, wx.ID_ANY, wx.DefaultPosition, wx.DefaultSize,
            (wx.TAB_TRAVERSAL | wx.BORDER_NONE | wx.TRANSPARENT_WINDOW |
             wx.FULL_REPAINT_ON_RESIZE)
        )
        panel.SetBackgroundStyle(kBackgroundStyle)
        return panel


    ###########################################################
    def _buildLeftPanel(self):
        """Folder tree, detection filters and the scan button."""
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = TranslucentStaticText(self._leftPanel, -1, "Folders")
        makeFontBold(label)
        sizer.Add(label, 0, wx.BOTTOM, _kCtrlPadding)

        # First wx.GenericDirCtrl in this codebase.  DIR_ONLY because the file
        # listing is the middle pane's job -- showing files in both places
        # would give two selections that can disagree.
        self._dirCtrl = wx.GenericDirCtrl(
            self._leftPanel, -1, style=wx.DIRCTRL_DIR_ONLY | wx.BORDER_SIMPLE
        )
        # Keep the native list colours: applyToTree repaints backgrounds but
        # repairs contrast only on wx.StaticText, so a recoloured tree keeps
        # black item text -- invisible once the user picks a dark background.
        #
        # The reference in self._dirTreeCtrl is LOAD-BEARING, not tidiness.
        # svKeepOwnBackground is a Python attribute on a wxPython proxy, and
        # the proxy for a window Python did not create lives only as long as
        # something holds it.  Tagging a local and letting it fall out of
        # scope loses the flag: applyToTree later calls GetChildren(), gets a
        # freshly-built proxy with no attribute on it, and recolours the tree
        # anyway.  Measured: with the local, tree background went to the app
        # colour with the text left at (0,0,0).
        self._dirCtrl.svKeepOwnBackground = True
        self._dirTreeCtrl = self._dirCtrl.GetTreeCtrl()
        if self._dirTreeCtrl is not None:
            self._dirTreeCtrl.svKeepOwnBackground = True
        # Proportion 1 so the tree absorbs the height, but with a floor:
        # the filters and the scan button below it must always have room, and
        # a tree that shrinks to nothing is less useful than one that scrolls.
        self._dirCtrl.SetMinSize((-1, _kMinTreeHeight))
        sizer.Add(self._dirCtrl, 1, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)

        self._analyzeButton = wx.Button(self._leftPanel, -1,
                                        "Add folder to database")
        self._analyzeButton.Enable(False)

        self._scanStatus = TranslucentStaticText(self._leftPanel, -1, "")
        makeFontDefault(self._scanStatus)

        sizer.Add(self._analyzeButton, 0, wx.TOP, _kBorder)
        # BOTTOM as well as TOP on the last item: without it the panel's
        # contents land flush on its edge (measured 614..637 in a 637 px
        # panel), which reads as clipped and actually clips in a shorter
        # window.
        sizer.Add(self._scanStatus, 0, wx.TOP | wx.BOTTOM, _kCtrlPadding)

        self._leftPanel.SetSizer(sizer)


    ###########################################################
    def _buildListPanel(self):
        """The thumbnail grid and its heading."""
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._folderLabel = TranslucentStaticText(
            self._listPanel, -1, "Select a folder")
        makeFontBold(self._folderLabel)
        sizer.Add(self._folderLabel, 0, wx.LEFT | wx.BOTTOM, _kCtrlPadding)

        searchRow = wx.BoxSizer(wx.HORIZONTAL)
        self._searchText = wx.TextCtrl(self._listPanel, -1, style=wx.TE_PROCESS_ENTER)
        self._searchText.svKeepOwnBackground = True
        # TextCtrl uses SetHint; SetDescriptiveText exists only on SearchCtrl.
        self._searchText.SetHint('Search saved records: words, phrases, AND / OR / NOT')
        self._searchText.Bind(wx.EVT_TEXT_ENTER, self.OnSearch)
        self._searchText.Bind(wx.EVT_KEY_DOWN, self._onSearchKeyDown)
        searchRow.Add(self._searchText, 1, wx.EXPAND)
        searchButton = wx.Button(self._listPanel, -1, 'Search')
        searchButton.Bind(wx.EVT_BUTTON, self.OnSearch)
        searchRow.Add(searchButton, 0, wx.LEFT, _kCtrlPadding)
        advancedButton = wx.Button(self._listPanel, -1, 'Advanced search')
        advancedButton.Bind(wx.EVT_BUTTON, self.OnAdvancedSearch)
        searchRow.Add(advancedButton, 0, wx.LEFT, _kCtrlPadding)
        matchesButton = wx.Button(self._listPanel, -1, 'Show matches')
        matchesButton.Bind(wx.EVT_BUTTON, self.OnShowSearchMatches)
        searchRow.Add(matchesButton, 0, wx.LEFT, _kCtrlPadding)
        helpButton = wx.Button(self._listPanel, -1, 'Search help')
        helpButton.Bind(wx.EVT_BUTTON, self.OnSearchHelp)
        searchRow.Add(helpButton, 0, wx.LEFT, _kCtrlPadding)
        sizer.Add(searchRow, 0, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)
        from frontEnd.ImageFileSort import SORT_LABELS
        sortRow = wx.BoxSizer(wx.HORIZONTAL)
        sortRow.Add(wx.StaticText(self._listPanel, label='Sort by:'),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kCtrlPadding)
        self._sortChoice = wx.Choice(self._listPanel, choices=SORT_LABELS)
        self._sortChoice.SetSelection(0)
        self._sortChoice.Bind(wx.EVT_CHOICE, self.OnSortChanged)
        sortRow.Add(self._sortChoice)
        sizer.Add(sortRow, 0, wx.BOTTOM, _kCtrlPadding)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        showLabel = TranslucentStaticText(self._listPanel, -1, 'Show only:')
        makeFontBold(showLabel)
        controls.Add(showLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kCtrlPadding)
        self._filterChecks = {}
        for text in _kFilterLabels:
            check = wx.CheckBox(self._listPanel, -1, text)
            makeFontDefault(check)
            self._filterChecks[text] = check
            check.Bind(wx.EVT_CHECKBOX, self.OnFilterChanged)
            controls.Add(check, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kCtrlPadding)
        personLabel = TranslucentStaticText(self._listPanel, -1, 'Person:')
        controls.Add(personLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, _kCtrlPadding)
        self._faceFilterNames = []
        self._faceNameChoice = wx.Choice(self._listPanel, -1, choices=['All names'])
        self._faceNameChoice.SetSelection(0)
        self._faceNameChoice.Bind(wx.EVT_CHOICE, self.OnFilterChanged)
        self._faceNameChoice.SetToolTip('Filter by recognized person')
        controls.Add(self._faceNameChoice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _kCtrlPadding)
        self._searchAllFolders = wx.CheckBox(self._listPanel, -1, 'All indexed folders')
        self._searchAllFolders.Bind(wx.EVT_CHECKBOX, self.OnSearch)
        controls.Add(self._searchAllFolders, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(controls, 0, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)
        self._searchError = wx.StaticText(self._listPanel, -1, '')
        sizer.Add(self._searchError, 0, wx.EXPAND | wx.BOTTOM, _kCtrlPadding)

        self._fileList = ImageThumbGrid(
            self._listPanel, self._logger,
            thumbSize=getFrontEndPref("imageViewThumbSize") or 128)
        self._fileList.Bind(EVT_THUMB_SELECTED, self.OnFileSelected)
        self._fileList.Bind(EVT_THUMB_READY, self.OnThumbnailReady)
        self._fileList.Bind(wx.EVT_CONTEXT_MENU, self.OnThumbnailContextMenu)
        sizer.Add(self._fileList, 1, wx.EXPAND | wx.LEFT, _kCtrlPadding)

        # Shown INSTEAD of the grid when there is nothing in it.  An empty
        # grid is an empty white box, which reads as a fault rather than as
        # an answer to "what is in this folder".
        self._emptyLabel = TranslucentStaticText(self._listPanel, -1, "")
        makeFontDefault(self._emptyLabel)
        sizer.Add(self._emptyLabel, 1,
                  wx.EXPAND | wx.LEFT | wx.TOP, _kCtrlPadding)

        self._listPanel.SetSizer(sizer)


    ###########################################################
    def _bindEvents(self):
        """Bind the controls.  Separate so the order is obvious."""
        self._dirCtrl.Bind(wx.EVT_DIRCTRL_SELECTIONCHANGED,
                           self.OnFolderChanged)
        self._analyzeButton.Bind(wx.EVT_BUTTON, self.OnAnalyzeFolder)
        for check in self._filterChecks.values():
            check.Bind(wx.EVT_CHECKBOX, self.OnFilterChanged)
        self._faceNameChoice.Bind(wx.EVT_CHOICE, self.OnFilterChanged)


    ###########################################################
    def _listFolder(self, path):
        """Show the media files in a folder.

        @param  path  Absolute path of the folder, or "" for none.
        """
        self._selectionScope = None
        self._fileList.setItems([])
        self._files = []
        self._allFiles = []
        self._currentDir = path or ""
        self._detailPanel.clear()

        if not path or not os.path.isdir(path):
            self._folderLabel.SetLabel("Photos and videos")
            self._setEmptyMessage(
                "Select a folder on the left to see the photos and videos "
                "in it.")
            return

        try:
            names = sorted(os.listdir(path), key=lambda n: n.lower())
        except OSError as e:
            # An unreadable folder is ordinary (a system directory, a
            # disconnected drive), so it is a label, not a dialog.
            self._logger.info("ImageView: cannot list %s: %s" % (path, e))
            self._folderLabel.SetLabel(os.path.basename(path) or path)
            self._setEmptyMessage("This folder can't be read.")
            return

        for name in names:
            if not _isMediaFile(name):
                continue
            fullPath = os.path.join(path, name)
            if not os.path.isfile(fullPath):
                # Vanished between listdir and here, or a directory whose name
                # happens to end in an image extension.
                continue
            self._files.append(fullPath)

        self._allFiles = list(self._files)
        self._analyzeButton.Enable(os.path.isdir(self._currentDir))
        self._scanStatus.SetLabel("")

        if not self._allFiles and not self._filtersActive():
            # Handled here rather than in _applyFilters, because "this folder
            # holds nothing we can read" is a different answer from "nothing
            # here matches your filters", and only this one can usefully name
            # the extensions.  _applyFilters owns the heading and the empty
            # state in every other case -- two owners is how they end up
            # disagreeing.
            self._folderLabel.SetLabel(os.path.basename(path) or path)
            self._setEmptyMessage(
                "No photos or videos in this folder.\n\n"
                "Looking for:\n"
                "   %s\n   %s"
                % (", ".join(e[1:] for e in _kImageExts),
                   ", ".join(e[1:] for e in _kVideoExts)))
            return

        self._applyFilters()


    ###########################################################
    def _setEmptyMessage(self, message):
        """Swap between the file list and an explanatory message.

        @param  message  Text to show instead of the list, or None to show
                         the list.
        """
        showList = message is None
        self._fileList.Show(showList)
        self._emptyLabel.Show(not showList)
        if message is not None:
            self._emptyLabel.SetLabel(message)
        self._listPanel.Layout()


    ###########################################################
    def OnFolderChanged(self, event):
        """A folder was picked in the tree.

        @param  event  The EVT_DIRCTRL_SELECTIONCHANGED event.
        """
        event.Skip()
        self._listFolder(self._dirCtrl.GetPath())


    ###########################################################
    def OnFileSelected(self, event):
        """A file was picked in the list.

        @param  event  The EVT_THUMB_SELECTED event.
        """
        event.Skip()
        path = event.getPath()
        if not path:
            return
        self._detailPanel.setFile(
            path, path.lower().endswith(_kVideoExts))
        self._updateVideoPreview()
        self._showStoredDetections(path)


    ###########################################################
    def OnThumbnailReady(self, event):
        """Refresh only the preview when the grid finishes its selected tile."""
        event.Skip()
        if event.getPath() == self._detailPanel.getPath():
            self._updateVideoPreview()


    def OnSortChanged(self, event):
        from frontEnd.ImageFileSort import sortPaths
        selected = self._fileList.getSelection()
        self._files = sortPaths(self._files, self._sortChoice.GetSelection())
        self._fileList.setItems(self._files)
        if selected in self._files:
            self._fileList.selectPath(selected)
        self._updateVideoPreview()


    def OnThumbnailContextMenu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            point = self._fileList.ScreenToClient(position)
            if not self._fileList.selectAtPosition(point):
                return
        path = self._fileList.getSelection()
        if not path:
            return
        menu = wx.Menu()
        reveal = menu.Append(wx.ID_ANY, 'Show in Explorer')
        rename = menu.Append(wx.ID_ANY, 'Rename file...')
        menu.Bind(wx.EVT_MENU, lambda e: self._showInExplorer(path), reveal)
        menu.Bind(wx.EVT_MENU, lambda e: self._renameFile(path), rename)
        try:
            self._fileList.PopupMenu(menu)
        finally:
            menu.Destroy()


    def _showInExplorer(self, path):
        import subprocess
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            # No shell: spaces and punctuation remain part of the file path.
            subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(path)])
        except OSError as exc:
            wx.MessageBox(str(exc), 'Could not open location', wx.OK | wx.ICON_ERROR, self)


    def _renameFile(self, path):
        if self._busyPath or self._scanning or not self._workQueue.empty():
            wx.MessageBox('Wait for Image view analysis to finish before renaming.',
                          'Analysis in progress', wx.OK | wx.ICON_INFORMATION, self)
            return
        db = self._getDb()
        if db is None:
            wx.MessageBox('The media database is unavailable.', 'Could not rename',
                          wx.OK | wx.ICON_ERROR, self)
            return
        with self._dbLock:
            locations = db.getLocations(path)
        dialog = wx.TextEntryDialog(self, 'New filename (keep the extension):',
                                    'Rename file', os.path.basename(path))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            newName = dialog.GetValue()
        finally:
            dialog.Destroy()
        if newName == os.path.basename(path):
            return
        allCopies = False
        if len(locations) > 1:
            dialog = wx.SingleChoiceDialog(
                self, 'This content has %d indexed copies. Rename which copies?\n\n%s'
                % (len(locations), path), 'Rename duplicate files',
                ['This copy only', 'All %d copies' % len(locations)])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                allCopies = dialog.GetSelection() == 1
            finally:
                dialog.Destroy()
        try:
            with wx.BusyCursor():
                with self._dbLock:
                    changes = db.renameFile(path, newName, allCopies)
            self._detailPanel.remapDescriptionDrafts(changes)
            self._allFiles = [changes.get(p, p) for p in self._allFiles]
            from frontEnd.MediaSelections import remapSelections
            saved = getFrontEndPref('imageMediaSelections') or {}
            if saved:
                setFrontEndPref('imageMediaSelections', remapSelections(saved, changes))
            if getattr(self, '_selectionScope', None) is not None:
                self._selectionScope = remapSelections({'scope': self._selectionScope}, changes)['scope']
            newPath = changes[path]
            self._detailPanel.setFile(newPath, newPath.lower().endswith(_kVideoExts))
            self._showStoredDetections(newPath)
            self._applyFilters()
            self._updateVideoPreview()
        except Exception as exc:
            self._logger.exception('ImageView: rename failed')
            wx.MessageBox(str(exc), 'Could not rename', wx.OK | wx.ICON_ERROR, self)


    def _updateVideoPreview(self):
        path, bitmap, failed = self._fileList.getSelectedThumbnail()
        if path:
            self._detailPanel.setVideoPreview(path, bitmap, failed)


    ###########################################################
    def _activeFilters(self):
        """Which filters are ticked.

        @return  (types, wantNudity, wantFace) -- types is a list of
                 objects.type values.
        """
        types = []
        wantNudity = False
        wantFace = False
        for label, check in self._filterChecks.items():
            if not check.GetValue():
                continue
            if label in _kFilterToType:
                types.append(_kFilterToType[label])
            elif label == "Nudity":
                wantNudity = True
            elif label == "Faces":
                wantFace = True
        return (types, wantNudity, wantFace)


    ###########################################################
    def _selectedFaceName(self):
        index = self._faceNameChoice.GetSelection() - 1
        return self._faceFilterNames[index] if 0 <= index < len(self._faceFilterNames) else None


    def _refreshFaceNames(self):
        """Refresh recognized names without losing the user's current filter."""
        db = self._getDb()
        if db is None:
            return
        try:
            with self._dbLock:
                names = db.getFaceNames()
        except Exception:
            self._logger.warning("ImageView: face-name lookup failed", exc_info=True)
            return
        selected = self._selectedFaceName()
        # A re-analysis can remove the last occurrence of this name. Keep the
        # active filter until the user clears it; don't silently widen results.
        if selected and selected not in names:
            names.append(selected)
            names.sort(key=str.casefold)
        if names != self._faceFilterNames:
            self._faceFilterNames = names
            self._faceNameChoice.Set(["All names"] + names)
            self._faceNameChoice.SetSelection(names.index(selected) + 1 if selected else 0)


    def _filtersActive(self):
        types, wantNudity, wantFace = self._activeFilters()
        return bool(types or wantNudity or wantFace or self._selectedFaceName()
                    or self._searchText.GetValue().strip() or self._searchAllFolders.GetValue())


    def OnSearch(self, event):
        self._selectionScope = None
        self._applyFilters()


    def _onSearchKeyDown(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.OnSearch(event)
            return
        event.Skip()


    def OnClearSearch(self, event):
        self._selectionScope = None
        self._searchText.ChangeValue('')
        self._applyFilters()


    def OnAdvancedSearch(self, event):
        from frontEnd.AdvancedMediaSearch import AdvancedMediaSearchDialog, buildQuery
        db = self._getDb()
        if not db:
            wx.MessageBox('The media database is unavailable.', 'Advanced search', wx.OK, self)
            return
        with self._dbLock:
            fields = db.getSearchFields()
        def validate(query):
            with self._dbLock:
                db.compileSearch(query)
        state = getattr(self, '_advancedSearchState', None)
        if state is None or buildQuery(state) != self._searchText.GetValue():
            state = dict(base=self._searchText.GetValue(),
                         rules=[] if self._searchText.GetValue() else [dict(field='all', op='Contains')],
                         join='All')
        dialog = AdvancedMediaSearchDialog(self, fields, validate, state, selectionPaths=list(self._files))
        try:
            result = dialog.ShowModal()
            if result == wx.ID_APPLY and dialog.loadedSelection is not None:
                name, paths = dialog.loadedSelection
                self._loadMediaSelection(name, paths)
            elif result == wx.ID_OK:
                self._advancedSearchState = dialog.getState()
                self._selectionScope = list(dialog.selectionPaths) if dialog.scope.GetSelection() == 1 else None
                self._selectionLabel = 'Current selection'
                self._searchText.ChangeValue(buildQuery(self._advancedSearchState))
                self._applyFilters()
        finally:
            dialog.Destroy()


    def _loadMediaSelection(self, name, paths):
        from frontEnd.MediaSelections import uniquePaths
        paths = uniquePaths(paths)
        self._selectionScope = [p for p in paths if _isMediaFile(p) and os.path.isfile(p)]
        self._selectionLabel = 'Selection: ' + name
        self._searchText.ChangeValue('')
        self._advancedSearchState = None
        for checkbox in self._filterChecks.values(): checkbox.SetValue(False)
        self._faceNameChoice.SetSelection(0)
        self._applyFilters()
        missing = len(paths) - len(self._selectionScope)
        if missing:
            self._searchError.SetLabel('%d saved file(s) are missing, unavailable or unsupported; skipped.' % missing)
            self._listPanel.Layout()


    def OnShowSearchMatches(self, event):
        from frontEnd.AdvancedMediaSearch import showMatches
        path = self._fileList.getSelection()
        query = getattr(self, '_appliedSearchQuery', '')
        if not path or not query:
            wx.MessageBox('Run a text search and select a thumbnail first.', 'Show matches', wx.OK, self)
            return
        db = self._getDb()
        if db:
            with self._dbLock:
                values = db.searchFieldValues(path)
            showMatches(self, query, values)


    def OnSearchHelp(self, event):
        from frontEnd.AdvancedMediaSearch import showHelp
        showHelp(
            'Search saved file details, descriptions, tags, paths and detections.\n'
            'Plain terms use case-insensitive CONTAINS (substring) matching.\n'
            'Press Enter or Search to apply. Existing Show-only filters also apply.\n\n'
            'Advanced search builds conditions for all fields or selected database columns,\n'
            'with exact matches, numeric/date ranges, exclusions, and saved searches.\n'
            'Show matches highlights matching field values for the selected thumbnail.\n'
            'Exact tag matches use semicolon-separated tags; exact person matches use the full name.\n'
            'Example: person:exact:Bernie AND detections.conf:ge:0.8\n\n'
            'Advanced search defaults to New search. Choose Current selection to search\n'
            'only the thumbnails currently displayed. Save/load selections stores that file list,\n'
            'not a query. Loading restores it, clears text/Show only filters, and skips missing files.\n'
            'The normal Search button or selecting a folder starts a new search.\n\n'
            'beach holiday   = both words (implicit AND)\n'
            'Bernie OR Rosemary\n'
            'sailing AND NOT filename:crop\n'
            'person:Bernie AND (tags:sailing OR ai:"blue sail")\n'
            'tags CONTAINS family\n\n'
            '"door"   = whole word door, not outdoor, doorbell or indoors\n'
            'word:door still works. ai:"door" limits the whole word to AI descriptions.\n'
            'ai:word:door   = whole word in the AI description only\n'
            'tags:word:"front door"   = literal phrase with word boundaries\n'
            'word:door AND NOT word:garage\n'
            'Whole words ignore case; punctuation, spaces and underscores separate words.\n'
            'door and doors are different words. A record with door AND outdoor\n'
            'still matches "door". "front door" matches the phrase with word boundaries.\n'
            'Use CONTAINS "door" or ai CONTAINS "door" for quoted substring matching.\n\n'
            'has:tags AND NOT has:ai   = tags filled, AI description blank\n'
            'empty:description_ai     = blank AI description\n'
            'Blank includes NULL, empty text, spaces and line breaks.\n\n'
            'Quote phrases and words such as "and". Use parentheses to group terms.\n'
            'Precedence: NOT, then AND, then OR.\n'
            'Fields: filename, path, tags, ai, person, type, subType, gender, age,\n'
            'and other saved file/detection column names.\n'
            'Without a field, all saved columns and duplicate paths are searched.\n'
            'EXIF read directly from files is not indexed.\n\n'
            'By default: selected folder and subfolders.\n'
            'All indexed folders: search across the saved library.\n'
            'Only files with saved records can match text searches.',
            'Image search help', wx.OK | wx.ICON_INFORMATION, self)


    def _applyFilters(self):
        """Filter saved results in this folder and all of its subfolders."""
        allFolders = self._searchAllFolders.GetValue()
        selection = getattr(self, '_selectionScope', None)
        query = self._searchText.GetValue().strip()
        self._searchError.SetLabel('')
        if selection is None and not allFolders and (not self._currentDir or not os.path.isdir(self._currentDir)):
            self._searchError.SetLabel('Choose a folder or enable All indexed folders.')
            self._listPanel.Layout()
            return
        types, wantNudity, wantFace = self._activeFilters()
        self._refreshFaceNames()
        faceName = self._selectedFaceName()

        if not types and not wantNudity and not wantFace and not faceName and not query and (selection is not None or not allFolders):
            self._appliedSearchQuery = ''
            from frontEnd.ImageFileSort import sortPaths
            source = self._allFiles if selection is None else [p for p in selection if _isMediaFile(p) and os.path.isfile(p)]
            self._files = sortPaths(source, self._sortChoice.GetSelection())
            self._fileList.setItems(self._files)
            selected = self._detailPanel.getPath()
            if selected in self._files:
                self._fileList.selectPath(selected)
            else:
                self._detailPanel.clear()
            self._updateFolderLabel()
            # Restoring the grid is NOT optional on this path.  Without it,
            # any earlier empty state -- "select a folder", "nothing matches"
            # -- stays on screen over a folder that does have files, and the
            # heading then contradicts the pane underneath it.
            self._setEmptyMessage(None if self._files else
                                  ("This selection has no available files." if selection is not None else
                                   "No photos or videos directly in this folder."))
            return

        db = self._getDb()
        matching = None
        if db is not None:
            try:
                with self._dbLock:
                    matching = db.pathsMatching(self._currentDir, types,
                                                wantNudity, wantFace, faceName=faceName,
                                                recursive=True, query=query, allFolders=allFolders or selection is not None)
            except ValueError as exc:
                self._searchError.SetLabel('Search error: %s' % exc)
                self._listPanel.Layout()
                return
            except Exception:
                self._logger.warning("ImageView: filter query failed: %s"
                                     % traceback.format_exc())
                self._searchError.SetLabel('Search failed. Try again; see the log for details.')
                self._listPanel.Layout()
                return

        if matching is None:
            matching = set()
        if selection is not None:
            from frontEnd.MediaSelections import withinSelection
            matching = withinSelection(matching, selection)
        self._appliedSearchQuery = query

        # Matching paths may be below the current folder and therefore absent
        # from the direct-child browsing list. Ignore missing/unsupported files.
        from frontEnd.ImageFileSort import sortPaths
        self._files = sortPaths((p for p in matching
                                if _isMediaFile(p) and os.path.isfile(p)),
                               self._sortChoice.GetSelection())
        self._fileList.setItems(self._files)
        selected = self._detailPanel.getPath()
        if selected in self._files:
            self._fileList.selectPath(selected)
        else:
            self._detailPanel.clear()
        self._updateFolderLabel()

        if not self._files:
            self._setEmptyMessage(
                "No saved files match this search and the selected filters.\n\n"
                "Text searches use saved records; detection filters require analysis.\n"
                "Try other terms or select All indexed folders.")
        else:
            self._setEmptyMessage(None)


    ###########################################################
    def _updateFolderLabel(self):
        """Put the visible/total counts in the heading."""
        if getattr(self, '_selectionScope', None) is not None:
            self._folderLabel.SetLabel('%s -- %d of %d files' % (
                getattr(self, '_selectionLabel', 'Current selection'), len(self._files), len(self._selectionScope)))
            self._listPanel.Layout()
            return
        if not self._currentDir and not self._searchAllFolders.GetValue():
            return
        name = ('All indexed folders' if self._searchAllFolders.GetValue() else
                os.path.basename(self._currentDir) or self._currentDir)
        shown, total = len(self._files), len(self._allFiles)
        if self._filtersActive():
            self._folderLabel.SetLabel(
                "%s  --  %d matching file%s (including subfolders)"
                % (name, shown, "" if shown == 1 else "s"))
        elif shown == total:
            self._folderLabel.SetLabel(
                "%s  --  %d file%s" % (name, total,
                                       "" if total == 1 else "s"))
        else:
            self._folderLabel.SetLabel(
                "%s  --  %d of %d file%s" % (name, shown, total,
                                             "" if total == 1 else "s"))
        self._listPanel.Layout()


    ###########################################################
    def _countUnanalysed(self):
        """How many files in this folder have never been analysed.

        @return int
        """
        db = self._getDb()
        if db is None:
            return len(self._allFiles)
        count = 0
        try:
            with self._dbLock:
                for path in self._allFiles:
                    row = db.getFile(path)
                    if row is None or row["analyzedMs"] is None:
                        count += 1
        except Exception:
            return len(self._allFiles)
        return count


    ###########################################################
    def OnFilterChanged(self, event):
        """A detection checkbox or recognized-person selection changed.

        @param  event  The checkbox or choice event.
        """
        event.Skip()
        self._applyFilters()


    ###########################################################
    def OnAnalyzeFolder(self, event):
        """Register a folder recursively, with optional detections."""
        event.Skip()
        if self._scanning:
            self._folderCancel.set()
            self._analyzeButton.Enable(False)
            self._scanStatus.SetLabel("Stopping after the current operation...")
            return
        if not os.path.isdir(self._currentDir):
            return
        db = self._getDb()
        if db is None:
            self._scanStatus.SetLabel("No database; cannot store results.")
            return
        dialog = wx.SingleChoiceDialog(
            self, "Add supported photos and videos in this folder and its subfolders.\n"
            "Skip dot-prefixed files/folders and folders containing RAW.\n"
            "Both options verify and link identical files.\n\n" + self._currentDir,
            "Add folder to database",
            ["Add files with analysis (default)",
             "Add filenames only (no new detections)"])
        dialog.SetSelection(0)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            analyze = dialog.GetSelection() == 0
        finally:
            dialog.Destroy()
        self._scanning = True
        self._folderCancel = threading.Event()
        self._analyzeButton.SetLabel("Stop")
        self._scanStatus.SetLabel("Preparing folder import...")
        self._ensureWorker()
        self._workQueue.put(dict(root=self._currentDir, analyze=analyze,
                                 cancel=self._folderCancel))


    def _confirmFolderModels(self, missing, response, ready, cancel):
        try:
            if cancel.is_set() or self._stopEvent.is_set():
                return
            dialog = wx.MessageDialog(
                self, "%s detection models are not loaded or are disabled.\n\n"
                "Continue without these detections, or abort the folder import?"
                % " and ".join(missing), "Analysis models unavailable",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            dialog.SetYesNoLabels("Continue", "Abort")
            try:
                response.append(dialog.ShowModal() == wx.ID_YES)
            finally:
                dialog.Destroy()
        finally:
            ready.set()


    def _importFolder(self, job):
        """Worker-side service check, traversal, hashing and optional analysis."""
        cancel = job['cancel']
        cancelled = lambda: cancel.is_set() or self._stopEvent.is_set()
        client = None
        message = "Folder import aborted."
        try:
            if cancelled():
                return
            cfg = UserMediaAnalysis.loadConfig() if job['analyze'] else {}
            if job['analyze']:
                client = UserMediaAnalysis.openDetectionClient(self._logger)
                if client is None:
                    message = "Detection service unavailable. No files added; try filenames only."
                    return
                cfg, missing = UserMediaFolderImport.availableConfig(cfg, client.ping())
                if missing:
                    ready, response = threading.Event(), []
                    self._post(self._confirmFolderModels, missing, response, ready, cancel)
                    while not ready.wait(0.1):
                        if cancelled():
                            return
                    if not response or not response[0] or cancelled():
                        return
            db = self._getDb()
            if db is None:
                raise RuntimeError("User media database unavailable")
            counts = UserMediaFolderImport.importFolder(
                job['root'], db, self._dbLock, cfg, client, job['analyze'],
                cancelled=cancelled, logger=self._logger,
                progress=lambda c: self._post(self._folderProgress, cancel, c),
                pause=lambda: cancel.wait(_kScanGapSecs))
            message = ("Stopped. " if cancelled() else "Complete. ") + self._folderSummary(counts)
        except Exception:
            self._logger.error("ImageView: folder import failed: %s" % traceback.format_exc())
            message = "Folder import failed; see the front-end log."
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self._post(self._folderFinished, cancel, message)


    @staticmethod
    def _folderSummary(counts):
        return ("%d registered; %d analysed; %d existing results reused; %d errors."
                % (counts['registered'], counts['analyzed'], counts['reused'], counts['failed']))


    def _folderProgress(self, cancel, counts):
        if cancel is self._folderCancel and not cancel.is_set():
            self._scanStatus.SetLabel(self._folderSummary(counts))
            self._leftPanel.Layout()


    def _folderFinished(self, cancel, message):
        if cancel is not self._folderCancel:
            return
        self._scanning = False
        self._analyzeButton.SetLabel("Add folder to database")
        self._analyzeButton.Enable(os.path.isdir(self._currentDir))
        self._scanStatus.SetLabel(message)
        self._leftPanel.Layout()
        self._refreshFaceNames()
        self._applyFilters()
        path = self._detailPanel.getPath()
        if path:
            self._detailPanel.setFile(path, path.lower().endswith(_kVideoExts))
            self._showStoredDetections(path)


    ###########################################################
    def _getDb(self):
        """Open the detections database on first use.

        Lazy because a user who never opens this tab should not pay for it,
        and because a failure here must degrade browsing to "no detections"
        rather than stopping the tab from existing.

        @return db  A UserMediaDb, or None if it could not be opened.
        """
        with self._dbLock:
            if self._db is None:
                try:
                    self._db = UserMediaDb(self._logger).open()
                except Exception:
                    self._logger.error("ImageView: cannot open the user "
                                       "media database: %s"
                                       % traceback.format_exc())
                    self._db = False
            return self._db or None


    ###########################################################
    def _loadDescriptions(self, path):
        """Read user text independently of whether analysis has run."""
        db = self._getDb()
        if db is None:
            raise RuntimeError("The user media database is unavailable")
        with self._dbLock:
            values = db.getDescriptions(path)
            values['locations'] = db.getLocations(path)
            return values


    def _saveDescriptions(self, path, tags, description):
        """Serialize description writes with the analysis worker."""
        db = self._getDb()
        if db is None:
            raise RuntimeError("The user media database is unavailable")
        with self._dbLock:
            db.saveDescriptions(path, tags, description)


    ###########################################################
    def _showStoredDetections(self, path):
        """Put whatever we have stored for a file into the detail pane.

        @param  path  Absolute path.
        """
        if self._busyPath == path:
            self._detailPanel.setDetections(None, None, busy=True)
            return

        db = self._getDb()
        if db is None:
            self._detailPanel.setDetections(None, None)
            return

        try:
            with self._dbLock:
                fileRow = db.getFile(path)
                rows = db.getDetections(path) if fileRow is not None else []
        except Exception:
            self._logger.warning("ImageView: detection lookup failed: %s"
                                 % traceback.format_exc())
            self._detailPanel.setDetections(None, None)
            return

        if fileRow is None or fileRow["analyzedMs"] is None:
            self._detailPanel.setDetections(None, None)
        else:
            self._detailPanel.setDetections(rows, fileRow)


    ###########################################################
    def _onAnalyzeRequested(self, path):
        """Analyze was clicked for one file.

        @param  path  Absolute path.
        """
        self._ensureWorker()
        self._busyPath = path
        self._detailPanel.setDetections(None, None, busy=True)
        self._detailPanel.setStatus("Queued...")
        self._workQueue.put(path)


    ###########################################################
    def _ensureWorker(self):
        """Start the analysis worker if it is not already running."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopEvent.clear()
        self._worker = threading.Thread(target=self._workerLoop,
                                        name="ImageViewAnalysis",
                                        daemon=True)
        self._worker.start()


    ###########################################################
    def stopWorker(self):
        """Stop the analysis thread and wait briefly for it to notice.

        Registered as an exit notification, and also called when the view is
        destroyed.  Safe to call more than once.
        """
        if getattr(self, '_fileList', None) is not None:
            try:
                self._fileList.stop()
            except Exception:
                pass
        self._scanning = False
        self._stopEvent.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            # A join with a timeout, not an unbounded one: the worker can be
            # inside a detection RPC that takes seconds, and a shutdown that
            # can hang is worse than one that leaves a daemon thread to die
            # with the process.
            worker.join(timeout=2.0)
        self._worker = None
        with self._dbLock:
            if self._db:
                try:
                    self._db.close()
                except Exception:
                    pass
            self._db = None


    ###########################################################
    def _workerLoop(self):
        """Analyse queued files, one at a time, until told to stop.

        Runs OFF the UI thread.  Everything it sends back goes through
        wx.CallAfter, and every one of those callbacks has to assume the view
        may have been destroyed in the meantime.
        """
        client = None
        try:
            while not self._stopEvent.is_set():
                try:
                    path = self._workQueue.get(timeout=_kWorkerPollSecs)
                except queue.Empty:
                    continue

                if self._stopEvent.is_set():
                    break

                if isinstance(path, dict):
                    self._importFolder(path)
                    continue

                if client is None:
                    client = UserMediaAnalysis.openDetectionClient(
                        self._logger)
                    if client is None:
                        self._post(self._analysisFailed, path,
                                   "The detection service is not running. "
                                   "Start the app with Start.bat so the back "
                                   "end is up, then try again.")
                        continue

                self._analyseOne(path, client)

                if self._scanning:
                    # Leave a gap in the shared inference lock.  Interruptible,
                    # so Stop and app exit stay immediate.
                    self._stopEvent.wait(_kScanGapSecs)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass


    ###########################################################
    def _analyseOne(self, path, client):
        """Analyse one file and store the result.

        @param  path    Absolute path.
        @param  client  A DetectionServiceClient.
        """
        try:
            cfg = UserMediaAnalysis.loadConfig()
            self._post(self._detailPanel.setStatus, "Analyzing...")

            def progress(done, total):
                self._post(self._detailPanel.setStatus,
                           "Analyzing... frame %d of %d" % (done, total))

            result = UserMediaAnalysis.analyzeFile(
                path, client, cfg, logger=self._logger,
                **({"progressFn": progress,
                    "cancelFn": self._stopEvent.is_set}
                   if UserMediaAnalysis.isVideo(path) else {}))

            db = self._getDb()
            if db is not None:
                with self._dbLock:
                    db.saveResult(path, result)
        except Exception:
            self._logger.error("ImageView: analysis failed for %s: %s"
                               % (path, traceback.format_exc()))
            self._post(self._analysisFailed, path,
                       "Analysis failed; see the front-end log.")
            return

        self._post(self._analysisDone, path, result)


    ###########################################################
    def _post(self, fn, *args):
        """Call something on the UI thread, if the view still exists.

        @param  fn    The callable.
        @param  args  Its arguments.
        """
        def guarded():
            # `not self` is the documented wx test for a window whose C++ side
            # has been destroyed.  Without it, a result arriving after the user
            # switched away is a call into freed memory.
            if not self:
                return
            try:
                fn(*args)
            except Exception:
                self._logger.warning("ImageView: UI update failed: %s"
                                     % traceback.format_exc())
        wx.CallAfter(guarded)


    ###########################################################
    def _analysisDone(self, path, result):
        """A file finished analysing.  Refresh the pane if it is still shown.

        @param  path    The file that was analysed.
        @param  result  The analysis result dict.
        """
        if self._busyPath == path:
            self._busyPath = None
        self._refreshFaceNames()
        if not self._scanning and self._filtersActive():
            self._applyFilters()

        elapsed = result.get("elapsedMs") or 0
        note = "Analysed in %.1f s" % (elapsed / 1000.0)
        if result.get("kind") == "video" and result.get("sampled"):
            note += " (%d frames sampled" % result["sampled"]
            note += ", capped" if result.get("truncated") else ""
            note += ")"

        if self._detailPanel.getPath() == path:
            self._showStoredDetections(path)
            self._detailPanel.setStatus(note)


    ###########################################################
    def _analysisFailed(self, path, message):
        """A file could not be analysed.

        @param  path     The file.
        @param  message  What to tell the user.
        """
        if self._busyPath == path:
            self._busyPath = None
        if self._detailPanel.getPath() == path:
            # Re-read first: without this the pane keeps saying "Analyzing..."
            # for the rest of the session, because the only thing that clears
            # that text is a detections refresh.
            self._showStoredDetections(path)
            self._detailPanel.setBusy(False)
            self._detailPanel.setStatus(message)


    ###########################################################
    def _savePrefs(self):
        """Save sash positions and the folder we were in.

        Registered with the top-level window, which calls it on close.
        """
        try:
            setFrontEndPref("imageViewSashPos1",
                            self._mainSplitterWindow.GetSashPosition(0))
            setFrontEndPref("imageViewSashPos2",
                            self._rightSplitterWindow.GetSashPosition(0))
            setFrontEndPref("imageViewLastFolder", self._currentDir)
            setFrontEndPref("imageViewThumbSize",
                            self._fileList.getThumbSize())
        except Exception:
            # Never let a pref write stop the app from closing.
            self._logger.warning("ImageView: could not save prefs",
                                 exc_info=True)


    ###########################################################
    def _loadPrefs(self):
        """Restore sash positions and the last folder.

        Registered with the top-level window, which calls it once the views
        have been given a size.  A missing pref becomes 1 rather than being
        skipped: setting the sash forces the splitter to resize and settle on
        a sane value, which leaving it alone does not.
        """
        pos1 = getFrontEndPref("imageViewSashPos1")
        pos2 = getFrontEndPref("imageViewSashPos2")

        # MonitorView passes 1 here and lets the splitter settle.  That does
        # not work for this view: with no saved position the splitter falls
        # back to best sizes, and a GenericDirCtrl's best size takes over half
        # the window (measured: 511 px of 1184).  Give it real defaults.
        if not pos1:
            pos1 = _kDefaultTreeWidth

        self._mainSplitterWindow.SetSashPosition(0, pos1)

        if not pos2:
            # Derive the width the right-hand side is ABOUT to have, rather
            # than reading _rightPanel -- it still holds its pre-split width
            # until the layout that follows this call, and using that left the
            # detail pane 228 px wide instead of the 320 asked for.
            clientWidth = self.GetClientSize().width
            rightWidth = (clientWidth - pos1
                          - self._mainSplitterWindow.GetSashSize()
                          - 2 * _kBorder)
            if rightWidth <= 0:
                # Not laid out yet; the splitter will clamp this sensibly.
                rightWidth = _kMinListWidth + _kDefaultDetailWidth
            pos2 = max(_kMinListWidth, rightWidth - _kDefaultDetailWidth)

        self._rightSplitterWindow.SetSashPosition(0, pos2)

        lastFolder = getFrontEndPref("imageViewLastFolder")
        if lastFolder and os.path.isdir(lastFolder):
            try:
                self._dirCtrl.ExpandPath(lastFolder)
                self._listFolder(lastFolder)
            except Exception:
                self._logger.info("ImageView: could not reopen %s"
                                  % lastFolder)


    ###########################################################
    def _onDestroy(self, event):
        """Release anything that could outlive the window.

        The guard matters: EVT_WINDOW_DESTROY propagates up from every child,
        so without it this runs once per control in the view.
        """
        if event.GetEventObject() == self:
            self.stopWorker()
        event.Skip()


    ###########################################################
    def setActiveView(self, viewParams=None):
        """@see BaseView.setActiveView

        Keep this cheap.  FrontEndFrame calls _switchView() on every view while
        the frame is still being constructed, to measure the best size, so real
        work here lands in cold start behind the startup window.  Anything
        expensive belongs in a wx.CallAfter.
        """
        super(ImageView, self).setActiveView(viewParams)
        self._suspendPlaybackAccelerators()


    def _suspendPlaybackAccelerators(self):
        """Release video-search shortcuts while editing/browsing user media.

        On Windows, even disabled menu items consume their accelerator keys
        before a TextCtrl receives EVT_KEY_DOWN. Remove the accelerator suffixes
        while this view is active and restore them when leaving it.
        """
        from frontEnd import MenuIds
        if getattr(self, '_playbackAcceleratorLabels', None):
            return
        self._playbackAcceleratorLabels = []
        menuBar = self.GetTopLevelParent().GetMenuBar()
        index = menuBar.FindMenu(MenuIds.kControlsMenu)
        if index == wx.NOT_FOUND:
            return
        for item in menuBar.GetMenu(index).GetMenuItems():
            label = item.GetItemLabel()
            if '\t' in label:
                self._playbackAcceleratorLabels.append((item, label))
                item.SetItemLabel(label.split('\t', 1)[0])


    def _restorePlaybackAccelerators(self):
        for item, label in getattr(self, '_playbackAcceleratorLabels', []):
            item.SetItemLabel(label)
        self._playbackAcceleratorLabels = []


    ###########################################################
    def deactivateView(self):
        """@see BaseView.deactivateView"""
        self._restorePlaybackAccelerators()
        super(ImageView, self).deactivateView()


    ###########################################################
    def prepareToClose(self):
        """@see BaseView.prepareToClose

        NOTE: FrontEndFrame calls this on the CURRENT view only, so it can
        never be the only place threads are stopped -- quitting from another
        tab would skip it entirely.  Thread shutdown goes through
        registerExitNotification(), which fires for every registrant.
        """
        self.stopWorker()
