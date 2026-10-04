#!/usr/bin/env python

#*****************************************************************************
#
# AppColors.py
#   The user-selectable background colour for the main window, plus the
#   contrast maths needed to keep text readable on top of it.
#
#*****************************************************************************

import wx


# Deliberately dependency-free: GradientPanelWin (this package) reads the
# current colour from here, so this module must not import anything from
# frontEnd.  The front end PUSHES the saved preference down at startup via
# setAppBackgroundColour(), and again whenever the user changes it.
_appBackground = None       # (r, g, b), or None to mean "system default"


# Below this contrast ratio against the background, text gets replaced.
#
# 4.5:1 is the WCAG AA floor for body-sized text, which is what these labels
# are -- the more permissive 3:1 is for large text, and at that setting the
# app's accent blue survived on a near-black background at 3.35:1, which is
# not comfortably readable.  Checked against the real palette: at 4.5 the
# accent blue and the grey hint text are both left untouched on the default
# light background (both ~5.1:1) and both get rescued on dark ones.
_kMinContrastRatio = 4.5

# Not pure black/white: softer ends look less harsh and match the existing
# near-black label text.
_kDarkText  = (20, 20, 20)
_kLightText = (240, 240, 240)


###############################################################
def systemBackgroundColour():
    """@return  wx.Colour  The OS default the app has always used."""
    return wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DFACE)


###############################################################
def setAppBackgroundColour(rgb):
    """Set the app-wide background.

    @param  rgb  (r, g, b) tuple, a wx.Colour, or None to mean the system
                 default.
    """
    global _appBackground
    if rgb is None:
        _appBackground = None
    elif isinstance(rgb, wx.Colour):
        _appBackground = (rgb.Red(), rgb.Green(), rgb.Blue())
    else:
        _appBackground = tuple(int(c) for c in rgb[:3])


###############################################################
def getAppBackgroundColour():
    """@return  wx.Colour  The current background; the system default until
                           the user picks something."""
    if _appBackground is None:
        return systemBackgroundColour()
    return wx.Colour(*_appBackground)


###############################################################
def isCustomBackground():
    """@return  True if the user has chosen a colour of their own."""
    return _appBackground is not None


###############################################################
def _channels(colour):
    """Accept a wx.Colour or an (r, g, b) sequence -> (r, g, b) ints."""
    if isinstance(colour, wx.Colour):
        return colour.Red(), colour.Green(), colour.Blue()
    return tuple(int(c) for c in colour[:3])


###############################################################
def relativeLuminance(colour):
    """Perceived brightness of a colour, 0.0 (black) to 1.0 (white).

    The sRGB formula from WCAG 2.x -- the gamma expansion matters: a naive
    (r+g+b)/3 average rates mid-blue about as bright as mid-yellow, which is
    exactly the mistake that produces unreadable UIs.

    @param  colour  wx.Colour or (r, g, b).
    @return lum     0.0 .. 1.0
    """
    out = []
    for c in _channels(colour):
        c = c / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


###############################################################
def contrastRatio(a, b):
    """Contrast between two colours, 1.0 (identical) to 21.0 (black/white).

    @param  a, b   wx.Colour or (r, g, b).
    @return ratio  1.0 .. 21.0
    """
    la = relativeLuminance(a)
    lb = relativeLuminance(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


###############################################################
def pickForeground(background):
    """Choose readable text for a background.

    @param  background  wx.Colour or (r, g, b).
    @return colour      wx.Colour, dark or light, whichever contrasts more.
    """
    if contrastRatio(background, _kDarkText) >= \
            contrastRatio(background, _kLightText):
        return wx.Colour(*_kDarkText)
    return wx.Colour(*_kLightText)


###############################################################
def needsContrastRepair(foreground, background):
    """@return True if this text would be hard to read on this background."""
    return contrastRatio(foreground, background) < _kMinContrastRatio


###############################################################
def applyToTree(window, background=None, foreground=None):
    """Recolour a window and everything inside it.

    Backgrounds cascade to children at CREATION time in wx, so changing a
    parent afterwards does not reach the children that already exist -- hence
    the walk (same recursion as vitaToolbox.wx.BindChildren).

    Text is handled by repair, not by blanket overwrite: a label is only
    recoloured when its CURRENT colour would be unreadable on the new
    background.  That keeps intentional accents (face-name blue, warning
    orange, health-status green/red) exactly as their authors set them, while
    rescuing the many grey hint labels when the user picks something dark.

    @param  window      Root of the tree to restyle.
    @param  background  wx.Colour to apply; defaults to the app colour.
    @param  foreground  Replacement text colour; defaults to the readable
                        choice for the background.
    @return count       (windowsRecoloured, labelsRepaired) -- returned so
                        tests and callers can see it actually did something.
    """
    if window is None:
        return (0, 0)
    if background is None:
        background = getAppBackgroundColour()
    if foreground is None:
        foreground = pickForeground(background)
    return _applyToTree(window, background, foreground)


###############################################################
def _applyToTree(window, background, foreground):
    recoloured = 0
    repaired = 0

    if _wantsBackground(window):
        try:
            window.SetBackgroundColour(background)
            recoloured += 1
        except Exception:
            pass

    if isinstance(window, wx.StaticText):
        try:
            if needsContrastRepair(window.GetForegroundColour(), background):
                window.SetForegroundColour(foreground)
                repaired += 1
        except Exception:
            pass

    try:
        children = window.GetChildren()
    except Exception:
        children = []
    for child in children:
        c, r = _applyToTree(child, background, foreground)
        recoloured += c
        repaired += r

    return (recoloured, repaired)


###############################################################
def _wantsBackground(window):
    """Should this window follow the app background?

    Skips the surfaces that carry a meaning of their own: video canvases are
    black so letterboxing reads as empty space, and the trial/legacy banners
    are a fixed brand navy.
    """
    # Opt-out hook for anything that needs to keep its own colour.
    if getattr(window, 'svKeepOwnBackground', False):
        return False

    # Imported lazily: this module is imported by GradientPanel, which these
    # would drag into a cycle at load time.
    try:
        from vitaToolbox.wx.BitmapWindow import BitmapWindow
        if isinstance(window, BitmapWindow):
            return False
    except Exception:
        pass

    # GL canvases paint every pixel themselves; setting a background on them
    # is at best pointless and at worst upsets the context.
    try:
        import wx.glcanvas
        if isinstance(window, wx.glcanvas.GLCanvas):
            return False
    except Exception:
        pass

    return True
