#!/usr/bin/env python

#*****************************************************************************
#
# AttributeTrigger.py
#    Trigger: based on detection attributes (faces / nudity)
#
#*****************************************************************************

"""Trigger matching objects by their detection ATTRIBUTES.

The classic TargetTrigger matches on the objects.type column ('person',
'vehicle', ...).  Face recognition and nudity live in the objectAttributes
side table instead (faceName / faceDetConf / nudity, keyed by objUid), so the
"Nudity" and "Faces" rule targets use this trigger.  Structure and lifecycle
mirror TargetTrigger exactly: push a DataManager filter, delegate to the child
trigger (or read object ranges directly), then clear the filter.

attrSpec (see DataManager.setAttributeFilter):
    {'nudity': True}                          nudity flagged
    {'face': True, 'faceNames': [...names]}   face detected; names empty = any
                                              face; "Unknown" = unrecognized
"""

# Python imports...
import operator

# Local imports...
from .BaseTrigger import BaseTrigger


###############################################################
class AttributeTrigger(BaseTrigger):
    ###########################################################
    def __init__(self, dataMgr, attrSpec, childTrigger=None):
        """Initializer for the AttributeTrigger class.

        @param  dataMgr       The DataManager instance.
        @param  attrSpec      The attribute spec dict (see module docstring).
        @param  childTrigger  The child trigger that we're modifying.
        """
        BaseTrigger.__init__(self)

        self._dataMgr = dataMgr
        self._attrSpec = attrSpec
        self._childTrigger = childTrigger


    ###########################################################
    def __str__(self):
        """Create a string representation of the trigger

        @return strDesc  A string description of the trigger
        """
        if self._attrSpec.get('nudity'):
            what = 'nudity'
        else:
            names = [n for n in (self._attrSpec.get('faceNames') or []) if n]
            what = 'face' if not names else 'face %s' % ' or '.join(names)

        strDesc = 'AttributeTrigger - ' + what
        if self._childTrigger:
            strDesc += ' doing ' + str(self._childTrigger)
        return strDesc


    ###########################################################
    def setProcessingCoordSpace(self, coordSpace):
        """Sets the processing coordinate space for searches.

        @param  coordSpace  The coordinate space as a 2-tuple, (width, height).
        """
        if self._childTrigger:
            self._childTrigger.setProcessingCoordSpace(coordSpace)


    ###########################################################
    def search(self, timeStart=None, timeStop=None, type='single', procSizesMsRange=None):
        """Search the database for objects tripping the trigger

        @param  timeStart        The time to start searching from, None for beginning
        @param  timeStop         The time to stop searching at, None for present
        @param  type             The type of search to be performed
        @param  procSizesMsRange Per-range processing sizes (see TargetTrigger).
        @return triggered        A list of dbId, frame, time tuples for objects
                                 that set off the trigger
        """
        _ = type
        triggered = []

        # Set the database filter for the desired attributes...
        self._dataMgr.setAttributeFilter(self._attrSpec, timeStart, timeStop)

        # If we have a child trigger return the result of it's search on the
        # filtered database
        if self._childTrigger:
            triggered = self._childTrigger.search(timeStart, timeStop, type, procSizesMsRange)
        else:
            objIds = self._dataMgr.getObjectsBetweenTimes(timeStart, timeStop)
            bboxes = self._dataMgr.getObjectBboxesBetweenTimes(
                                                    objIds, timeStart, timeStop)
            triggered = map(operator.itemgetter(6, 4, 5), bboxes)

        # Remove the database filter
        self._dataMgr.setAttributeFilter(None)

        return triggered


    ###########################################################
    def searchForRanges(self, timeStart=None, timeStop=None, procSizesMsRange=None):
        """Search the database for objects tripping the trigger

        @param  timeStart         The time to start searching from, None for all time
        @param  timeStop          The time to stop searching at, None for present
        @param  procSizesMsRange  Per-range processing sizes (see TargetTrigger).
        @return resultItems       An iterable of tuples, like this: [
                                    (objId, ((firstMs, firstFrame),
                                             (lastMs, lastFrame)))
                                    ...
                                  ]
        """
        # Set the database filter for the desired attributes...
        self._dataMgr.setAttributeFilter(self._attrSpec, timeStart, timeStop)

        # If we have a child trigger return the result of it's search on the
        # filtered database
        if self._childTrigger:
            resultItems = self._childTrigger.searchForRanges(timeStart, timeStop, procSizesMsRange)
        else:
            resultItems = self._dataMgr.getObjectRangesBetweenTimes(
                timeStart, timeStop
            )

        # Remove the database filter
        self._dataMgr.setAttributeFilter(None)

        return resultItems


    ###########################################################
    def finalize(self, objList, procSizesMsRange=None):
        """Do a final search on some objects assuming all data has been received

        @param  objList          A list or set of dbIds of objects to search
        @param  procSizesMsRange Per-range processing sizes (see TargetTrigger).
        @return triggered        A list of dbId, frame, time tuples
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
    def setDataManager(self, dataManager):
        """Set the data manager containing the desired search information

        @param  dataManager  The new data manager
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
        """Retrieve lines to be displayed for debugging video."""
        if self._childTrigger:
            return self._childTrigger.getVideoDebugLines()
        return []


    ###########################################################
    def spatiallyAware(self):
        """Checks if this trigger uses spacial information for processing.

        @return  bool  True if this trigger uses, contains, or processes spacial
                       information needed for it to work properly.
        """
        if self._childTrigger:
            return self._childTrigger.spatiallyAware()

        return False
