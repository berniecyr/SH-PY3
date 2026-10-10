#! /usr/local/bin/python

#*****************************************************************************
#
# ImageThumbGrid.py
#     A scrolling grid of thumbnails for the user's photos and videos.
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
A scrolling grid of thumbnails, sized for folders with thousands of files.

### Why this is not wx.lib.agw.thumbnailctrl

wxPython ships a thumbnail browser and it was rejected after reading its source
(venv\Lib\site-packages\wx\lib\agw\).  Four findings, each verified:

  * **It deletes the user's originals.**  ThumbnailCtrl.OnThumbChar routes
    WXK_DELETE to DeleteFiles, which calls os.remove on the real file behind one
    confirmation dialog.  Its own docstring says "this method deletes the
    original files too."  For a browser over someone's irreplaceable photos that
    is disqualifying by itself.
  * **It keeps every image at full resolution.**  Thumb.LoadImage stores the
    decoded wx.Image and never downscales it, so a few hundred 12 MP photos
    exhaust memory.
  * **Its loader thread cannot be stopped.**  ShowThumbs clears _isrunning and
    then sets it back two lines later, swaps the item list out from under the
    running thread, and starts a raw unjoinable _thread -- which then calls
    Refresh() from off the main thread.  Nothing stops it when the view closes.
  * **It does not recognise .mp4.**  Its extension list stops at .mpeg/.mpg/.mov.

### What this does instead

  * **Virtual painting.**  Only tiles inside the viewport are drawn, so paint
    cost is a function of window size, not folder size.
  * **Bounded memory.**  Decoded bitmaps live in an LRU capped at
    _kMaxLiveBitmaps; everything else is dropped and re-read from the on-disk
    cache, which is cheap.  Nothing ever holds a full-resolution decode.
  * **A stoppable worker.**  One thread, an explicit threading.Event, and a
    queue that is drained on stop.  It is joinable, and the view joins it.
  * **A disk cache** under <dataDir>\usermedia\thumbs, keyed by path, mtime,
    size and thumbnail size, so a second visit to a folder is near-instant.
    Deliberately NOT under videos\ -- see UserMediaDb for why that matters.
    Spread over 256 subfolders, indexed, and cleaned of thumbnails whose
    original is gone by a background pass -- see ImageThumbCache.

### Public surface

Kept deliberately small -- setItems / getSelection / setThumbSize / stop, plus
EVT_THUMB_SELECTED -- because when the Qt migration reaches FrontEndFrame this
is the control most worth replacing with a QListView, and a narrow interface is
what makes that a contained change.
"""

# Python imports...
import os
import queue
import subprocess
import threading
import traceback

# Common 3rd-party imports...
import wx

# Toolbox imports...

# Local imports...
from frontEnd.ImageThumbCache import (getThumbCacheDir, cacheKey, shardPath,
                                     findThumb, recordThumb,
                                     startMaintenance, stopMaintenance)


# Constants...

# Tile geometry.
_kDefaultThumbSize = 128
_kTilePadding = 10
_kCaptionHeight = 16
_kSelectionRadius = 4

# How many decoded bitmaps to keep.  At 128 px that is about 49 KB each, so
# 600 tiles is roughly 29 MB -- bounded, and far more than any viewport shows.
_kMaxLiveBitmaps = 600

# How far outside the viewport to decode ahead, in rows.  Enough that a scroll
# lands on ready tiles, small enough that arriving in a 4000-file folder does
# not queue 4000 decodes.
_kPrefetchRows = 3

# Worker idle poll; short enough that stopping is immediate.
_kWorkerPollSecs = 0.2

# Windows: never flash a console for the ffmpeg we spawn for video frames.
_kNoWindow = (subprocess.CREATE_NO_WINDOW
              if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)

_kVideoExts = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.mpg', '.mpeg',
               '.wmv', '.webm')

# Tile states.
_kPending = 0
_kReady = 1
_kFailed = 2


# Events...
myEVT_THUMB_SELECTED = wx.NewEventType()
EVT_THUMB_SELECTED = wx.PyEventBinder(myEVT_THUMB_SELECTED, 1)
myEVT_THUMB_READY = wx.NewEventType()
EVT_THUMB_READY = wx.PyEventBinder(myEVT_THUMB_READY, 1)
# A tile was double-clicked: open it large.
myEVT_THUMB_ACTIVATED = wx.NewEventType()
EVT_THUMB_ACTIVATED = wx.PyEventBinder(myEVT_THUMB_ACTIVATED, 1)


##############################################################################
class ThumbSelectedEvent(wx.PyCommandEvent):
    """Fired when the selected tile changes."""

    def __init__(self, evtType, uid, path):
        super(ThumbSelectedEvent, self).__init__(evtType, uid)
        self._path = path

    def getPath(self):
        """@return  Absolute path of the selected file, or None."""
        return self._path


##############################################################################
def drawVideoBadge(dc, x, y):
    """Draw the shared play marker at the given top-left pixel."""
    size = 14
    dc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, 160)))
    dc.SetPen(wx.Pen(wx.Colour(0, 0, 0)))
    dc.DrawCircle(x + size // 2, y + size // 2, size // 2)
    dc.SetBrush(wx.Brush(wx.Colour(255, 255, 255)))
    dc.SetPen(wx.Pen(wx.Colour(255, 255, 255)))
    cx, cy = x + size // 2, y + size // 2
    dc.DrawPolygon([wx.Point(cx - 2, cy - 4), wx.Point(cx - 2, cy + 4),
                    wx.Point(cx + 4, cy)])


##############################################################################
class _Tile(object):
    """One file's place in the grid."""

    __slots__ = ("path", "isVideo", "bitmap", "state")

    def __init__(self, path):
        self.path = path
        self.isVideo = path.lower().endswith(_kVideoExts)
        self.bitmap = None
        self.state = _kPending


##############################################################################
class ImageThumbGrid(wx.ScrolledWindow):
    """A virtual, bounded-memory grid of file thumbnails."""

    ###########################################################
    def __init__(self, parent, logger, thumbSize=_kDefaultThumbSize):
        """Initializer for ImageThumbGrid.

        @param  parent     The parent window.
        @param  logger     A logger.
        @param  thumbSize  Tile image size in pixels.
        """
        super(ImageThumbGrid, self).__init__(
            parent, -1, style=wx.BORDER_SIMPLE | wx.WANTS_CHARS)

        self._logger = logger
        self._thumbSize = thumbSize
        self._tiles = []
        self._selection = -1
        # Every highlighted tile; _selection is the one the details pane shows
        # and the keyboard moves from.  _anchor is where a Shift+click range
        # starts.
        self._marked = set()
        self._anchor = -1
        self._cols = 1

        # LRU of indices whose bitmap is currently held.
        self._live = []

        # Worker plumbing.
        self._queue = queue.Queue()
        self._queued = set()
        self._queuedLock = threading.Lock()
        self._stopEvent = threading.Event()
        self._worker = None

        # Background tidy of the disk cache.  Returns at once; the thread
        # waits a minute before touching the disk so startup never pays.
        startMaintenance(logger, thumbSize)

        # It paints every pixel it owns, so let AppColors leave it alone --
        # applyToTree repairs contrast only on wx.StaticText, and a recoloured
        # background under unchanged caption text is how this goes unreadable.
        self.svKeepOwnBackground = True

        self.SetBackgroundColour(wx.Colour(255, 255, 255))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetScrollRate(0, 20)

        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_SIZE, self.OnSize)
        self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
        self.Bind(wx.EVT_LEFT_DCLICK, self.OnLeftDClick)
        self.Bind(wx.EVT_KEY_DOWN, self.OnKeyDown)
        self.Bind(wx.EVT_SCROLLWIN, self.OnScrollWin)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._onDestroy)


    # -- public surface ---------------------------------------------------

    ###########################################################
    def setItems(self, paths):
        """Replace everything shown.

        @param  paths  Absolute paths, in display order.
        """
        self._drainQueue()
        self._tiles = [_Tile(p) for p in paths]
        self._live = []
        self._selection = -1
        self._marked = set()
        self._anchor = -1
        self.Scroll(0, 0)
        self._relayout()
        self.Refresh()
        self._requestVisible()


    ###########################################################
    def getSelection(self):
        """@return  Absolute path of the selected file, or None."""
        if 0 <= self._selection < len(self._tiles):
            return self._tiles[self._selection].path
        return None


    ###########################################################
    def getSelectedPaths(self):
        """@return  Every highlighted file, in display order."""
        indices = set(self._marked)
        if 0 <= self._selection < len(self._tiles):
            indices.add(self._selection)
        return [self._tiles[i].path for i in sorted(indices)
                if 0 <= i < len(self._tiles)]


    ###########################################################
    def selectAll(self):
        """Highlight every tile (Ctrl+A)."""
        if not self._tiles:
            return
        if self._selection < 0:
            self._setSelection(0)
        self._marked = set(range(len(self._tiles)))
        self.Refresh()


    ###########################################################
    def getSelectedThumbnail(self):
        """Return (path, cached bitmap, failed) without starting another decode."""
        if 0 <= self._selection < len(self._tiles):
            tile = self._tiles[self._selection]
            return tile.path, tile.bitmap, tile.state == _kFailed
        return None, None, False


    ###########################################################
    def selectPath(self, path):
        """Select a file by path, if it is shown.

        @param  path  Absolute path.
        @return bool  True if it was found.
        """
        for i, tile in enumerate(self._tiles):
            if tile.path == path:
                self._setSelection(i, notify=False)
                # Scroll even when it was already selected: the grid may have
                # been hidden behind the large view while the user moved on.
                self._scrollIntoView(i)
                return True
        return False


    ###########################################################
    def setThumbSize(self, size):
        """Change the tile size.

        @param  size  Image size in pixels.
        """
        size = max(48, min(320, int(size)))
        if size == self._thumbSize:
            return
        self._thumbSize = size
        # Every cached bitmap is the wrong size now.  Drop them and re-request
        # only what is visible -- NOT the whole folder, which is the mistake
        # the shipped control makes on a zoom keypress.
        self._drainQueue()
        for tile in self._tiles:
            tile.bitmap = None
            tile.state = _kPending
        self._live = []
        self._relayout()
        self.Refresh()
        self._requestVisible()


    ###########################################################
    def getThumbSize(self):
        """@return  Current tile image size in pixels."""
        return self._thumbSize


    ###########################################################
    def stop(self):
        """Stop the decode thread.  Safe to call more than once."""
        stopMaintenance()
        self._stopEvent.set()
        self._drainQueue()
        worker = self._worker
        if worker is not None and worker.is_alive():
            # Bounded: the worker can be inside an ffmpeg call, and a shutdown
            # that can hang is worse than a daemon thread dying with the
            # process.
            worker.join(timeout=2.0)
        self._worker = None


    # -- layout and painting ----------------------------------------------

    ###########################################################
    def _tileSize(self):
        """@return  (width, height) of one tile including padding."""
        return (self._thumbSize + 2 * _kTilePadding,
                self._thumbSize + 2 * _kTilePadding + _kCaptionHeight)


    ###########################################################
    def _relayout(self):
        """Recompute the column count and the virtual size."""
        tileW, tileH = self._tileSize()
        width = max(tileW, self.GetClientSize().width)
        self._cols = max(1, width // tileW)
        rows = (len(self._tiles) + self._cols - 1) // self._cols
        self.SetVirtualSize((self._cols * tileW, max(1, rows * tileH)))


    ###########################################################
    def _visibleRange(self, prefetchRows=0):
        """Which tile indices are on screen.

        @param  prefetchRows  Extra rows to include either side.
        @return (first, last)  Inclusive index range, clamped.
        """
        if not self._tiles:
            return (0, -1)
        _, tileH = self._tileSize()
        _, scrollY = self.GetViewStart()
        _, unitY = self.GetScrollPixelsPerUnit()
        top = scrollY * unitY
        height = self.GetClientSize().height

        firstRow = max(0, top // tileH - prefetchRows)
        lastRow = (top + height) // tileH + prefetchRows
        first = int(firstRow * self._cols)
        last = int((lastRow + 1) * self._cols - 1)
        return (max(0, first), min(len(self._tiles) - 1, last))


    ###########################################################
    def _tileRect(self, index):
        """Where a tile sits in virtual coordinates.

        @param  index  Tile index.
        @return wx.Rect
        """
        tileW, tileH = self._tileSize()
        row, col = divmod(index, self._cols)
        return wx.Rect(col * tileW, row * tileH, tileW, tileH)


    ###########################################################
    def OnPaint(self, event):
        """Draw the visible tiles only.

        @param  event  The EVT_PAINT event.
        """
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        self.DoPrepareDC(dc)

        if not self._tiles:
            return

        first, last = self._visibleRange()
        selBrush = wx.Brush(wx.Colour(205, 226, 252))
        selPen = wx.Pen(wx.Colour(120, 170, 230))
        textColour = wx.Colour(40, 40, 40)
        dimColour = wx.Colour(150, 150, 150)

        font = self.GetFont()
        dc.SetFont(font)

        for index in range(first, last + 1):
            tile = self._tiles[index]
            rect = self._tileRect(index)

            if index == self._selection or index in self._marked:
                dc.SetBrush(selBrush)
                dc.SetPen(selPen)
                dc.DrawRoundedRectangle(rect, _kSelectionRadius)

            if tile.bitmap is not None:
                bmpW, bmpH = tile.bitmap.GetWidth(), tile.bitmap.GetHeight()
                x = rect.x + (rect.width - bmpW) // 2
                y = rect.y + _kTilePadding + (self._thumbSize - bmpH) // 2
                dc.DrawBitmap(tile.bitmap, x, y, True)
            else:
                # A placeholder box, so the grid has its final shape before
                # any decode finishes and scrolling does not reflow.
                dc.SetBrush(wx.Brush(wx.Colour(245, 245, 245)))
                dc.SetPen(wx.Pen(wx.Colour(225, 225, 225)))
                box = wx.Rect(rect.x + _kTilePadding,
                              rect.y + _kTilePadding,
                              self._thumbSize, self._thumbSize)
                dc.DrawRectangle(box)
                dc.SetTextForeground(dimColour)
                msg = "?" if tile.state == _kFailed else "..."
                tw, th = dc.GetTextExtent(msg)
                dc.DrawText(msg, box.x + (box.width - tw) // 2,
                            box.y + (box.height - th) // 2)

            if tile.isVideo:
                self._drawVideoBadge(dc, rect)

            caption = os.path.basename(tile.path)
            dc.SetTextForeground(textColour)
            caption = self._elide(dc, caption, rect.width - 6)
            tw, _th = dc.GetTextExtent(caption)
            dc.DrawText(caption, rect.x + (rect.width - tw) // 2,
                        rect.y + _kTilePadding + self._thumbSize + 2)


    ###########################################################
    def _drawVideoBadge(self, dc, rect):
        """Mark a tile as a video.

        @param  dc    The device context.
        @param  rect  The tile rect.
        """
        size = 14
        x = rect.x + _kTilePadding + 3
        y = rect.y + _kTilePadding + self._thumbSize - size - 3
        drawVideoBadge(dc, x, y)


    ###########################################################
    def _elide(self, dc, text, maxWidth):
        """Shorten a caption to fit, with an ellipsis.

        @param  dc        The device context, for measuring.
        @param  text      The caption.
        @param  maxWidth  Available width in pixels.
        @return str
        """
        if dc.GetTextExtent(text)[0] <= maxWidth:
            return text
        ellipsis = "…"
        while text and dc.GetTextExtent(text + ellipsis)[0] > maxWidth:
            text = text[:-1]
        return text + ellipsis


    ###########################################################
    def OnSize(self, event):
        """Recompute columns when the pane is resized.

        @param  event  The EVT_SIZE event.
        """
        event.Skip()
        self._relayout()
        self.Refresh()
        self._requestVisible()


    ###########################################################
    def OnScrollWin(self, event):
        """Decode whatever scrolled into view.

        @param  event  The EVT_SCROLLWIN event.
        """
        event.Skip()
        # After the scroll has actually happened, not before.
        wx.CallAfter(self._requestVisible)


    # -- selection ---------------------------------------------------------

    ###########################################################
    def _hitTest(self, x, y):
        """Which tile is at a client point.

        @param  x  Client x.
        @param  y  Client y.
        @return    Tile index, or -1.
        """
        vx, vy = self.CalcUnscrolledPosition(x, y)
        tileW, tileH = self._tileSize()
        col = vx // tileW
        row = vy // tileH
        if col < 0 or col >= self._cols or row < 0:
            return -1
        index = int(row * self._cols + col)
        if 0 <= index < len(self._tiles):
            return index
        return -1


    ###########################################################
    def OnLeftDown(self, event):
        """Select the tile that was clicked.

        @param  event  The EVT_LEFT_DOWN event.
        """
        event.Skip()
        self.SetFocus()
        index = self._hitTest(event.GetX(), event.GetY())
        # A click on empty space past the last tile must not fire a selection
        # event -- with lazy analysis, a stray event is a stray detection run.
        if index < 0:
            return
        if event.ShiftDown() and self._anchor >= 0:
            # Shift: the range from the anchor; Ctrl+Shift adds the range.
            low, high = sorted((self._anchor, index))
            span = set(range(low, high + 1))
            marked = (self._marked | span) if event.ControlDown() else span
            self._setSelection(index, keepAnchor=True)
            self._marked = marked
            self.Refresh()
        elif event.ControlDown():
            # Ctrl: add or remove this tile, keeping the rest.
            marked = set(self.getSelectedIndices())
            marked ^= {index}
            if marked:
                self._setSelection(index if index in marked else max(marked))
                self._marked = marked
                self._anchor = index
                self.Refresh()
        else:
            self._setSelection(index)


    def OnLeftDClick(self, event):
        """Ask the parent to open the double-clicked tile large.

        @param  event  The EVT_LEFT_DCLICK event.
        """
        index = self._hitTest(event.GetX(), event.GetY())
        if index < 0:
            return
        self._setSelection(index)
        activated = ThumbSelectedEvent(myEVT_THUMB_ACTIVATED, self.GetId(),
                                       self._tiles[index].path)
        activated.SetEventObject(self)
        self.GetEventHandler().ProcessEvent(activated)


    def getSelectedIndices(self):
        """@return  Indices of every highlighted tile, sorted."""
        indices = set(self._marked)
        if self._selection >= 0:
            indices.add(self._selection)
        return sorted(indices)


    def selectAtPosition(self, position):
        """Select a context-clicked tile, ignoring blank space.

        A right-click inside a multiple selection keeps it, so the menu acts
        on all of it; anywhere else selects just that tile.
        """
        index = self._hitTest(position.x, position.y)
        if index < 0:
            return False
        self.SetFocus()
        if index not in self.getSelectedIndices():
            self._setSelection(index)
        return True


    ###########################################################
    def OnKeyDown(self, event):
        """Arrow-key navigation.

        Deliberately no WXK_DELETE handling.  The control this replaces routed
        Delete to os.remove on the user's original file; nothing here is
        allowed to delete anything.

        @param  event  The EVT_KEY_DOWN event.
        """
        if not self._tiles:
            event.Skip()
            return

        key = event.GetKeyCode()
        if key == ord('A') and event.ControlDown():
            self.selectAll()
            return
        current = self._selection if self._selection >= 0 else 0
        step = {wx.WXK_LEFT: -1, wx.WXK_RIGHT: 1,
                wx.WXK_UP: -self._cols, wx.WXK_DOWN: self._cols}.get(key)

        if step is None:
            if key == wx.WXK_HOME:
                self._setSelection(0)
            elif key == wx.WXK_END:
                self._setSelection(len(self._tiles) - 1)
            else:
                event.Skip()
            return

        target = current + step
        if 0 <= target < len(self._tiles):
            self._setSelection(target)


    ###########################################################
    def _setSelection(self, index, notify=True, keepAnchor=False):
        """Select a tile, scroll it into view and tell the parent.

        @param  index   Tile index.
        @param  notify      False to select without firing the event.
        @param  keepAnchor  True to keep where a Shift+click range starts.
        """
        # Selecting one tile ends any multiple selection.
        self._marked = {index}
        if not keepAnchor:
            self._anchor = index
        if index == self._selection:
            self.Refresh()
            return
        self._selection = index
        self._scrollIntoView(index)
        self.Refresh()
        if notify:
            event = ThumbSelectedEvent(myEVT_THUMB_SELECTED, self.GetId(),
                                       self.getSelection())
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)


    ###########################################################
    def _scrollIntoView(self, index):
        """Make sure a tile is visible.

        @param  index  Tile index.
        """
        if not (0 <= index < len(self._tiles)):
            return
        _, tileH = self._tileSize()
        _, unitY = self.GetScrollPixelsPerUnit()
        if unitY <= 0:
            return
        rect = self._tileRect(index)
        _, scrollY = self.GetViewStart()
        top = scrollY * unitY
        height = self.GetClientSize().height

        if rect.y < top:
            self.Scroll(0, rect.y // unitY)
        elif rect.y + rect.height > top + height:
            self.Scroll(0, max(0, (rect.y + rect.height - height) // unitY))
        self._requestVisible()


    # -- decoding ----------------------------------------------------------

    ###########################################################
    def _requestVisible(self):
        """Queue decodes for what is on screen, and evict what is far away."""
        if not self._tiles:
            return
        first, last = self._visibleRange(_kPrefetchRows)

        for index in range(first, last + 1):
            tile = self._tiles[index]
            if tile.bitmap is None and tile.state == _kPending:
                with self._queuedLock:
                    if index in self._queued:
                        continue
                    self._queued.add(index)
                self._ensureWorker()
                self._queue.put((index, tile.path, tile.isVideo,
                                 self._thumbSize))

        self._evict(keep=(first, last))


    ###########################################################
    def _evict(self, keep):
        """Drop bitmaps beyond the memory cap, furthest from view first.

        This is the bound the shipped control does not have: without it, a
        browse through a large folder retains every image it ever decoded.

        @param  keep  (first, last) visible range to protect.
        """
        if len(self._live) <= _kMaxLiveBitmaps:
            return
        first, last = keep
        # Oldest first, but never drop something currently on screen.
        survivors = []
        for index in self._live:
            if len(self._live) - len(survivors) <= _kMaxLiveBitmaps:
                survivors.append(index)
                continue
            if first <= index <= last:
                survivors.append(index)
                continue
            tile = self._tiles[index] if index < len(self._tiles) else None
            if tile is not None:
                tile.bitmap = None
                tile.state = _kPending
        self._live = survivors


    ###########################################################
    def _ensureWorker(self):
        """Start the decode thread if it is not running."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopEvent.clear()
        self._worker = threading.Thread(target=self._workerLoop,
                                        name="ImageThumbGrid",
                                        daemon=True)
        self._worker.start()


    ###########################################################
    def _drainQueue(self):
        """Throw away pending work."""
        with self._queuedLock:
            self._queued.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


    ###########################################################
    def _workerLoop(self):
        """Decode queued thumbnails until told to stop.  Off the UI thread."""
        while not self._stopEvent.is_set():
            try:
                index, path, isVideo, size = self._queue.get(
                    timeout=_kWorkerPollSecs)
            except queue.Empty:
                continue
            if self._stopEvent.is_set():
                break
            try:
                data = self._makeThumbnail(path, isVideo, size)
            except Exception:
                self._logger.info("ImageThumbGrid: thumbnail failed for "
                                  "%s: %s" % (path, traceback.format_exc()))
                data = None
            self._post(index, path, size, data)


    ###########################################################
    def _makeThumbnail(self, path, isVideo, size):
        """Produce thumbnail bytes for one file, using the disk cache.

        @param  path     Absolute path.
        @param  isVideo  True for a video.
        @param  size     Thumbnail size in pixels.
        @return          Raw JPEG/PNG bytes, or None.
        """
        name, stat = cacheKey(path, size)
        cached = findThumb(name)
        if cached is not None:
            try:
                with open(cached, "rb") as f:
                    return f.read()
            except OSError:
                # Removed by the cleanup pass between the check and the open;
                # just make it again.
                pass

        cachePath = shardPath(name, create=True)
        if isVideo:
            self._extractVideoFrame(path, cachePath, size)
        else:
            self._shrinkImage(path, cachePath, size)

        if os.path.isfile(cachePath):
            recordThumb(name, path, stat, size, self._logger)
            with open(cachePath, "rb") as f:
                return f.read()
        return None


    ###########################################################
    def _shrinkImage(self, path, cachePath, size):
        """Decode a still small and write it to the cache.

        @param  path       Source file.
        @param  cachePath  Where to write the thumbnail.
        @param  size       Target size in pixels.
        """
        from PIL import Image
        with Image.open(path) as img:
            # draft() downscales DURING decode for JPEG, so a 12 MP source
            # never becomes a 36 MB buffer on the way to a 128 px tile.
            img.draft('RGB', (size, size))
            img = img.convert('RGB')
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            tmp = cachePath + ".tmp"
            img.save(tmp, "JPEG", quality=82)
        os.replace(tmp, cachePath)


    ###########################################################
    def _extractVideoFrame(self, path, cachePath, size):
        """Pull one frame out of a video with the bundled ffmpeg.

        @param  path       Source file.
        @param  cachePath  Where to write the thumbnail.
        @param  size       Target size in pixels.
        """
        from appCommon.InstallPaths import getFfmpegExe
        tmp = cachePath + ".tmp.jpg"
        # -ss before -i seeks without decoding everything up to that point.
        # 1 second in rather than frame 0: the first frame of a phone video is
        # very often black.
        cmd = [getFfmpegExe(), "-nostdin", "-loglevel", "error",
               "-ss", "1", "-i", path, "-frames:v", "1",
               "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease"
                      % (size, size),
               "-y", tmp]
        try:
            subprocess.run(cmd, timeout=20, creationflags=_kNoWindow,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            # A video shorter than the seek, or one ffmpeg cannot open.  Try
            # the very first frame before giving up.
            try:
                cmd[cmd.index("-ss") + 1] = "0"
                subprocess.run(cmd, timeout=20, creationflags=_kNoWindow,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except Exception:
                return
        if os.path.isfile(tmp):
            os.replace(tmp, cachePath)


    ###########################################################
    def _post(self, index, path, size, data):
        """Hand a finished thumbnail back to the UI thread.

        @param  index  Tile index the work was queued for.
        @param  path   The path it was for.
        @param  size   The thumbnail size it was made at.
        @param  data   Encoded image bytes, or None on failure.
        """
        def apply():
            # `not self` is the wx test for a window whose C++ side is gone.
            # Without it a thumbnail arriving after the view closed is a call
            # into freed memory.
            if not self:
                return
            with self._queuedLock:
                self._queued.discard(index)
            # The folder may have changed while this was decoding; only accept
            # the result if the tile is still the same file at the same size.
            if not (0 <= index < len(self._tiles)):
                return
            tile = self._tiles[index]
            if tile.path != path or size != self._thumbSize:
                return

            if data is None:
                tile.state = _kFailed
            else:
                bitmap = self._decodeBitmap(data)
                if bitmap is None:
                    tile.state = _kFailed
                else:
                    tile.bitmap = bitmap
                    tile.state = _kReady
                    self._live.append(index)
            self.RefreshRect(self._tileRectOnScreen(index))
            if index == self._selection:
                event = ThumbSelectedEvent(myEVT_THUMB_READY, self.GetId(), path)
                wx.PostEvent(self, event)
        wx.CallAfter(apply)


    ###########################################################
    def _decodeBitmap(self, data):
        """Turn encoded bytes into a wx.Bitmap.

        @param  data  Encoded image bytes.
        @return       A wx.Bitmap, or None.
        """
        import io
        try:
            image = wx.Image(io.BytesIO(data))
            if not image.IsOk():
                return None
            return wx.Bitmap(image)
        except Exception:
            return None


    ###########################################################
    def _tileRectOnScreen(self, index):
        """A tile's rect in client coordinates, for a targeted repaint.

        @param  index  Tile index.
        @return wx.Rect
        """
        rect = self._tileRect(index)
        x, y = self.CalcScrolledPosition(rect.x, rect.y)
        return wx.Rect(x, y, rect.width, rect.height)


    ###########################################################
    def _onDestroy(self, event):
        """Stop the worker before the window goes away.

        @param  event  The EVT_WINDOW_DESTROY event.
        """
        if event.GetEventObject() == self:
            self.stop()
        event.Skip()
