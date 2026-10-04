#! /usr/local/bin/python

#*****************************************************************************
#
# RealTimeRule.py
#    Encapsulation of query class combined with the scheduling information applying to it.
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
import datetime
import time

# Common 3rd-party imports...

# Local imports...



# Globals...
_kDefaultRuleSchedule = {'dayType' : 'Every day',
                         'customDays' : [],
                         'is24Hours' : True,
                         'startHour' : 8,
                         'stopHour' : 18,
                         'startMin' : 0,
                         'stopMin' : 0,
                         'startType' : 'fixed',
                         'startOffset' : 0,
                         'stopType' : 'fixed',
                         'stopOffset' : 0,
                         'latitude' : None,
                         'longitude' : None}

def _calcSunriseSunsetMinutes(lat, lon, forDate=None):
    """Return (sunrise, sunset) as integer minutes from local midnight.

    Uses a simplified NOAA solar algorithm.  Returns (None, None) for polar
    day/night conditions.

    @param lat      Latitude in decimal degrees (positive = north).
    @param lon      Longitude in decimal degrees (positive = east).
    @param forDate  The date to compute for (datetime.date); None = today.  The
                    retrospective search passes the searched date so the window
                    matches that day's sun times, not today's.
    """
    import math as _math

    if forDate is None:
        forDate = datetime.date.today()
    N = forDate.timetuple().tm_yday

    # Sun's mean longitude and anomaly
    Lsun = (280.460 + 0.9856474 * (N - 1)) % 360
    g    = _math.radians((357.528 + 0.9856003 * (N - 1)) % 360)

    # Ecliptic longitude
    lam = _math.radians(
        (Lsun + 1.915 * _math.sin(g) + 0.020 * _math.sin(2 * g)) % 360)

    # Declination
    sin_dec = _math.sin(_math.radians(23.439)) * _math.sin(lam)
    dec     = _math.asin(sin_dec)

    # Equation of time (minutes)
    f   = _math.radians((279.575 + 0.9856474 * (N - 1)) % 360)
    EqT = (-104.0 * _math.sin(f) + 596.0 * _math.cos(f)
           - 4.0  * _math.sin(2*f) + 0.5 * _math.cos(2*f)
           + 0.2  * _math.sin(3*f) + 0.8 * _math.cos(3*f)) / 60.0

    # Hour angle at horizon (solar elevation = -0.833° for refraction)
    cos_ha = (_math.sin(_math.radians(-0.833))
              - _math.sin(_math.radians(lat)) * sin_dec) / \
             (_math.cos(_math.radians(lat)) * _math.cos(dec))

    if cos_ha < -1 or cos_ha > 1:
        return None, None  # polar day or night

    ha_deg = _math.degrees(_math.acos(cos_ha))

    # Local timezone offset in hours (DST resolved for forDate's noon, not now,
    # so a search for a past date across a DST change still lines up).
    _refTs = time.mktime((forDate.year, forDate.month, forDate.day,
                          12, 0, 0, 0, 0, -1))
    if time.daylight and time.localtime(_refTs).tm_isdst:
        tz_hours = -time.altzone / 3600.0
    else:
        tz_hours = -time.timezone / 3600.0

    noon_min  = 720.0 - 4.0 * lon - EqT + tz_hours * 60.0
    return int(noon_min - ha_deg * 4.0), int(noon_min + ha_deg * 4.0)


_kEveryday = [0, 1, 2, 3, 4, 5, 6]
_kWeekdays = [0, 1, 2, 3, 4]
_kWeekend = [5, 6]
_kDayStrToInt = {'Mon':0, 'Tue':1, 'Wed':2, 'Thu':3, 'Fri':4, 'Sat':5, 'Sun':6}


def _dayListForSchedule(schedule):
    """The set of active weekday ints (Mon=0..Sun=6) for a schedule dict."""
    dayType = schedule.get('dayType', 'Every day')
    if dayType == "Every day":
        return _kEveryday
    if dayType == "Weekdays":
        return _kWeekdays
    if dayType == "Weekends":
        return _kWeekend
    return [_kDayStrToInt[d] for d in schedule.get('customDays', [])
            if d in _kDayStrToInt]


def _effectiveTimesForDate(schedule, localDate):
    """Resolve (startHour, startMin, stopHour, stopMin) for a schedule on a date.

    Fixed hours pass through; sunrise/sunset types are resolved with the stored
    lat/lon for localDate (+ offsets, clamped to a valid clock minute).  Falls
    back to the stored fixed hours if location is missing or polar.  This is the
    single implementation shared by the live path (_getEffectiveTimes, today) and
    the retrospective search (an arbitrary localDate).
    """
    startType = schedule.get('startType', 'fixed')
    stopType  = schedule.get('stopType',  'fixed')

    if startType == 'fixed' and stopType == 'fixed':
        return (schedule['startHour'], schedule['startMin'],
                schedule['stopHour'],  schedule['stopMin'])

    lat = schedule.get('latitude')
    lon = schedule.get('longitude')
    if lat is None or lon is None:
        return (schedule['startHour'], schedule['startMin'],
                schedule['stopHour'],  schedule['stopMin'])

    sunriseMin, sunsetMin = _calcSunriseSunsetMinutes(lat, lon, localDate)
    if sunriseMin is None:
        return (schedule['startHour'], schedule['startMin'],
                schedule['stopHour'],  schedule['stopMin'])

    refMin = {'sunrise': sunriseMin, 'sunset': sunsetMin}
    startTotal = refMin.get(startType,
                 schedule['startHour'] * 60 + schedule['startMin']) \
                 + schedule.get('startOffset', 0)
    stopTotal  = refMin.get(stopType,
                 schedule['stopHour']  * 60 + schedule['stopMin']) \
                 + schedule.get('stopOffset', 0)

    startTotal = max(0, min(1439, startTotal))
    stopTotal  = max(0, min(1439, stopTotal))
    return (startTotal // 60, startTotal % 60,
            stopTotal  // 60, stopTotal  % 60)


def getScheduleWindowsForDate(schedule, localDate):
    """Active schedule windows for a LOCAL calendar date, as epoch-ms intervals.

    Returns a list of (startMs, stopMs) tuples, replicating getScheduleInfo()'s
    semantics (day-of-week, is24Hours, fixed/sunrise/sunset, and the sunset->
    sunrise wrap) but for an arbitrary date -- so the retrospective search can
    match the live path.  An empty list means nothing is scheduled that day
    (drop all results); a single whole-day window means no effective time filter.

    @param  schedule   The rule schedule dict (RealTimeRule.getSchedule()).
    @param  localDate  A datetime.date (the searched day, local time).
    @return windows    List of (startMs, stopMs), or None if schedule is falsy.
    """
    if not schedule:
        return None

    dayList = _dayListForSchedule(schedule)
    weekday = localDate.weekday()
    dayStartMs = int(time.mktime(localDate.timetuple()) * 1000)
    dayEndMs = int(time.mktime(
        (localDate + datetime.timedelta(days=1)).timetuple()) * 1000)

    if schedule.get('is24Hours', True):
        return [(dayStartMs, dayEndMs)] if weekday in dayList else []

    startH, startM, stopH, stopM = _effectiveTimesForDate(schedule, localDate)
    startMs = dayStartMs + (startH * 60 + startM) * 60000
    stopMs  = dayStartMs + (stopH * 60 + stopM) * 60000
    wraps = (stopH < startH) or (stopH == startH and stopM < startM)

    windows = []
    if not wraps:
        if weekday in dayList:
            windows.append((startMs, stopMs))
    else:
        # sunset->sunrise: this day's own night start [start, midnight), plus the
        # tail of the previous scheduled day's night [midnight, stop).
        if weekday in dayList:
            windows.append((startMs, dayEndMs))
        if (weekday - 1) % 7 in dayList:
            windows.append((dayStartMs, stopMs))
    return windows


def scheduleCanRun(schedule):
    """Whether a schedule can ever fire.

    @return  False iff the schedule can never run -- a custom day set with no
             days selected, or a zero-length fixed time window; True otherwise.
    """
    if not _dayListForSchedule(schedule):
        return False                      # no days selected -> never runs
    if schedule.get('is24Hours', True):
        return True
    if (schedule.get('startType', 'fixed') == 'fixed' and
            schedule.get('stopType', 'fixed') == 'fixed' and
            schedule.get('startHour') == schedule.get('stopHour') and
            schedule.get('startMin') == schedule.get('stopMin')):
        return False                      # zero-length fixed window never fires
    return True


def scheduleIconState(schedule):
    """Clock-icon state for the Search screen's per-rule icon strip.

    @return  None  -> default schedule (24 hours, Every day): show no clock icon.
             True  -> custom schedule that can run: enabled clock.
             False -> custom schedule that can never run: disabled clock.
    """
    if not schedule:
        return None
    if schedule.get('is24Hours', True) and \
       schedule.get('dayType', 'Every day') == 'Every day':
        return None
    return scheduleCanRun(schedule)


##############################################################################
class RealTimeRule(object):
    """A class containing information about real time rules."""
    ###########################################################
    def __init__(self, queryName, cameraLocation):
        """RealTimeRule constructor.

        @param queryName       The name of the query this rule uses to search.
        @param cameraLocation  The location where the rule is active.
        """
        super(RealTimeRule, self).__init__()

        self._queryName = queryName
        self._cameraLocation = cameraLocation
        self._isEnabled = True
        self._schedule = _kDefaultRuleSchedule


    ###########################################################
    def getQueryName(self):
        """Get the name of the rule's query.

        @return name  The name of the rule's query.
        """
        return self._queryName


    ###########################################################
    def setQueryName(self, queryName):
        """Set the name of the rule's query.

        @param  name  The name of the rule's query.
        """
        self._queryName = queryName


    ###########################################################
    def getSchedule(self):
        """Get the rule's schedule.

        @return schedule  The rule's schedule.
        """
        return self._schedule


    ###########################################################
    def setSchedule(self, schedule):
        """Set the rule's schedule.

        @param  schedule  The rule's schedule.
        """
        self._schedule = schedule


    ###########################################################
    def getCameraLocation(self):
        """Get the rule's camera location.

        @return cameraLocation  The rule's cameraLocation.
        """
        return self._cameraLocation


    ###########################################################
    def setCameraLocation(self, cameraLocation):
        """Set the rule's camera location.

        @param  cameraLocation  The rule's camera location.
        """
        self._cameraLocation = cameraLocation


    ###########################################################
    def isEnabled(self):
        """Return whether the rule is enabled.

        @param  enabled  True if the rule is enabled.
        """
        return self._isEnabled


    ###########################################################
    def setEnabled(self, enabled=True):
        """Set whether the rule is enabled or not.

        @param  enabled  True if the rule should be enabled.
        """
        self._isEnabled = enabled


    ###########################################################
    def _getEffectiveTimes(self):
        """Return (startHour, startMin, stopHour, stopMin) for today.

        Resolves sunrise/sunset types using the stored lat/lon; falls back to
        the stored fixed hours if location is missing or polar conditions apply.
        Shares its implementation with the retrospective search via the
        module-level _effectiveTimesForDate().
        """
        return _effectiveTimesForDate(self._schedule, datetime.date.today())


    ###########################################################
    def getScheduleInfo(self):
        """Determine if the rule is currently scheduled and the next change.

        @param  isScheduled  True if the rule is currently scheduled to run.
        @param  nextChange   The time in seconds when the rule should change
                             state, or None if it will never change.
        """
        isScheduled = True
        nextChange = None

        now = datetime.datetime.today()
        nowDayInt = now.weekday()

        dayType = self._schedule['dayType']
        is24 = self._schedule['is24Hours']
        startHour, startMin, stopHour, stopMin = self._getEffectiveTimes()
        dateWraps = (stopHour < startHour) or \
                    (stopHour == startHour and stopMin < startMin)

        if dayType == "Every day":
            dayList = _kEveryday
        elif dayType == "Weekdays":
            dayList = _kWeekdays
        elif dayType == "Weekends":
            dayList = _kWeekend
        else:
            dayList = [_kDayStrToInt[dayStr] for
                       dayStr in self._schedule['customDays']]

        # 24 hours a day
        if is24:
            # Determine the next start or stop time, 12:00am
            nextChange = now.replace(hour=0, minute=0, second=0, microsecond=0)
            dayDelta = 1

            if len(dayList) == 7:
                # If we're active every day we never have a change.
                isScheduled, nextChange = True, None

            elif nowDayInt in dayList:
                # If we're active today find the next day we aren't.
                while (nowDayInt+dayDelta) % 7 in dayList:
                    dayDelta += 1
                isScheduled, nextChange = True, _datetimeToSeconds(nextChange +
                                                datetime.timedelta(dayDelta))
            else:
                # If we're not active find the next day we are.
                while (nowDayInt+dayDelta) % 7 not in dayList:
                    dayDelta += 1
                isScheduled, nextChange = False, _datetimeToSeconds(nextChange +
                                                 datetime.timedelta(dayDelta))

        # Specified times without wrapping.
        elif not dateWraps:
            # Determine if for any given day we would be before, in or after
            # the specified time range.
            beforeTimeRange = now.hour < startHour or \
                              (now.hour == startHour and now.minute < startMin)
            inTimeRange = not beforeTimeRange and ((now.hour < stopHour) or \
                          (now.hour == stopHour and now.minute < stopMin))
            afterTimeRange = (not beforeTimeRange and not inTimeRange)

            # Calculate the times we would start or stop if we were active or
            # inactive.
            activeNextChange = now.replace(hour=stopHour, minute=stopMin)
            inactiveNextChange = now.replace(hour=startHour, minute=startMin)

            if nowDayInt not in dayList or afterTimeRange:
                # After the scheduled time, return the next scheduled day
                # at the start time.
                dayDelta = 1
                while (nowDayInt+dayDelta) % 7 not in dayList:
                    dayDelta += 1
                isScheduled = False
                nextChange = _datetimeToSeconds(inactiveNextChange +
                                                datetime.timedelta(dayDelta))
            elif inTimeRange:
                # If we're in the time range, return the current day at the
                # stop time.
                isScheduled = True
                nextChange = _datetimeToSeconds(activeNextChange)
            else:
                # Else we're beforeTimeRange, return the current day at the
                # start time.
                isScheduled = False
                nextChange = _datetimeToSeconds(inactiveNextChange)

        # Specified times that continue into the following day.
        else:
            # Determine if our hour/minute falls inside the next day schedule,
            # the current day schedule, or always unscheduled.
            nextDayTimeRange = now.hour < stopHour or \
                               (now.hour == stopHour and now.minute < stopMin)
            notInTimeRange = not nextDayTimeRange and ((now.hour < startHour) \
                             or (now.hour == startHour and \
                                 now.minute < startMin))
            curDayTimeRange = not nextDayTimeRange and not notInTimeRange

            # Calculate the times we would start or stop if we were active or
            # inactive.
            activeNextChange = now.replace(hour=stopHour, minute=stopMin)
            inactiveNextChange = now.replace(hour=startHour, minute=startMin)

            if nowDayInt in dayList and curDayTimeRange:
                # If we're on a scheduled day and in the starting time range...
                isScheduled = True
                nextChange = _datetimeToSeconds(activeNextChange +
                                                datetime.timedelta(1))
            elif (nowDayInt-1)%7 in dayList and nextDayTimeRange:
                # We're on the day after a scheduled day and in the next day
                # time range...
                isScheduled = True
                nextChange = _datetimeToSeconds(activeNextChange)
            elif nowDayInt in dayList:
                # We're on a scheduled day in a non-scheduled time...
                isScheduled = False
                nextChange = _datetimeToSeconds(inactiveNextChange)
            else:
                # We're on a non scheduled day in a non-scheduled time...
                dayDelta = 1
                while (nowDayInt+dayDelta) % 7 not in dayList:
                    dayDelta += 1
                isScheduled = False
                nextChange = _datetimeToSeconds(inactiveNextChange +
                                                datetime.timedelta(dayDelta))

        return isScheduled, nextChange


    ###########################################################
    def getScheduleSummary(self, use12HourTime=True):
        """Retrieve a text string describing the rule schedule."""
        summaryStr = ''

        if not self.isEnabled():
            return "Disabled"

        # Add the days wording
        if self._schedule['dayType'] == "Custom...":
            summaryStr = ', '.join(self._schedule['customDays'])
        else:
            summaryStr = self._schedule['dayType']
        summaryStr += ' - '

        # Add the hour wording
        startType = self._schedule.get('startType', 'fixed')
        stopType  = self._schedule.get('stopType',  'fixed')
        isSolar = (startType in ('sunrise', 'sunset') or
                   stopType  in ('sunrise', 'sunset'))

        if self._schedule['is24Hours']:
            summaryStr += "24 hours"
        elif isSolar:
            def _solarLabel(sType, offset):
                label = sType.capitalize()
                if offset > 0:
                    label += "+%dmin" % offset
                elif offset < 0:
                    label += "%dmin" % offset
                return label
            summaryStr += _solarLabel(startType,
                                      self._schedule.get('startOffset', 0))
            summaryStr += " to "
            summaryStr += _solarLabel(stopType,
                                      self._schedule.get('stopOffset', 0))
        else:
            startHour = self._schedule['startHour']
            if use12HourTime:
                if startHour > 12:
                    startHour -= 12
                if startHour == 0:
                    startHour = 12
                summaryStr += str(startHour)
            else:
                summaryStr += ("%02d" % startHour)

            if self._schedule['startMin'] or not use12HourTime:
                summaryStr += ":%02i" % self._schedule['startMin']
            if use12HourTime:
                if self._schedule['startHour'] < 12:
                    summaryStr += ' am'
                else:
                    summaryStr += ' pm'
            summaryStr += ' to '

            stopHour = self._schedule['stopHour']
            if use12HourTime:
                if stopHour > 12:
                    stopHour -= 12
                if stopHour == 0:
                    stopHour = 12
                summaryStr += str(stopHour)
            else:
                summaryStr += ("%02d" % stopHour)

            if self._schedule['stopMin'] or not use12HourTime:
                summaryStr += ":%02i" % self._schedule['stopMin']

            if use12HourTime:
                if self._schedule['stopHour'] < 12:
                    summaryStr += ' am'
                else:
                    summaryStr += ' pm'

            if (self._schedule['startHour'] > self._schedule['stopHour']) or \
                   ((self._schedule['startHour'] == self._schedule['stopHour'])
                   and (self._schedule['startMin'] >= self._schedule['stopMin'])):
                summaryStr += ' the next day'

        return summaryStr


#####################################################################
def _datetimeToSeconds(datetimeObj):
    # Convert a datetime object to seconds since epoch
    return time.mktime(datetimeObj.timetuple())
