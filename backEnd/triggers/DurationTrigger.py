#!/usr/bin/env python

#*****************************************************************************
#
# DurationTrigger.py
#     Trigger: duration-based (exclude objects if visible for less than specified time)
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



import operator

from .BaseTrigger import BaseTrigger


# How long an object may be missing from the child trigger's output before its
# duration clock restarts.  The clock used to restart on ANY frame gap, and the
# real-time search -- which runs in short incremental windows -- also forgot an
# object whenever one window happened to hold none of its frames.  The motion
# tracker drops frames constantly while carrying the same object (it coasts a
# track for 3 s, VideoPipeline._kMotionCooldown), so "more than N seconds"
# measured the longest UNBROKEN run instead of how long the object was there.
# Measured 2026-09-23 on 010_SouthGate_lr 17:42:33: two person tracks of 7.8 s
# and 6.9 s never reached the rule's 4 s -- their longest unbroken runs were
# 1.0 s and 1.8 s -- so "People in 010_SouthGate_lr" never fired.
#
# Matches QueuedDataManagerCloud._kTrackLostGraceMs (2000), the tolerance the
# type vote uses for the same dropouts.
#
# Opt-in per trigger (maxGapMs), not global: on rules whose target is
# "anything", the tolerance lets flickering foliage tracks run their clock up
# too.  Over 2026-09-23 06:00-17:45 it would have taken unclassified tracks
# passing "Any object ... more than N s" from 10 to 75 on 020_BigTree (3 s),
# 99 to 415 on 030_Hill (2 s) and 166 to 326 on 120_Ravine (2 s).  Rules on a
# detected class (person/vehicle/animal/face/nudity) are already filtered by
# the detector, which is where the tolerance belongs.  See SavedQueryDataModel.
kDurationGapToleranceMs = 2000


###############################################################
class DurationTrigger(BaseTrigger):
    """A trigger that fires when another trigger remains active over time"""
    ###########################################################
    def __init__(self, childTrigger, msecs, moreThan=True, maxGapMs=0):
        """Initializer for the DurationTrigger class

        @param  childTrigger  The trigger to monitor
        @param  msecs         The duration boundary in msecs
        @param  moreThan      If True will alert when childTrigger has been
                              active longer duration.  If False, will fire while
                              childTrigger has been active less than duration.
        @param  maxGapMs      How long the object may be missing before the
                              duration restarts (see kDurationGapToleranceMs).
                              0 = legacy: restart on any skipped frame, and
                              forget objects absent from a real-time window.
        """
        BaseTrigger.__init__(self)

        self._childTrigger = childTrigger
        self._msecs = msecs
        self._moreThan = moreThan
        self._maxGapMs = max(0, int(maxGapMs or 0))

        self._playOffset = 0
        if self._moreThan:
            self._playOffset = msecs

        # A dictionary of objectId to (first seen time, last frame seen,
        # last seen time)
        self._activeObjects = {}


    ###########################################################
    def __str__(self):
        """Create a string representation of the trigger

        @return strDesc  A string description of the trigger
        """
        strDesc = 'Duration Trigger - Fires when [%s] is active for ' % str(
                                                            self._childTrigger)
        if self._moreThan:
            strDesc += 'more than'
        else:
            strDesc += 'less than'
        strDesc += ' %.3f seconds.' % (self._msecs/1000.)

        return strDesc


    ###########################################################
    def setProcessingCoordSpace(self, coordSpace):
        """Sets the processing coordinate space for searches.

        Note 1: Should call 'spatiallyAware()' first to see if it is worth
                calling this function, since only spatially aware triggers will
                implement it.
        Note 2: There is no need to overload this method if designing a new
                subclass that will not be spatially aware, unless it will
                contain and expose functionality of other triggers that are
                spatially aware.

        @param  coordSpace  The coordinate space as a 2-tuple, (width, height).
        """
        self._childTrigger.setProcessingCoordSpace(coordSpace)


    ###########################################################
    def search(self, timeStart=None, timeStop=None, type='single', procSizesMsRange=None):
        """Search the database for objects tripping the trigger

        @param  timeStart        The time to start searching from, None for beginning
        @param  timeStop         The time to stop searching at, None for present
        @param  type             The type of search to be performed
                                   'single'   - The database is presumed complete
                                   'realtime' - Maintain state between searches
        @param  procSizesMsRange A list of sizes the camera was processed at for
                                 certain ranges of time. Contains a list of 4-tuples
                                 of (procWidth, procHeight, firstMs, lastMs).
                                 Note: if the list contains only one 4-tuple, then
                                 procWidth and procHeight is unique, and firstMs and
                                 lastMs should be ignored; they may hold None values.
                                 If the list contains more than one 4-tuple, then
                                 procWidth and procHeight are not unique, and you must
                                 use the firstMs and lastMs to determine which
                                 procSize was used for a specified period of time.
        @return triggered        A list of dbId, frame, time tuples for objects that
                                 set off the trigger
        """
        if type == 'single':
            # Reset the state dict
            self.reset()

        return self._doSearch(self._childTrigger.search(timeStart, timeStop,
                                                        type, procSizesMsRange))

        # TODO: Need to call finalize()?  It shouldn't be needed for duration
        # triggers, since we can't ever trigger unless we actually got more
        # data...


    ###########################################################
    def searchForRanges(self, timeStart=None, timeStop=None, procSizesMsRange=None):
        """Search the database for objects tripping the trigger, as ranges.

        We deliberately do NOT use the inherited BaseTrigger.searchForRanges:
        it starts a new range on every frame-number discontinuity in our output,
        and our per-frame filter (_doSearch) punches holes on purpose -- it drops
        the first `_msecs` of each visibility run and re-arms after a gap (any
        skipped frame, or one longer than maxGapMs when that is set).
        Coalescing that holed output would fragment a single object into several
        ranges, which INFLATES the result count when this (restrictive) filter is
        enabled -- the opposite of what a filter should do.

        The unfiltered query returns exactly one range per object (TargetTrigger's
        SQL GROUP BY path), so mirror it: collapse our surviving frames into a
        single (min,max) range per object.  Playback still rewinds to the true
        start via getPlayTimeOffset().

        @return resultItems  An iterable of tuples, like this: [
                               (objId, ((firstMs, firstFrame),
                                        (lastMs, lastFrame)), camLoc)
                               ...
                             ]
        """
        perObj = {}
        for objId, frameNum, ms in self.search(timeStart, timeStop, 'single',
                                                procSizesMsRange):
            rng = perObj.get(objId)
            if rng is None:
                perObj[objId] = [(ms, frameNum), (ms, frameNum)]
            else:
                if ms < rng[0][0]:
                    rng[0] = (ms, frameNum)
                if ms > rng[1][0]:
                    rng[1] = (ms, frameNum)
        return [(objId, (rng[0], rng[1]), None)
                for objId, rng in perObj.items()]


    ###########################################################
    def finalize(self, objList, procSizesMsRange=None):
        """Do a final search on some objects assuming all data has been received

        @param  objList          A list or set of dbIds of objects to search
        @param  procSizesMsRange A list of sizes the camera was processed at for
                                 certain ranges of time. Contains a list of 4-tuples
                                 of (procWidth, procHeight, firstMs, lastMs).
                                 Note: if the list contains only one 4-tuple, then
                                 procWidth and procHeight is unique, and firstMs and
                                 lastMs should be ignored; they may hold None values.
                                 If the list contains more than one 4-tuple, then
                                 procWidth and procHeight are not unique, and you must
                                 use the firstMs and lastMs to determine which
                                 procSize was used for a specified period of time.
        @return triggered        A list of dbId, frame, time tuples for objects that
                                 set off the trigger presuming no more data will come
        """
        # TODO: Since we finalize ourselves in search(), isn't child in charge
        # of finalizing itself?
        return self._doSearch(
            self._childTrigger.finalize(objList, procSizesMsRange),
            True
        )


    ###########################################################
    def reset(self):
        """Remove any continuation data from a trigger"""
        self._activeObjects = {}

        # TODO: Since we reset ourselves in search(), isn't child in charge of
        # resetting itself?
        self._childTrigger.reset()


    ###########################################################
    def _doSearch(self, triggerResults, isFinalize=False):
        """Update the active objects and trigger if conditions are met

        @param  triggerResults  The results of a search on the child trigger
        @param  isFinalize      True if this is being called from finalize()
        @return triggered       A list of dbId, frame, time tuples for objects
                                that set off the trigger
        """
        triggered = []

        # We need the results to be sorted by time which they likely won't be
        triggerResults.sort(key=operator.itemgetter(2))

        prevActive = set(self._activeObjects.keys())
        curActive = set()

        for result in triggerResults:
            # Add this objId to the currently active list
            curActive.add(result[0])

            if result[0] in self._activeObjects:
                firstTime, lastFrame, lastTime = self._activeObjects[result[0]]
                if self._maxGapMs:
                    # Restart only after more than a tracker dropout.
                    if result[2] - lastTime > self._maxGapMs:
                        firstTime = result[2]
                elif lastFrame < result[1]-1:
                    # Ensure we don't count skipped frames
                    firstTime = result[2]
                self._activeObjects[result[0]] = (firstTime, result[1],
                                                  result[2])

                # Calc the duration of this trigger
                diff = result[2] - firstTime

                if self._moreThan:
                    if diff > self._msecs:
                        triggered.append(result)
                elif diff < self._msecs:
                    triggered.append(result)
            else:
                # New object, add it to the dictionary
                self._activeObjects[result[0]] = (result[2], result[1],
                                                  result[2])

                if not self._moreThan:
                    # If this is the first time we've seen this object trigger
                    # it's been happening for less than any possible duration
                    triggered.append(result)

        # Remove objects no longer triggering from our active list
        if not isFinalize:
            if not self._maxGapMs:
                expired = prevActive.difference(curActive)
            elif triggerResults:
                # Only once gone for longer than maxGapMs: a real-time window
                # can be shorter than a tracker dropout.  An empty batch carries
                # no clock, so nothing expires on it; the gap test above still
                # restarts an object that comes back late.
                newest = triggerResults[-1][2]
                expired = [objId for objId, (_, _, lastTime) in
                           self._activeObjects.items()
                           if newest - lastTime > self._maxGapMs]
            else:
                expired = ()
            for objId in expired:
                del self._activeObjects[objId]

        return triggered


    ###########################################################
    def setDataManager(self, dataManager):
        """Set the data manager containing the desired search information

        @param  dataManager  The new data manager
        """
        self._childTrigger.setDataManager(dataManager)


    ###########################################################
    def getPlayTimeOffset(self):
        """Return the time in ms before trigger the video should start playing

        @return msOffset  The time in ms to 'rewind' before the first fire.
        @return preserve  True if clips should preserve msOffset if possible.
        """
        return self._playOffset, True


    ###########################################################
    def shouldCombineClips(self):
        """Determine whether overlapping clips should be combined.

        @return combine  True if overlaping clips should be combined.
        """
        return self._childTrigger.shouldCombineClips()


    ###########################################################
    def getVideoDebugLines(self):
        """Retrieve lines to be displayed for debugging video.

        @param  triggerLines  A list of TriggerLineSegment or TriggerRegion
                              objects, which can be used to retrieve a list of
                              (x1,y1,x2,y2) tuples defining lines to display on
                              the screen by calling their getPoints(coordSpace)
                              instance method.
        """
        return self._childTrigger.getVideoDebugLines()


    ###########################################################
    def spatiallyAware(self):
        """Checks if this trigger uses spacial information for processing.

        @return  bool  True if this trigger uses, contains, or processes spacial
                       information needed for it to work properly. False
                       otherwise.
        """
        return self._childTrigger.spatiallyAware()
