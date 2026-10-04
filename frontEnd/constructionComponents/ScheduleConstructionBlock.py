#!/usr/bin/env python

#*****************************************************************************
#
# ScheduleConstructionBlock.py
#   The flow chart block for a rule's Action Schedule.
#
#   The schedule used to live in a box at the bottom of the Take action tab,
#   which left that tab with no room for anything else.  It now has its own
#   block at the top of the flow chart, and this summarises it in the two lines
#   a construction block has room for.
#
#   Unlike the other blocks, the schedule is not part of the SavedQueryDataModel
#   -- it belongs to the rule, and lives in the config panel's own controls
#   until the dialog is closed.  So there is no model to listen to: the config
#   panel calls setSchedule() when the schedule changes.
#
#*****************************************************************************

import sys

# Common 3rd-party imports...
import wx

# Local imports...
from .ConstructionBlock import ConstructionBlock, kDefaultConstructionBlockSize

# Constants...
_kColor = (80, 80, 80)      # Grey-ish, matching the black SCHEDULE title art

# clock_small.png is 40x40; the other block icons are 32 tall.
_kIconPath = "frontEnd/bmps/clock_small.png"
_kIconSize = 32

_kAllDayLabel = "All day"
_kNever = "Never"

_kDayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']


##############################################################################
def _scheduleLabel(schedule):
    """Summarise a schedule in the two lines a construction block can show.

    @param  schedule  A schedule dict, as built by
                      ResponseConfigPanel.getSchedule().
    @return label     A one or two line label.
    """
    if not schedule:
        return _kAllDayLabel

    dayType = schedule.get('dayType', 'Every day')
    if dayType == 'Every day':
        days = "Every day"
    elif dayType == 'Custom...':
        customDays = schedule.get('customDays') or []
        if not customDays:
            # No day ticked: the actions can never fire.  Worth saying plainly
            # rather than showing an empty line.
            return _kNever
        if len(customDays) > 3:
            days = "%d days" % len(customDays)
        else:
            # Keep the week's order rather than the order they were ticked in.
            days = " ".join(d for d in _kDayNames if d in customDays)
    else:
        days = dayType         # "Weekdays" / "Weekends"

    if schedule.get('is24Hours', True):
        return "%s\n%s" % (days, _kAllDayLabel)

    startType = schedule.get('startType', 'fixed')
    stopType = schedule.get('stopType', 'fixed')
    if startType == 'fixed' and stopType == 'fixed':
        when = "%02d:%02d-%02d:%02d" % (schedule.get('startHour', 0),
                                        schedule.get('startMin', 0),
                                        schedule.get('stopHour', 0),
                                        schedule.get('stopMin', 0))
    else:
        # Sunrise/sunset, abbreviated to fit: "Rise-Set", "Set-Rise".
        _short = {'sunrise': "Rise", 'sunset': "Set"}
        when = "%s-%s" % (_short.get(startType, startType.title()),
                          _short.get(stopType, stopType.title()))

    return "%s\n%s" % (days, when)


##############################################################################
class ScheduleConstructionBlock(ConstructionBlock):
    """The construction block for a rule's action schedule."""

    ###########################################################
    def __init__(self, parent, schedule=None,
                 pos=wx.DefaultPosition, size=kDefaultConstructionBlockSize):
        """ScheduleConstructionBlock constructor.

        @param  schedule  The initial schedule dict, or None for the default.
        @param  parent    Our parent UI element.
        @param  pos       Our UI position.
        @param  size      Our UI size.
        """
        self._schedule = schedule

        super(ScheduleConstructionBlock, self).__init__(
            parent, self._loadIcon(), _scheduleLabel(schedule), _kColor,
            pos, size
        )


    ###########################################################
    @staticmethod
    def _loadIcon():
        """Load the clock, scaled to the height the other block icons use.

        @return bmp  The icon bitmap, or None if it could not be loaded.
        """
        try:
            img = wx.Bitmap(_kIconPath).ConvertToImage()
            if img.GetHeight() != _kIconSize:
                scale = float(_kIconSize) / img.GetHeight()
                img = img.Scale(max(1, int(img.GetWidth() * scale)), _kIconSize,
                                wx.IMAGE_QUALITY_HIGH)
            return img.ConvertToBitmap()
        except Exception:
            return None


    ###########################################################
    def setSchedule(self, schedule):
        """Update our label to reflect a new schedule.

        Called by the config panel as the schedule controls change; there is no
        data model to listen to (see the module docstring).

        @param  schedule  The schedule dict.
        """
        self._schedule = schedule
        self.SetLabel(_scheduleLabel(schedule))


##############################################################################
def test_main():
    """OB_REDACT
       Contains various self-test code.
    """
    print("NO TESTS")


##############################################################################
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_main()
    else:
        print("Try calling with 'test' as the argument.")
