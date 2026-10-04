#!/usr/bin/env python

#*****************************************************************************
#
# MinTravelTrigger.py
#    Trigger: travel-based (exclude objects that barely moved)
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


# Python imports...
import operator

# Common 3rd-party imports...

# Toolbox imports...

# Local imports...
from .BaseTrigger import BaseTrigger


# Travel thresholds are quoted at this frame size, matching
# VideoPipeline._kThresholdRefSize, so one number means the same thing on every
# camera.  The stored centroid extremes are in whatever size that camera was
# ANALYSED at (typically 640x360), so the threshold is scaled down to match
# before it reaches SQL.
_kThresholdRefSize = (1280, 720)


###############################################################
class MinTravelTrigger(BaseTrigger):
    """Excludes objects whose centroid barely moved over their life.

    "Travel" is the span of the centroid: (maxCx-minCx) + (maxCy-minCy), stored
    per object by DataManager.  It is the strongest separator this fleet has
    between a real subject and camera noise -- measured across 16,215 recorded
    objects, median 407 for person/animal against 102 for unknowns, where blob
    AREA separates them only 3.8x.  Something that flickers in place at the edge
    of frame is the same SIZE as a real subject at distance; only movement tells
    them apart.

    Shaped exactly like MinSizeTrigger: push a SQL predicate into the
    DataManager, delegate to the child, then clear it again.
    """

    ###########################################################
    def __init__(self, dataMgr, minTravel, childTrigger=None):
        """Initializer for the MinTravelTrigger class.

        @param  dataMgr       The DataManager instance.
        @param  minTravel     The minimum distance an object must have travelled,
                              in pixels at _kThresholdRefSize.
        @param  childTrigger  The child trigger that we're modifying.
        """
        BaseTrigger.__init__(self)

        self._dataMgr = dataMgr
        self._minTravel = minTravel
        self._childTrigger = childTrigger


    ###########################################################
    def __str__(self):
        """Create a string representation of the trigger

        @return strDesc  A string description of the trigger
        """
        strDesc = 'MinTravelTrigger %d' % (self._minTravel)
        if self._childTrigger:
            strDesc += ' doing ' + str(self._childTrigger)
        return strDesc


    ###########################################################
    def _scaledThreshold(self, procSizesMsRange):
        """Return the threshold in ANALYSIS pixels for this camera.

        Travel is a LENGTH, so it scales with the linear ratio between the
        analysis frame and the reference frame -- not with the area ratio that
        the two size thresholds use.

        When a camera was analysed at more than one size across the searched
        range there is no single correct scale, so take the SMALLEST, which
        gives the most permissive threshold.  Showing a few extra low-travel
        detections is recoverable; silently hiding real ones is not.
        """
        if not self._minTravel:
            return 0

        refW, refH = _kThresholdRefSize
        scale = None
        for entry in (procSizesMsRange or []):
            try:
                procW, procH = int(entry[0]), int(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if procW <= 0 or procH <= 0:
                continue
            thisScale = ((procW * procH) / float(refW * refH)) ** 0.5
            if scale is None or thisScale < scale:
                scale = thisScale

        if scale is None:
            # Unknown processing size: fall back to the reference, i.e. treat the
            # number exactly as quoted.
            scale = 1.0

        return int(round(self._minTravel * scale))


    ###########################################################
    def _pushFilter(self, procSizesMsRange):
        """Apply our threshold on top of any already in force; return the old one.

        Two of these triggers can be active at once -- the Search screen wraps a
        live slider around a query that may already carry the editor's own travel
        filter -- so an inner one must COMBINE rather than replace.  The stricter
        threshold wins, which is what "both filters are on" should mean.  Without
        this the inner trigger's reset clears the outer one, and the slider looks
        connected while doing nothing.

        (MinSizeTrigger simply overwrites here.  It gets away with it because
        only one is ever constructed, from the single target block; this trigger
        has two independent sources, so it cannot.)
        """
        prev = 0
        try:
            prev = self._dataMgr.getMinTravelFilter()
        except AttributeError:
            # Older DataManager without the accessor: behave as before.
            pass
        mine = self._scaledThreshold(procSizesMsRange)
        self._dataMgr.setMinTravelFilter(max(prev, mine))
        return prev


    ###########################################################
    def _popFilter(self, prev):
        """Put back whatever threshold was in force before _pushFilter."""
        self._dataMgr.setMinTravelFilter(prev)


    ###########################################################
    def search(self, timeStart=None, timeStop=None, type='single', procSizesMsRange=None):
        """Search the database for objects tripping the trigger

        @param  timeStart        The time to start searching from, None for beginning
        @param  timeStop         The time to stop searching at, None for present
        @param  type             The type of search to be performed
                                   'single'   - The database is presumed complete
                                   'realtime' - Maintain state between searches
        @param  procSizesMsRange A list of sizes the camera was processed at for
                                 certain ranges of time. Contains a list of
                                 4-tuples of (procWidth, procHeight, firstMs,
                                 lastMs).
        @return triggered        A list of dbId, frame, time tuples for objects
                                 that set off the trigger
        """
        _ = type

        # Set the database filter for min travel...
        prev = self._pushFilter(procSizesMsRange)

        try:
            # If we have a child trigger return the result of it's search on the
            # filtered database
            if self._childTrigger:
                triggered = self._childTrigger.search(timeStart, timeStop, type, procSizesMsRange)

            else:
                objIds = self._dataMgr.getObjectsBetweenTimes(timeStart, timeStop)
                bboxes = self._dataMgr.getObjectBboxesBetweenTimes(
                                                    objIds, timeStart, timeStop)
                triggered = map(operator.itemgetter(6, 4, 5), bboxes)
        finally:
            # Restore in a finally: _filterStr is mutable state shared by every
            # later query on this DataManager, so a filter leaked by an exception
            # here would silently narrow unrelated searches for the rest of the
            # session.
            self._popFilter(prev)

        return triggered


    ###########################################################
    def searchForRanges(self, timeStart=None, timeStop=None, procSizesMsRange=None):
        """Search the database for objects tripping the trigger

        @param  timeStart         The time to start searching from, None for all
                                  time
        @param  timeStop          The time to stop searching at, None for present
        @param  procSizesMsRange  A list of sizes the camera was processed at for
                                  certain ranges of time.
        @return resultItems       A iterable of tuples, like this: [
                                    (objId, ((firstMs, firstFrame),
                                             (lastMs, lastFrame)))
                                    ...
                                  ]
        """
        # Set the database filter for min travel...
        prev = self._pushFilter(procSizesMsRange)

        try:
            # If we have a child trigger return the result of it's search on the
            # filtered database
            if self._childTrigger:
                resultItems = self._childTrigger.searchForRanges(timeStart, timeStop, procSizesMsRange)

            else:
                resultItems = self._dataMgr.getObjectRangesBetweenTimes(
                    timeStart, timeStop
                )
        finally:
            # Restore the previous filter -- see search().
            self._popFilter(prev)

        return resultItems


    ###########################################################
    def finalize(self, objList, procSizesMsRange=None):
        """Do a final search on some objects assuming all data has been received

        @param  objList          A list or set of dbIds of objects to search
        @param  procSizesMsRange A list of sizes the camera was processed at for
                                 certain ranges of time.
        @return triggered        A list of dbId, frame, time tuples for objects
                                 that set off the trigger presuming no more data
                                 will come
        """
        if self._childTrigger:
            return self._childTrigger.finalize(objList, procSizesMsRange)

        return []


    ###########################################################
    def reset(self):
        """Remove any continuation data from a trigger"""
        if self._childTrigger:
            self._childTrigger.reset()


    ###########################################################
    def setProcessingCoordSpace(self, coordSpace):
        """Set the coordinate space of the processing size.

        @param  coordSpace  The (width, height) the video is processed at.
        """
        # Forwarded, unlike MinSizeTrigger which drops it: a child that needs the
        # coord space must still receive it through us.
        if self._childTrigger:
            self._childTrigger.setProcessingCoordSpace(coordSpace)


    ###########################################################
    def setDataManager(self, dataManager):
        """Set the data manager containing the desired search information

        @param  dataMgr  The new data manager
        """
        self._dataMgr = dataManager
        if self._childTrigger:
            self._childTrigger.setDataManager(dataManager)


    ###########################################################
    def getPlayTimeOffset(self):
        """Return the time in ms before trigger the video should start playing

        @return msOffset  The time in ms to 'rewind' before the first fire.
        @return preserve  True if clips should preserve msOffset if possible.
        """
        if self._childTrigger:
            return self._childTrigger.getPlayTimeOffset()
        return 0, False


    ###########################################################
    def shouldCombineClips(self):
        """Determine whether overlapping clips should be combined.

        @return combine  True if overlaping clips should be combined.
        """
        if self._childTrigger:
            return self._childTrigger.shouldCombineClips()
        return True


    ###########################################################
    def getVideoDebugLines(self):
        """Retrieve lines to be displayed for debugging video.

        @return triggerLines  Lines to display on the screen.
        """
        if self._childTrigger:
            return self._childTrigger.getVideoDebugLines()
        return []


    ###########################################################
    def spatiallyAware(self):
        """Checks if this trigger uses spacial information for processing.

        @return  bool  True if this trigger uses, contains, or processes spacial
                       information needed for it to work properly. False
                       otherwise.
        """
        # True, and unlike MinSizeTrigger's hardcoded True this is deliberate:
        # the threshold is scaled by the camera's processing size, so the search
        # genuinely has to run per camera rather than in one pass across all of
        # them (see SearchUtils._getMatchingRanges).
        return True
