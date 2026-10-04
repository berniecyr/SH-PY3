#!/usr/bin/env python

#*****************************************************************************
#
# ScheduleLocationPicker.py
#
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

"""
Utilities for schedule solar-time location selection.

Two entry points for schedule dialogs:
  - startLocationAutoDetect(onFound, onFailed)  — background IP geolocation
  - LocationPickerDialog                         — offline city list picker
  - schedAutoDetectLocation(...)                 — convenience wrapper used by dialogs
  - schedOnPickCity(...)                         — convenience wrapper used by dialogs
"""

import json
import threading
import urllib.request

import wx


# ---------------------------------------------------------------------------
# Major world cities sorted alphabetically.
# Each entry: (display_name, latitude_decimal, longitude_decimal)
# ---------------------------------------------------------------------------
_kMajorCities = sorted([
    # North America
    ("Anchorage, USA",           61.22, -149.90),
    ("Atlanta, USA",             33.75,  -84.39),
    ("Boston, USA",              42.36,  -71.06),
    ("Calgary, Canada",          51.05, -114.07),
    ("Chicago, USA",             41.88,  -87.63),
    ("Dallas, USA",              32.78,  -96.80),
    ("Denver, USA",              39.74, -104.98),
    ("Honolulu, USA",            21.31, -157.86),
    ("Houston, USA",             29.76,  -95.37),
    ("Las Vegas, USA",           36.17, -115.14),
    ("Los Angeles, USA",         34.05, -118.24),
    ("Miami, USA",               25.77,  -80.19),
    ("Mexico City, Mexico",      19.43,  -99.13),
    ("Minneapolis, USA",         44.98,  -93.27),
    ("Montreal, Canada",         45.50,  -73.57),
    ("New York, USA",            40.71,  -74.01),
    ("Ottawa, Canada",           45.42,  -75.69),
    ("Phoenix, USA",             33.45, -112.07),
    ("Portland, USA",            45.52, -122.68),
    ("San Francisco, USA",       37.77, -122.42),
    ("Seattle, USA",             47.61, -122.33),
    ("Toronto, Canada",          43.65,  -79.38),
    ("Vancouver, Canada",        49.25, -123.12),
    ("Washington DC, USA",       38.91,  -77.04),
    # South America
    ("Bogota, Colombia",          4.71,  -74.07),
    ("Buenos Aires, Argentina", -34.60,  -58.38),
    ("Caracas, Venezuela",       10.48,  -66.88),
    ("Lima, Peru",              -12.05,  -77.04),
    ("Montevideo, Uruguay",     -34.90,  -56.19),
    ("Santiago, Chile",         -33.45,  -70.67),
    ("Sao Paulo, Brazil",       -23.55,  -46.63),
    # Europe
    ("Amsterdam, Netherlands",   52.37,    4.90),
    ("Athens, Greece",           37.98,   23.73),
    ("Barcelona, Spain",         41.39,    2.15),
    ("Berlin, Germany",          52.52,   13.40),
    ("Brussels, Belgium",        50.85,    4.35),
    ("Budapest, Hungary",        47.50,   19.04),
    ("Copenhagen, Denmark",      55.68,   12.57),
    ("Dublin, Ireland",          53.33,   -6.25),
    ("Edinburgh, UK",            55.95,   -3.19),
    ("Helsinki, Finland",        60.17,   24.93),
    ("Istanbul, Turkey",         41.01,   28.95),
    ("Kiev, Ukraine",            50.45,   30.52),
    ("Lisbon, Portugal",         38.72,   -9.14),
    ("London, UK",               51.51,   -0.13),
    ("Madrid, Spain",            40.42,   -3.70),
    ("Milan, Italy",             45.47,    9.19),
    ("Moscow, Russia",           55.75,   37.62),
    ("Oslo, Norway",             59.91,   10.75),
    ("Paris, France",            48.85,    2.35),
    ("Prague, Czech Republic",   50.09,   14.42),
    ("Reykjavik, Iceland",       64.13,  -21.94),
    ("Rome, Italy",              41.90,   12.49),
    ("Stockholm, Sweden",        59.33,   18.07),
    ("Vienna, Austria",          48.21,   16.37),
    ("Warsaw, Poland",           52.23,   21.01),
    ("Zurich, Switzerland",      47.38,    8.54),
    # Africa
    ("Addis Ababa, Ethiopia",     9.03,   38.74),
    ("Cairo, Egypt",             30.04,   31.24),
    ("Cape Town, South Africa", -33.93,   18.42),
    ("Casablanca, Morocco",      33.59,   -7.62),
    ("Dar es Salaam, Tanzania",  -6.80,   39.28),
    ("Johannesburg, South Africa",-26.20, 28.04),
    ("Lagos, Nigeria",            6.45,    3.39),
    ("Nairobi, Kenya",           -1.29,   36.82),
    ("Tunis, Tunisia",           36.82,   10.17),
    # Middle East
    ("Amman, Jordan",            31.96,   35.95),
    ("Baghdad, Iraq",            33.34,   44.40),
    ("Beirut, Lebanon",          33.89,   35.50),
    ("Dubai, UAE",               25.20,   55.27),
    ("Riyadh, Saudi Arabia",     24.69,   46.72),
    ("Tehran, Iran",             35.69,   51.39),
    ("Tel Aviv, Israel",         32.08,   34.78),
    # Asia
    ("Almaty, Kazakhstan",       43.23,   76.85),
    ("Bangkok, Thailand",        13.75,  100.52),
    ("Beijing, China",           39.91,  116.39),
    ("Chennai, India",           13.08,   80.27),
    ("Delhi, India",             28.67,   77.22),
    ("Dhaka, Bangladesh",        23.72,   90.41),
    ("Guangzhou, China",         23.12,  113.25),
    ("Hanoi, Vietnam",           21.03,  105.85),
    ("Hong Kong",                22.32,  114.17),
    ("Jakarta, Indonesia",       -6.21,  106.85),
    ("Karachi, Pakistan",        24.86,   67.01),
    ("Kuala Lumpur, Malaysia",    3.14,  101.69),
    ("Manila, Philippines",      14.60,  120.98),
    ("Mumbai, India",            19.08,   72.88),
    ("Osaka, Japan",             34.69,  135.50),
    ("Seoul, South Korea",       37.57,  126.98),
    ("Shanghai, China",          31.23,  121.47),
    ("Singapore",                 1.35,  103.82),
    ("Taipei, Taiwan",           25.04,  121.51),
    ("Tashkent, Uzbekistan",     41.30,   69.27),
    ("Tokyo, Japan",             35.69,  139.69),
    ("Ulaanbaatar, Mongolia",    47.90,  106.92),
    ("Yangon, Myanmar",          16.87,   96.15),
    # Oceania
    ("Adelaide, Australia",     -34.93,  138.60),
    ("Auckland, New Zealand",   -36.86,  174.77),
    ("Brisbane, Australia",     -27.47,  153.03),
    ("Christchurch, N. Zealand",-43.53,  172.64),
    ("Melbourne, Australia",    -37.81,  144.96),
    ("Perth, Australia",        -31.95,  115.86),
    ("Sydney, Australia",       -33.87,  151.21),
], key=lambda c: c[0])


# ---------------------------------------------------------------------------
# Background IP geolocation
# ---------------------------------------------------------------------------

def startLocationAutoDetect(onFound, onFailed=None):
    """Start a background thread to detect lat/lon from the machine's public IP.

    The result is delivered on the wx main thread via wx.CallAfter.
    Only configuration-time internet access is required; runtime solar
    calculations use the stored coordinates with no network access.

    @param  onFound   Callable(lat, lon) invoked on main thread on success.
    @param  onFailed  Optional callable() invoked on main thread on failure.
    """
    def _worker():
        try:
            req = urllib.request.Request(
                'https://ipapi.co/json/',
                headers={'User-Agent': 'SighthoundVideo/1.0'}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            lat = data.get('latitude')
            lon = data.get('longitude')
            if lat is not None and lon is not None:
                wx.CallAfter(onFound, float(lat), float(lon))
                return
        except Exception:
            pass
        if onFailed is not None:
            wx.CallAfter(onFailed)

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Convenience wrappers used by schedule dialogs
# ---------------------------------------------------------------------------

def schedAutoDetectLocation(latCtrl, lonCtrl, hintLabel, dialog):
    """Trigger IP location auto-detection if lat/lon fields are currently empty.

    Shows feedback in hintLabel and populates the controls on the main thread.
    Uses dialog._schedFetching to prevent overlapping fetches.

    @param  latCtrl    wx.TextCtrl for latitude.
    @param  lonCtrl    wx.TextCtrl for longitude.
    @param  hintLabel  wx.StaticText used for status/hint feedback.
    @param  dialog     Host dialog; _schedFetching attribute stored here.
    """
    if getattr(dialog, '_schedFetching', False):
        return
    dialog._schedFetching = True
    hintLabel.SetLabel("Detecting location from IP...")

    def _onFound(lat, lon):
        dialog._schedFetching = False
        if not latCtrl.GetValue().strip() and not lonCtrl.GetValue().strip():
            latCtrl.SetValue('%.4f' % lat)
            lonCtrl.SetValue('%.4f' % lon)
            hintLabel.SetLabel("Auto-detected from IP — adjust if needed")
        else:
            hintLabel.SetLabel("(decimal degrees, e.g. 45.4, -75.7)")

    def _onFailed():
        dialog._schedFetching = False
        hintLabel.SetLabel("Could not detect — enter manually or pick a city")

    startLocationAutoDetect(_onFound, _onFailed)


def schedOnPickCity(parent, latCtrl, lonCtrl, hintLabel):
    """Open LocationPickerDialog; populate controls on OK.

    @param  parent     wx parent for the dialog.
    @param  latCtrl    wx.TextCtrl for latitude.
    @param  lonCtrl    wx.TextCtrl for longitude.
    @param  hintLabel  wx.StaticText for status/hint feedback.
    """
    dlg = LocationPickerDialog(parent)
    try:
        if dlg.ShowModal() == wx.ID_OK:
            lat, lon = dlg.getSelection()
            latCtrl.SetValue('%.4f' % lat)
            lonCtrl.SetValue('%.4f' % lon)
            hintLabel.SetLabel("Location set from city list — adjust if needed")
    finally:
        dlg.Destroy()


# ---------------------------------------------------------------------------
# City picker dialog
# ---------------------------------------------------------------------------

class LocationPickerDialog(wx.Dialog):
    """Dialog for selecting a geographic location from a pre-built city list.

    Works completely offline; no internet connection is required.
    """

    ###########################################################
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, -1, "Pick Location",
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            self._selection = None
            self._filteredCities = list(_kMajorCities)
            self._doInit()
        except:
            self.Destroy()
            raise


    ###########################################################
    def _doInit(self):
        filterLabel = wx.StaticText(self, -1, "Filter:")
        self._filterCtrl = wx.TextCtrl(self, -1, "")
        filterRow = wx.BoxSizer(wx.HORIZONTAL)
        filterRow.Add(filterLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        filterRow.Add(self._filterCtrl, 1, wx.EXPAND)

        self._listBox = wx.ListBox(self, -1, size=(340, 300),
                                   choices=[c[0] for c in _kMajorCities],
                                   style=wx.LB_SINGLE)
        self._listBox.SetSelection(0)

        self._coordLabel = wx.StaticText(self, -1, "")
        font = self._coordLabel.GetFont()
        font.MakeBold()
        self._coordLabel.SetFont(font)
        self._refreshCoordLabel()

        buttonSizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK, self).Bind(wx.EVT_BUTTON, self._onOK)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(filterRow, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self._listBox, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self._coordLabel, 0, wx.ALL, 8)
        sizer.Add(buttonSizer, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self._filterCtrl.Bind(wx.EVT_TEXT, self._onFilter)
        self._listBox.Bind(wx.EVT_LISTBOX, self._onListSelect)

        self.Fit()
        self.CenterOnParent()
        self._filterCtrl.SetFocus()


    ###########################################################
    def _onFilter(self, event):
        term = self._filterCtrl.GetValue().strip().lower()
        if term:
            self._filteredCities = [c for c in _kMajorCities
                                     if term in c[0].lower()]
        else:
            self._filteredCities = list(_kMajorCities)
        self._listBox.Set([c[0] for c in self._filteredCities])
        if self._filteredCities:
            self._listBox.SetSelection(0)
        self._refreshCoordLabel()


    ###########################################################
    def _onListSelect(self, event):
        self._refreshCoordLabel()


    ###########################################################
    def _refreshCoordLabel(self):
        idx = self._listBox.GetSelection()
        if idx != wx.NOT_FOUND and idx < len(self._filteredCities):
            _, lat, lon = self._filteredCities[idx]
            ns = 'N' if lat >= 0 else 'S'
            ew = 'E' if lon >= 0 else 'W'
            self._coordLabel.SetLabel(
                "%.4f°%s,  %.4f°%s" % (abs(lat), ns, abs(lon), ew))
        else:
            self._coordLabel.SetLabel("")


    ###########################################################
    def _onOK(self, event):
        idx = self._listBox.GetSelection()
        if idx != wx.NOT_FOUND and idx < len(self._filteredCities):
            city = self._filteredCities[idx]
            self._selection = (city[1], city[2])
            self.EndModal(wx.ID_OK)
        else:
            self.EndModal(wx.ID_CANCEL)


    ###########################################################
    def getSelection(self):
        """Return (lat, lon) of the selected city.

        @return  (lat, lon) tuple, or None if dialog was cancelled.
        """
        return self._selection
