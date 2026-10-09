#! /usr/local/bin/python

"""
## @file
One image shown as large as the Images tab's centre panel allows.

Opened by double-clicking a thumbnail.  The mouse wheel steps through the
listing (down = next, up = previous), Home and End jump to its first and last
file, Delete asks to delete the file, and Escape goes back to the thumbnails.  The view only reports those requests;
ImageView decides what they do.
"""

# Python imports...
import os

# Common 3rd-party imports...
import wx

# Local imports...


# Decode at most this many times the panel size, so a resize can rescale the
# held copy instead of decoding the file again.
_kDecodeHeadroom = 2


##############################################################################
def fitSize(imageSize, boxSize):
    """Largest size with the image's aspect ratio that fits in a box.

    @param  imageSize  (w, h) of the image.
    @param  boxSize    (w, h) available.
    @return (w, h)     At least 1x1.
    """
    iw, ih = imageSize
    bw, bh = boxSize
    if iw <= 0 or ih <= 0 or bw <= 0 or bh <= 0:
        return (1, 1)
    scale = min(bw / float(iw), bh / float(ih))
    return (max(1, int(iw * scale)), max(1, int(ih * scale)))


##############################################################################
def loadImage(path, maxSize):
    """Decode a photo upright and no larger than needed.

    @param  path     Absolute path.
    @param  maxSize  (w, h) the result need not exceed.
    @return          An RGB PIL image, or None if it cannot be read.
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as img:
            # draft() lets the JPEG decoder skip straight to a reduced scale.
            img.draft('RGB', maxSize)
            img = ImageOps.exif_transpose(img).convert('RGB')
            img.thumbnail(maxSize)
            return img
    except Exception:
        return None


##############################################################################
class ImageLargeView(wx.Panel):
    """Paints one image, letterboxed, and reports wheel/Delete/Escape."""

    ###########################################################
    def __init__(self, parent, onStep, onDelete, onClose):
        """Initializer for ImageLargeView.

        @param  parent    The parent window.
        @param  onStep    f(+1 or -1) -- show the next or previous file.
                          f(None, first) -- Home/End: the first (True) or
                          last (False) file in the listing.
        @param  onDelete  f() -- delete the file being shown.
        @param  onClose   f() -- return to the thumbnails.
        """
        super(ImageLargeView, self).__init__(parent, -1,
                                             style=wx.WANTS_CHARS)
        self._onStep = onStep
        self._onDelete = onDelete
        self._onClose = onClose
        self._path = None
        self._image = None
        self._fallback = None
        self._caption = ''
        self._scaled = (None, None)

        # Paints every pixel it owns; see ImageThumbGrid.
        self.svKeepOwnBackground = True
        self.SetBackgroundColour(wx.Colour(32, 32, 32))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)

        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_SIZE, self.OnSize)
        self.Bind(wx.EVT_MOUSEWHEEL, self.OnMouseWheel)
        self.Bind(wx.EVT_KEY_DOWN, self.OnKeyDown)
        self.Bind(wx.EVT_LEFT_DOWN, lambda event: self.SetFocus())


    ###########################################################
    def getPath(self):
        """@return  The file being shown, or None."""
        return self._path


    ###########################################################
    def setFile(self, path, caption='', fallbackBitmap=None):
        """Show a file.

        @param  path            Absolute path.
        @param  caption         Text drawn under the image, e.g. "3 of 40".
        @param  fallbackBitmap  Shown scaled when the file is not a decodable
                                still -- a video's thumbnail, typically.
        """
        self._path = path
        self._caption = caption
        self._fallback = fallbackBitmap
        self._image = None
        self._scaled = (None, None)
        if path:
            w, h = self.GetClientSize()
            self._image = loadImage(path, (max(1, w) * _kDecodeHeadroom,
                                           max(1, h) * _kDecodeHeadroom))
        self.Refresh()


    ###########################################################
    def OnSize(self, event):
        """Repaint at the new size."""
        event.Skip()
        self.Refresh()


    ###########################################################
    def OnMouseWheel(self, event):
        """Wheel down shows the next file, wheel up the previous one."""
        rotation = event.GetWheelRotation()
        if rotation:
            self._onStep(1 if rotation < 0 else -1)


    ###########################################################
    def OnKeyDown(self, event):
        """Escape, Delete, Home/End, and the arrows/page keys as the wheel."""
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self._onClose()
        elif key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            self._onDelete()
        elif key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME):
            self._onStep(None, True)
        elif key in (wx.WXK_END, wx.WXK_NUMPAD_END):
            self._onStep(None, False)
        elif key in (wx.WXK_RIGHT, wx.WXK_DOWN, wx.WXK_PAGEDOWN):
            self._onStep(1)
        elif key in (wx.WXK_LEFT, wx.WXK_UP, wx.WXK_PAGEUP):
            self._onStep(-1)
        else:
            event.Skip()


    ###########################################################
    def OnPaint(self, event):
        """Letterbox the image into the panel, caption underneath."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        w, h = self.GetClientSize()
        captionH = dc.GetTextExtent('Ag')[1] + 8 if self._caption else 0
        box = (w, max(1, h - captionH))

        bitmap = None
        if self._image is not None:
            size = fitSize(self._image.size, box)
            if self._scaled[0] != size:
                img = self._image.resize(size) if size != self._image.size else self._image
                self._scaled = (size, wx.Bitmap.FromBuffer(size[0], size[1], img.tobytes()))
            bitmap = self._scaled[1]
        elif self._fallback is not None and self._fallback.IsOk():
            fw, fh = self._fallback.GetWidth(), self._fallback.GetHeight()
            size = fitSize((fw, fh), box)
            bitmap = wx.Bitmap(self._fallback.ConvertToImage().Scale(
                size[0], size[1], wx.IMAGE_QUALITY_HIGH))

        dc.SetTextForeground(wx.Colour(230, 230, 230))
        if bitmap is not None:
            dc.DrawBitmap(bitmap, (w - bitmap.GetWidth()) // 2,
                          (box[1] - bitmap.GetHeight()) // 2)
        elif self._path:
            message = 'Cannot display %s' % os.path.basename(self._path)
            tw, th = dc.GetTextExtent(message)
            dc.DrawText(message, (w - tw) // 2, (box[1] - th) // 2)
        if self._caption:
            tw, th = dc.GetTextExtent(self._caption)
            dc.DrawText(self._caption, (w - tw) // 2, h - captionH + 4)
