#!/usr/bin/env python

#*****************************************************************************
#
# TriggerUtils.py
#     Various trigger-related utility functions
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


from ctypes import Structure, c_int


###############################################################
class BBOX(Structure):
    """Bounding box matching the layout expected by optsearches."""
    _fields_ = [("x1", c_int),
                ("y1", c_int),
                ("x2", c_int),
                ("y2", c_int)]


# ---------------------------------------------------------------------------
# Pure-Python implementations of the two optsearches.c functions.
# These replace the compiled C DLL so the module can be imported in dev mode
# without a build step.  Semantics are identical to the C originals.
# ---------------------------------------------------------------------------

_CENTER = 0; _TOP = 1; _BOTTOM = 2; _LEFT = 3; _RIGHT = 4
_IS_LEFT = 0; _IS_RIGHT = 1; _IS_ON = 2
_FROM_LEFT = 0; _FROM_RIGHT = 1; _FROM_ANY = 2


def _track_point(box, location):
    x1, y1, x2, y2 = box.x1, box.y1, box.x2 - 1, box.y2 - 1
    if location == _CENTER:  return (x1 + x2) // 2, (y1 + y2) // 2
    if location == _TOP:     return (x1 + x2) // 2, y1
    if location == _BOTTOM:  return (x1 + x2) // 2, y2
    if location == _LEFT:    return x1, (y1 + y2) // 2
    return x2, (y1 + y2) // 2   # RIGHT


def _side(px, py, x1, y1, x2, y2):
    a = (x2 - x1) * (py - y1)
    b = (y2 - y1) * (px - x1)
    if a > b: return _IS_LEFT
    if a < b: return _IS_RIGHT
    return _IS_ON


def did_obj_cross(prevBox, curBox, boundary, location, direction):
    """Port of optsearches.c:did_obj_cross."""
    px, py = _track_point(prevBox, location)
    cx, cy = _track_point(curBox,  location)
    bx1, by1, bx2, by2 = boundary.x1, boundary.y1, boundary.x2, boundary.y2

    prevDir = _side(px, py, bx1, by1, bx2, by2)
    curDir  = _side(cx, cy, bx1, by1, bx2, by2)
    if prevDir == curDir:
        return 0

    objXs = px - cx;  objYs = py - cy
    segXs = bx1 - bx2; segYs = by1 - by2
    denom = objXs * segYs - segXs * objYs
    if denom == 0:
        return 0

    objCp = px * cy - cx * py
    segCp = bx1 * by2 - bx2 * by1
    intX  = (objCp * segXs - segCp * objXs) / denom
    intY  = (objCp * segYs - segCp * objYs) / denom

    in_obj = (min(px, cx) <= intX <= max(px, cx))
    in_seg = (min(bx1, bx2) <= intX <= max(bx1, bx2))
    if not (in_obj and in_seg):
        return 0
    if bx1 == bx2 and not (min(by1, by2) <= intY <= max(by1, by2)):
        return 0
    if px == cx and not (min(py, cy) <= intY <= max(py, cy)):
        return 0

    if direction == _FROM_ANY:
        return 1 if prevDir != _IS_ON else 0
    return 1 if direction == prevDir else 0


def is_obj_inside(box, location, segments, numSegments):
    """Port of optsearches.c:is_obj_inside (ray-casting point-in-polygon)."""
    tx, ty = _track_point(box, location)
    tX2 = -1
    tXs = tx - tX2
    tCp = tXs * ty
    intersections = 0

    for i in range(numSegments):
        s = segments[i]
        x1, y1, x2, y2 = s.x1, s.y1, s.x2, s.y2
        xs = x1 - x2;  ys = y1 - y2
        denom = tXs * ys - xs * 0   # tYs == 0
        if denom == 0:
            continue
        cp = x1 * y2 - x2 * y1
        intX = (tCp * xs - cp * tXs) / denom
        intY = (tCp * ys - cp * 0)   / denom

        if not (min(tx, 10000) <= intX <= max(tx, 10000)):
            continue
        if not (min(x1, x2) <= intX <= max(x1, x2)):
            continue
        if x1 == x2 and not (min(y1, y2) <= intY <= max(y1, y2)):
            continue
        if tx == tX2 and not (min(ty, ty) <= intY <= max(ty, ty)):
            continue
        if ((x1 == intX and y1 == intY and y2 > intY) or
                (x2 == intX and y2 == intY and y1 > intY)):
            continue
        intersections += 1

    return intersections % 2

kTrackPointStrToInt = {'center' : 0,
                       'top'    : 1,
                       'bottom' : 2,
                       'left'   : 3,
                       'right'  : 4}

kDirectionStrToInt = {'left'  : 0,
                      'right' : 1,
                      'any'   : 2}

# Make the module itself act as _searchlib so optimizedIsPointInside and
# optimizedDidObjCrossLine can call the pure-Python is_obj_inside / did_obj_cross
# (originally these called a compiled optsearches C DLL; the pure-Python ports
# above are the drop-in replacements).
import sys as _sys
_searchlib = _sys.modules[__name__]


###############################################################
def optimizedDidObjCrossLine(prevBbox, curBbox, boundary, location, direction):
    """Determine whether an object crossed a boundary.

    @param  prevBbox   A BBOX of the bounding box at the previous frame.
    @param  curBbox    A BBOX of the bounding box at the current frame.
    @param  boundary   A BBOX defining the line segment to test.
    @param  location   A value from kTrackPointStrToInt.
    @param  direction  A value from kDirectionStrToInt.
    @return didCross   True if the object tracking point crossed boundary.
    """
    retVal = _searchlib.did_obj_cross(prevBbox, curBbox, boundary, location,
                                      direction)
    if retVal == 1:
        return True
    return False


###############################################################
def optimizedIsPointInside(bbox, trackLocation, cSegments, numSegments):
    """Determine whether a point on an object is inside a region.

    @param  bbox           A BBOX struct of the object's bounding box.
    @param  trackLocation  The location on the point to track, must be a
                           value from kTrackPointStrToInt.
    @param  cSegments      A ctypes Array of segments defining the array.
    @param  numSegments    The number of segments in cSegments.
    @return isInside       True if the point is inside the region, else False.
    """
    retVal = _searchlib.is_obj_inside(bbox, trackLocation, cSegments,
                                      numSegments)
    if retVal == 1:
        return True
    return False


###############################################################
def getBboxTrackingPoint(bbox, location='center'):
    """Return a point on the bbox

    @param  bbox      Coordinates for the top left and bottom right of a rect
    @param  location  The location on the box to track
    @return point     X,Y coordinates of the requested point
    """
    assert location in list(kTrackPointStrToInt.keys())

    # Remember that (x2, y2) on bbox are _outside_ the object; adjust so they're
    # not for the math below...
    bbox = (bbox[0], bbox[1], bbox[2]-1, bbox[3]-1)

    if location == 'center':
        return ((bbox[2]+bbox[0])/2, (bbox[3]+bbox[1])/2)
    elif location == 'top':
        return ((bbox[2]+bbox[0])/2, bbox[1])
    elif location == 'bottom':
        return ((bbox[2]+bbox[0])/2, bbox[3])
    elif location == 'left':
        return (bbox[0], (bbox[3]+bbox[1])/2)
    elif location == 'right':
        return (bbox[2], (bbox[3]+bbox[1])/2)