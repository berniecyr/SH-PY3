#!/usr/bin/env python

#*****************************************************************************
#
# WebRuleSearch.py
#     Rule-based search for the LAN record viewer (WebServer.py).
#
#     The desktop Search screen searches by *rule*: a saved query tree
#     (targets, regions/lines/doors, min-size, duration) plus the rule's
#     schedule.  The web viewer's own search (WebServer._searchObjects) is
#     hand-written SQL over the object DB and knows nothing about rules, so the
#     two screens returned genuinely different result sets for the same
#     footage.  This module closes that gap by driving the SAME engine the
#     desktop uses -- appCommon.SearchUtils.getSearchResults -- from inside the
#     web-server child process.
#
#     That engine is wx-free and already runs headlessly: NetworkMessageServer
#     does exactly this for its XML-RPC clients.  The front end proves the
#     read-only, out-of-process half: SearchResultsDataModel._delayedSearch
#     opens its own read-only ClipManager/DataManager and calls
#     getSearchResults directly.  We copy that pattern verbatim.
#
#     This lives in its own module so WebServer.py's import surface stays
#     stdlib-only.  WebServer imports it lazily, on the first rule search, so
#     the heavy dependencies pulled in by DataManager (cv2/numpy/PIL) never
#     load for a viewer session that only uses the field filters.
#
#     DO NOT import NetworkMessageServer from here.  It imports BackEndPrefs,
#     which is the one "import wx" under backEnd/ -- and there is no display in
#     the web-server process.  Nothing in the search stack needs it.
#
#*****************************************************************************

import datetime
import os
import pickle
import threading
import time
import traceback

# Module-level imports are deliberately limited to cheap ones: listRules is
# called on every page load, and RealTimeRule (datetime + time) is needed to
# unpickle a .rule anyway.  Everything expensive -- DataManager drags in
# PIL/cv2/numpy through ClipReader -- is imported inside the function that
# needs it, so only an actual rule search pays for it.
from appCommon.CommonStrings import kAnyCameraStr
from appCommon.CommonStrings import kQueryExt
from appCommon.CommonStrings import kRuleExt
from appCommon.CommonStrings import kSearchViewDefaultRules
from vitaToolbox.path.PathUtils import normalizePath

from .RealTimeRule import scheduleIconState


###############################################################################
# Constants
###############################################################################

# A rule search costs one pass per day (see runRuleSearch), so cap how wide a
# single request may reach.  The desktop searches exactly one day at a time.
kMaxSearchDays = 31

# One rule search at a time.  ThreadingHTTPServer runs handlers concurrently,
# each search opens its own sqlite handles and walks the object DB, and a few
# browser tabs should not be able to pile heavy searches onto a machine whose
# real job is recording.
_searchLock = threading.Lock()


###############################################################################
# Rule loading
#
# Rules are two pickles per rule in <userLocalDataDir>/rules/:
#   <name>.rule    a RealTimeRule        (camera, schedule, enabled)
#   <name>.query   a SavedQueryDataModel (what to match, plus responses)
# The back end owns that directory; we only ever read it.
###############################################################################

def _unpickle(path, logger, what):
    """ Load one pickle, logging and swallowing any failure.

    @return  The unpickled object, or None.
    """
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except Exception:
        logger.error("could not unpickle %s %s:\n%s"
                     % (what, path, traceback.format_exc()))
        return None


def _loadRule(ruleDir, name, logger):
    """ Load <name>.rule.  Returns the RealTimeRule, or None. """
    if not ruleDir:
        return None
    path = os.path.join(ruleDir, name + kRuleExt)
    if not os.path.isfile(path):
        return None
    return _unpickle(path, logger, "rule")


def _scheduleOf(rule):
    """ The schedule dict for an ALREADY-LOADED rule, or None.

    Split out so a caller that has the rule in hand (runRuleSearch needs it
    for the camera too) does not unpickle the same file a second time.
    """
    if rule is None:
        return None
    try:
        return rule.getSchedule()
    except Exception:
        return None


def loadSchedule(ruleDir, name, logger):
    """ The schedule dict for a saved rule, or None.

    Built-in rules have no rule file and therefore no schedule -- matching the
    desktop, where SearchView._doSearch only looks a schedule up when the
    selected rule is a named back-end rule.
    """
    return _scheduleOf(_loadRule(ruleDir, name, logger))


def listRules(ruleDir, logger):
    """ Every rule the Search screen offers that the back end owns.

    That is the seven built-ins plus every saved rule on disk.  The front end's
    "Custom searches" are deliberately absent: they live in the front-end prefs
    pickle (SearchView._getSavedSearches) and no back-end process can see them.

    @return  list of dicts: name, isDefault, camera, enabled, scheduleState
             (None = default schedule, True = custom schedule that can run,
             False = custom schedule that can never run -- the same tri-state
             the desktop draws its clock icon from).
    """
    rules = [{"name": name, "isDefault": True, "camera": kAnyCameraStr,
              "enabled": True, "scheduleState": None}
             for name in kSearchViewDefaultRules]

    if not ruleDir or not os.path.isdir(ruleDir):
        return rules

    # Mirrors NetworkMessageServer._getRuleNames, normalizePath included.
    try:
        fileNames = os.listdir(ruleDir)
    except Exception:
        logger.error("could not list rule dir %s:\n%s"
                     % (ruleDir, traceback.format_exc()))
        return rules

    saved = []
    for fileName in fileNames:
        fileName = normalizePath(fileName)
        name, ext = os.path.splitext(fileName)
        if ext != kRuleExt:
            continue
        rule = _unpickle(os.path.join(ruleDir, fileName), logger, "rule")
        if rule is None:
            continue                      # corrupt rule: skip, don't fail
        try:
            schedule = rule.getSchedule()
            saved.append({
                "name":          name,
                "isDefault":     False,
                "camera":        rule.getCameraLocation(),
                "enabled":       bool(rule.isEnabled()),
                "scheduleState": scheduleIconState(schedule),
            })
        except Exception:
            logger.error("unreadable rule %s:\n%s"
                         % (name, traceback.format_exc()))

    saved.sort(key=lambda r: r["name"].lower())
    return rules + saved


def loadQuery(ruleDir, name, dataMgr, logger):
    """ Turn a rule name into an executable query (a trigger tree).

    Same precedence the desktop uses in SearchView.OnRuleChoice and the back
    end uses in NetworkMessageServer._getSearchInfo: a built-in rule is
    synthesized, anything else is unpickled, upgraded, and made usable.

    @return  The usable query, or None if the rule cannot be loaded.
    """
    from .SavedQueryDataModel import convertOld2NewSavedQueryDataModel
    from .triggers.TargetTrigger import getQueryForDefaultRule

    query = getQueryForDefaultRule(dataMgr, name)
    if query is not None:
        return query

    if not ruleDir:
        return None
    path = os.path.join(ruleDir, name + kQueryExt)
    if not os.path.isfile(path):
        logger.error("query %s doesn't exist" % name)
        return None

    queryModel = _unpickle(path, logger, "query")
    if queryModel is None:
        return None

    try:
        # An older query may have no coordinate space; upgrade it in memory
        # exactly as NetworkMessageServer._getQuery does.  We never write back.
        convertOld2NewSavedQueryDataModel(dataMgr, queryModel)
        return queryModel.getUsableQuery(dataMgr)
    except Exception:
        logger.error("could not build usable query for %s:\n%s"
                     % (name, traceback.format_exc()))
        return None


###############################################################################
# Search
###############################################################################

def _noFlush(_camName):
    """ Stand-in for the front end's BackEndClient.flushVideo.

    getSearchResults only consults flushFunc when searching *today*, and only
    to mark clips the recorder has not finished writing yet.  Returning (0, 0)
    makes SearchUtils' "stop <= realMaxTaggedMs" test unsatisfiable, so every
    clip falls through to getClipCoverage -- i.e. exactly the behaviour of not
    flushing at all.  The web server has no synchronous channel to the back
    end, and the only cost is that footage recorded in the last few seconds of
    today may read as not-yet-available.
    """
    return (0, 0)


def _localDates(startMs, endMs):
    """ The local calendar dates spanned by [startMs, endMs], inclusive. """
    firstDate = datetime.date.fromtimestamp(startMs / 1000.0)
    lastDate = datetime.date.fromtimestamp(endMs / 1000.0)
    dates = []
    day = firstDate
    while day <= lastDate:
        dates.append(day)
        day += datetime.timedelta(1)
    return dates


def runRuleSearch(ctx, ruleName, camera, startMs, endMs):
    """ Search recorded footage with a Search-screen rule.

    @param  ctx       Anything carrying logger/ruleDir/videoDir/clipDbPath/
                      objDbPath -- WebServer's _Context, or a stub for tests.
    @param  ruleName  A built-in or saved rule name (see listRules).
    @param  camera    A single camera to search, or None/"" for every camera.
    @param  startMs   Range start, epoch ms.
    @param  endMs     Range end, epoch ms.
    @return (clips, error).  clips is a list of MatchingClipInfo sorted newest
            first; error is None, or a message to show the user (clips empty).
    """
    from appCommon.SearchUtils import SearchConfig
    from appCommon.SearchUtils import getSearchResults

    from .ClipManager import ClipManager
    from .DataManager import DataManager

    logger = ctx.logger

    # Everything down to the search lock returns (clips, error) like the rest
    # of this function.  It used to run bare, so a caller that passed a
    # negative startMs -- which the web layer accepted until it was bounded --
    # got an OSError straight out of runRuleSearch instead: on Windows
    # datetime.date.fromtimestamp refuses any time before the epoch, a quirk
    # SearchView._isDateBold documents too.
    try:
        endMs = min(int(endMs), int(time.time() * 1000))
        startMs = int(startMs)
        if startMs > endMs:
            return [], "The start of the range is after the end."

        dates = _localDates(startMs, endMs)
    except Exception:
        logger.error("unusable search range %r-%r for rule %s:\n%s"
                     % (startMs, endMs, ruleName, traceback.format_exc()))
        return [], "That date range cannot be searched."

    if len(dates) > kMaxSearchDays:
        return [], ("Rule searches cover at most %d days at a time; "
                    "narrow the date range." % kMaxSearchDays)

    # Cameras: mirror SearchView.OnRuleChoice.  An explicit choice still wins,
    # so a rule can be run against a camera it was not configured for.  But
    # when the user leaves the picker on "any", the SAVED RULE'S OWN camera is
    # what runs -- the desktop does this in OnRuleChoice:
    #
    #     if camera == kAnyCameraStr:
    #         self._searchCamLoc = query.getVideoSource().getLocationName()
    #
    # That branch was missing here, and "any" expanded to every camera in the
    # system instead, so a rule explicitly scoped to one camera reported
    # matches from all of them.  Only built-in rules (People, Vehicles, ...)
    # own no camera, and those correctly keep expanding to everything.
    #
    # Read from the .rule file rather than the query's video source because
    # loadQuery returns the *usable* query, which no longer carries one; the
    # two agree, and getCameraLocation() is already what listRules reports to
    # the picker, so the search now matches what the UI says the rule covers.
    # A saved rule that happens to share a built-in's name must not lend it a
    # camera or a schedule.  loadQuery already gives the built-in precedence
    # (getQueryForDefaultRule is consulted before ruleDir is touched), and the
    # desktop does the same a level up: SearchView.OnRuleChoice clears
    # _searchRuleName as soon as a name resolves to a built-in, so _doSearch
    # never looks a schedule up for one.  Reading <built-in>.rule here was the
    # one place that diverged.
    savedRule = None
    if ruleName not in kSearchViewDefaultRules:
        savedRule = _loadRule(ctx.ruleDir, ruleName, logger)
    ruleCamera = None
    if savedRule is not None:
        try:
            ruleCamera = savedRule.getCameraLocation()
        except Exception:
            logger.error("could not read the camera for rule %s:\n%s"
                         % (ruleName, traceback.format_exc()))

    try:
        if camera and camera != kAnyCameraStr:
            cameraList = [camera]
        elif ruleCamera and ruleCamera != kAnyCameraStr:
            cameraList = [ruleCamera]
            logger.info("rule %s is scoped to camera %s; searching only that "
                        "one" % (ruleName, ruleCamera))
        else:
            # _facets opens the databases, which this function treats as
            # fallible everywhere else; it was the one call here that could
            # raise past the contract.
            from .WebServer import _facets
            cameraList = _facets(ctx)["cameras"]
    except Exception:
        logger.error("could not resolve the cameras for rule %s:\n%s"
                     % (ruleName, traceback.format_exc()))
        return [], "The search failed; see WebServer.log."
    if not cameraList:
        return [], "No cameras have recorded footage yet."

    with _searchLock:
        clipMgr = None
        dataMgr = None
        try:
            # Read-only, so we take only shared locks and never journal
            # concurrently with the recording back end.  Same as the front
            # end's SearchResultsDataModel._delayedSearch.
            clipMgr = ClipManager(logger)
            clipMgr.open(ctx.clipDbPath, readOnly=True)
            dataMgr = DataManager(logger, clipMgr, ctx.videoDir)
            dataMgr.open(ctx.objDbPath, readOnly=True)

            query = loadQuery(ctx.ruleDir, ruleName, dataMgr, logger)
            if query is None:
                return [], "Rule '%s' could not be loaded." % ruleName
            schedule = _scheduleOf(savedRule)

            # One pass per day.  getSearchResults takes a single date because
            # the schedule filter is defined per day (a sunset->sunrise rule
            # wraps midnight), so looping days reuses the desktop's exact code
            # path -- including RealTimeRule.getScheduleWindowsForDate -- and
            # needs no change to SearchUtils.
            searchConfig = SearchConfig()
            clips = []
            seen = set()
            for day in dates:
                _, dayClips = getSearchResults(
                    query, cameraList, day, dataMgr, clipMgr, searchConfig,
                    _noFlush, None, None, schedule)
                for clip in dayClips:
                    # getSearchTimes pads each day with slop so an event
                    # crossing midnight shows up on both days; de-dup it.
                    #
                    # Key on the REAL event bounds, not the padded
                    # startTime/stopTime.  SearchUtils._combineOverlappingClips
                    # rewrites the padded pair to stop one clip's lead-in from
                    # running into its neighbour's lead-out, and it decides
                    # that against whatever else is in the SAME day's results
                    # -- which differs between the day-N pass (all of day N)
                    # and the day-N+1 pass (only the last few minutes of day N,
                    # through the slop window).  The same clip can therefore
                    # come back with two different padded pairs and slip past a
                    # dedup keyed on them.  _applyScheduleFilter in that same
                    # module reads the real bounds for exactly this reason.
                    realStart = getattr(clip, "_realStartTime", clip.startTime)
                    realStop = getattr(clip, "_realStopTime", clip.stopTime)
                    key = (clip.camLoc, realStart, realStop)
                    if key in seen:
                        continue
                    seen.add(key)
                    clips.append(clip)

            # Drop anything outside the requested range: we searched whole
            # days, but the user asked for a time range within them.
            clips = [c for c in clips
                     if c.stopTime >= startMs and c.startTime <= endMs]
            clips.sort(key=lambda c: c.startTime, reverse=True)
            return clips, None

        except Exception:
            logger.error("rule search failed for '%s':\n%s"
                         % (ruleName, traceback.format_exc()))
            return [], "The search failed; see WebServer.log."
        finally:
            for mgr in (dataMgr, clipMgr):
                if mgr is not None:
                    try:
                        mgr.close()
                    except Exception:
                        pass
