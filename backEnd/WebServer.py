#*****************************************************************************
#
# WebServer.py
#   LAN-only record viewer for Sighthound Video (Python 3 rebuild).
#
#   This replaces the original nginx + XNAT (NAT-traversal) + XML/RPC-bridge
#   remote-access stack with a single, self-contained pure-Python HTTP server.
#   It runs in its own child process (spawned by BackEndProcessJumper) and:
#
#     * binds all local interfaces so devices ON THE LAN can reach it, but
#       never opens a router port / tunnel -- there is no internet exposure
#       (no port opener, no XNAT, no cloud).  "LAN only" by construction.
#     * requires a login (the username/password configured under
#       Tools -> Options -> Remote Access), issuing an HttpOnly session cookie.
#     * serves a record viewer that searches the detection database by any
#       field or combination (camera, object type, sub-type, face name,
#       gender, age, nudity, confidence, time range) and plays the matching
#       recorded clip.
#
#   Data is read straight from the same SQLite databases the back end writes
#   (object DB + clip DB), opened READ-ONLY per request so we never interfere
#   with the running back end.  Recorded clips are streamed from disk with
#   HTTP range support; per-detection thumbnails are extracted on demand with
#   the bundled ffmpeg and cached.
#
#   Public module API preserved for the rest of the app:
#       REALM, make_auth(), user_from_auth(), is_basic_auth(),
#       killWebServerProcesses(), runWebServer(), runNginx()
#
#*****************************************************************************
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
#*****************************************************************************

import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import ntpath
import os
import pathlib
import posixpath
import pickle
import queue
import random
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vitaToolbox.loggingUtils.LoggingUtils import getLogger
from . import MessageIds


###############################################################################
# Constants
###############################################################################

# realm, kept for compatibility with the credential expressions produced by
# NetworkMessageServer (which imports REALM from this module).
REALM = "Sighthound"

_kLogSize        = 1024 * 1024 * 10       # WebServer.log rotation size
_kWebServerLog   = "WebServer.log"
_kStatusFile     = "status"               # matches CommonStrings.kStatusFile
_kThumbCacheDir  = "thumbcache"
_kClipCacheDir   = "clipcache"            # browser-playable copies of HEVC
_kClipCacheMax   = 4 * 1024 * 1024 * 1024  # prune the clip cache above this
_kTranscodeSecs  = 300                    # ffmpeg timeout for one clip
_kTranscodeHeight = 720                   # downscale target for H.264
_kTranscodeCrf   = "28"                   # measured: 59 s of 4K -> ~15 MB
_kTranscodePreset = "veryfast"            # measured: ~6.6 s for a 59 s clip

# Hardware transcode.  BOTH halves have to stay on the GPU to be worth it --
# measured on this machine under real recording load, on one 23 MB 4K HEVC
# clip:
#     software decode + libx264         49.5 s
#     d3d11va decode  + libx264         94.7 s   (worse: frames copied back)
#     cuda decode     + libx264         49.5 s   (no gain: decode is the cost)
#     cuda decode + scale_cuda + nvenc   6.0 s   <- this one
# Tried once; if it fails, _hwTranscodeOk latches False and every later
# conversion goes straight to software.
_kHwDecodeArgs = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
_kHwEncodeArgs = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30"]

# None = not tried yet, True = works here, False = fall back to software.
_hwTranscodeOk = None
_kThumbCacheMax  = 800                    # max cached thumbnails before prune
_kSessionTtlSecs = 12 * 60 * 60           # login session lifetime
_kPingSecs       = 60                     # liveness ping to the back end
_kLoginMaxFails  = 6                      # failed logins per IP before lockout
_kLoginLockSecs  = 60                     # lockout duration after too many fails
_kSearchMaxLimit = 500                    # hard cap on rows per search
_kThumbTtlSecs   = 6 * 60 * 60            # re-extract a cached thumb after this
_kFreshClipSecs  = 150                    # below this a clip may still be settling

# Where the static front-end files live (shipped next to this module).
_kWebRootDir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "webroot")

# Numeric query parameters are bounded before they reach sqlite.  Three
# separate values used to escape as a 500 rather than being ignored like any
# other unusable filter:
#   "1e999"  -> float infinity, which int() refuses (OverflowError)
#   "-1e999" -> the same, negative
#   "1e308"  -> a FINITE float that converts to a Python int fine, and which
#               sqlite then refuses to bind (also OverflowError, raised from
#               execute() and so not covered by the DatabaseError guard)
# 2**62 is far past any real timestamp and safely inside sqlite's signed
# 64-bit INTEGER.
_kMaxNumber = 2 ** 62

# Object types the detector emits; used to validate the search "type" filter.
_kKnownTypes = ("person", "vehicle", "animal", "unknown", "object")


###############################################################################
# Authentication expression helpers  (preserved public API)
###############################################################################

def make_auth(user, passw, realm=None):
    """ Creates an authentication expression used to configure/verify access.

    Basic form:  "user:{SHA}<base64(sha1(pass))>"
    Digest form: "user:realm:<md5(user:realm:pass)>"

    @param user   The user name.
    @param passw  The password.
    @param realm  The realm for digest auth, or None for basic auth.
    @return       The single-line expression.
    """
    esc = lambda x: x.replace(':', '::')
    if realm is not None:
        ldg = lambda x: hashlib.md5(':'.join(x).encode('utf-8')).hexdigest()
        return "%s:%s:%s" % (esc(user), esc(realm), ldg((user, realm, passw)))
    md = hashlib.sha1()
    md.update(passw.encode('utf-8'))
    return "%s:{SHA}%s" % (esc(user),
                           base64.b64encode(md.digest()).decode('ascii'))


def user_from_auth(auth):
    """ Extracts the user name from an auth expression (basic or digest). """
    if not auth:
        return ""
    c = len(auth)
    i = 0
    c -= 1
    while i <= c:
        i = auth.find(":", i)
        if -1 == i:
            return ""
        if i < c and ':' == auth[i + 1]:
            i += 2
            continue
        break
    if i > c:
        return ""
    return auth[0:i].replace('::', ':')


def is_basic_auth(auth):
    """ True if the expression is a basic-auth (SHA) expression. """
    return auth.find(':{SHA}') == (len(auth) - 6 - 28)


def _verify_credentials(storedAuth, user, passw):
    """ Constant-time check of a submitted user/password against the stored
    auth expression (whichever scheme it uses).

    @return True if the credentials match the configured account.
    """
    if not storedAuth or not user:
        return False
    try:
        if is_basic_auth(storedAuth):
            calc = make_auth(user, passw)
        else:
            calc = make_auth(user, passw, REALM)
    except Exception:
        return False
    return hmac.compare_digest(calc, storedAuth)


###############################################################################
# Legacy entry points retained so existing imports keep resolving
###############################################################################

def killWebServerProcesses(logger):
    """ Legacy hook.  The rebuilt server runs entirely inside this child
    process (no forked nginx/XNAT), so there is nothing external to kill; the
    back end terminates the process directly.  Returns True for compatibility.
    """
    try:
        logger.info("killWebServerProcesses: LAN server is in-process, no-op")
    except Exception:
        pass
    return True


def runNginx(*args):
    """ Legacy nginx shared-library entry point.  Retained only so
    FrontEndLaunchpad's '--webserver' import resolves; the rebuilt server does
    not use nginx, so this must never actually be invoked.
    """
    sys.stderr.write("runNginx: nginx path is retired in the LAN rebuild\n")
    sys.exit(0)


###############################################################################
# Read-only database access
###############################################################################

def _connectRo(dbPath):
    """ Open a fresh READ-ONLY sqlite connection to a back-end database.

    Read-only + short busy timeout means we can query the live databases the
    back end is actively writing without ever blocking or corrupting them.
    """
    uri = pathlib.Path(dbPath).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=3000")
    except Exception:
        pass
    return conn


def _hasTravelColumns(ctx):
    """ -> True if objdb2 has the centroid columns the travel filter needs.

    Cached on the context after the first look: this connection never runs the
    schema upgrade (it is read-only), so the answer only changes when the back
    end migrates, and re-probing on every request would be a needless query.
    A False here means the back end has not been restarted since the upgrade.
    """
    cached = getattr(ctx, "_travelCols", None)
    if cached is not None:
        return cached
    ok = False
    try:
        conn = _connectRo(ctx.objDbPath)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(objects)")]
            ok = "maxCx" in cols
        finally:
            conn.close()
    except Exception:
        ok = False
    # Only cache a positive: a negative may just mean "not migrated yet", and we
    # want that to start working as soon as the back end has upgraded.
    if ok:
        ctx._travelCols = True
    return ok


###############################################################################
# Shared server context
###############################################################################

class _Context(object):
    """ Everything a request handler needs, shared across handler threads. """

    def __init__(self, logger, webDir, videoDir, clipDbPath, objDbPath, auth,
                 ruleDir=None):
        self.logger      = logger
        self.webDir      = webDir
        self.videoDir    = videoDir
        self.clipDbPath  = clipDbPath
        self.objDbPath   = objDbPath
        # <userLocalDataDir>/rules -- the saved rule/query pickles the desktop
        # Search screen searches by.  Passed in rather than derived from
        # webDir, which SIGHTHOUND_WEBDIR can relocate.  None disables
        # rule search (built-in rules still work).
        self.ruleDir     = ruleDir
        self._auth       = auth
        self._authLock   = threading.Lock()
        self._sessions   = {}          # token -> (user, expiryEpoch)
        self._sessLock   = threading.Lock()
        self._failLock   = threading.Lock()
        self._failByIp   = {}          # ip -> (count, lockUntilEpoch)
        self.thumbDir    = os.path.join(webDir, _kThumbCacheDir)
        self.clipCacheDir = os.path.join(webDir, _kClipCacheDir)
        for d in (self.thumbDir, self.clipCacheDir):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        # One conversion at a time, process-wide.  These are 4K sources and
        # this machine is also recording seventeen cameras: letting a page of
        # HEVC results start a transcode per result would starve the
        # recorder.  Holders re-check the cache after acquiring it, so the
        # second request for the same clip waits and then gets the cached
        # file rather than doing the work again.
        self.convertLock = threading.Lock()

    # -- auth / sessions -------------------------------------------------

    def getAuth(self):
        with self._authLock:
            return self._auth

    def setAuth(self, auth):
        with self._authLock:
            self._auth = auth
        # Changing credentials invalidates existing logins.
        with self._sessLock:
            self._sessions.clear()

    def login(self, user, passw):
        """ Verify credentials; on success return a new session token. """
        self.sweep()
        if not _verify_credentials(self.getAuth(), user, passw):
            return None
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with self._sessLock:
            self._sessions[token] = (user, time.time() + _kSessionTtlSecs)
        return token

    def sessionUser(self, token):
        """ Return the user for a valid, unexpired token, else None. """
        if not token:
            return None
        with self._sessLock:
            rec = self._sessions.get(token)
            if not rec:
                return None
            user, expiry = rec
            if time.time() > expiry:
                self._sessions.pop(token, None)
                return None
            return user

    def logout(self, token):
        with self._sessLock:
            self._sessions.pop(token, None)

    # -- brute-force throttling -----------------------------------------

    def loginLocked(self, ip):
        with self._failLock:
            rec = self._failByIp.get(ip)
            if not rec:
                return False
            count, lockUntil = rec
            return count >= _kLoginMaxFails and time.time() < lockUntil

    def noteLoginResult(self, ip, ok):
        with self._failLock:
            if ok:
                self._failByIp.pop(ip, None)
                return
            now = time.time()
            count, lockUntil = self._failByIp.get(ip, (0, 0))
            # A lapsed lockout wipes the slate.  The count used only ever to
            # grow, so once an address had reached _kLoginMaxFails every
            # single later mistake re-locked it for another
            # _kLoginLockSecs -- for the life of the process.  Now the cap
            # means "_kLoginMaxFails failures within a rolling window", and a
            # quiet _kLoginLockSecs clears it.
            if now >= lockUntil:
                count = 0
            self._failByIp[ip] = (count + 1, now + _kLoginLockSecs)

    def sweep(self):
        """ Drop expired sessions and lapsed failure records.

        Both dicts only ever lost an entry when that exact token or address
        came back, so an abandoned session and a one-off failure from a
        transient address stayed for the life of the process.  Called from
        login(), which is rare enough to carry the scan.
        """
        now = time.time()
        with self._sessLock:
            for token in [t for t, (_u, exp) in self._sessions.items()
                          if now > exp]:
                self._sessions.pop(token, None)
        with self._failLock:
            for ip in [i for i, (_c, until) in self._failByIp.items()
                       if now >= until]:
                self._failByIp.pop(ip, None)


###############################################################################
# Search + media helpers
###############################################################################

def _boundedNumber(text, cast=int, low=None, high=None):
    """ Parse a query-string number, or None if it is not usable.

    Catches OverflowError alongside ValueError, and rejects NaN and
    out-of-range values, so an unusable number is ignored the way every other
    unusable filter is rather than escaping as an internal error.  See
    _kMaxNumber for the three inputs that used to get through.

    @param  text  The raw parameter value.
    @param  cast  int or float.
    @param  low   Smallest acceptable value, or None.
    @param  high  Largest acceptable value, or None.
    @return the number, or None if it cannot be used.
    """
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    try:
        value = cast(value)
    except (ValueError, OverflowError):
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        return None
    return value


def _searchObjects(ctx, params):
    """ Query the object database for detections matching the filters.

    @param  params  Parsed query-string dict (values are lists from parse_qs).
    @return list of result dicts (most recent first).
    """
    def one(name, default=None):
        v = params.get(name)
        if not v:
            return default
        v = v[0].strip()
        return v if v != "" else default

    where = []
    args = []

    cam = one("camera")
    if cam:
        where.append("o.camLoc = ?")
        args.append(cam)

    otype = one("type")
    if otype and otype.lower() in _kKnownTypes:
        where.append("o.type = ?")
        args.append(otype.lower())

    startMs = one("startMs")
    endMs = one("endMs")
    # Convert FIRST, append only if it worked.  Appending the clause before
    # the conversion that can raise left a placeholder with no bound value,
    # so sqlite rejected the whole statement ("Incorrect number of bindings")
    # -- caught below as a DatabaseError and degraded to an empty result set.
    # A mistyped date therefore read as "nothing happened" rather than as an
    # ignored filter.  Timestamps are floored at 0: a negative one is not a
    # time anybody can have recorded, and it breaks date handling downstream
    # (datetime.date.fromtimestamp raises on Windows for anything negative).
    value = _boundedNumber(startMs, int, 0, _kMaxNumber) if startMs else None
    if value is not None:
        where.append("o.timeStop >= ?")
        args.append(value)
    value = _boundedNumber(endMs, int, 0, _kMaxNumber) if endMs else None
    if value is not None:
        where.append("o.timeStart <= ?")
        args.append(value)

    minConf = one("minConf")
    if minConf:
        value = _boundedNumber(minConf, float, -_kMaxNumber, _kMaxNumber)
        if value is not None:
            where.append("o.confidence >= ?")
            args.append(value)

    faceName = one("faceName")
    if faceName:
        where.append("a.faceName LIKE ?")
        args.append("%" + faceName + "%")

    if one("hasFace") in ("1", "true", "yes"):
        where.append("a.faceName IS NOT NULL AND a.faceName <> ''")

    gender = one("gender")
    if gender in ("M", "F"):
        where.append("a.gender = ?")
        args.append(gender)

    ageMin = one("ageMin")
    if ageMin:
        value = _boundedNumber(ageMin, int, -_kMaxNumber, _kMaxNumber)
        if value is not None:
            where.append("a.age >= ?")
            args.append(value)
    ageMax = one("ageMax")
    if ageMax:
        value = _boundedNumber(ageMax, int, -_kMaxNumber, _kMaxNumber)
        if value is not None:
            where.append("a.age <= ?")
            args.append(value)

    subType = one("subType")
    if subType:
        where.append("a.subType LIKE ?")
        args.append("%" + subType + "%")

    if one("nudity") in ("1", "true", "yes"):
        where.append("a.nudity = 1")

    # Travel: how far the detection's centre moved over its life, in px at
    # 1280x720 -- the same units the desktop search filter and the camera setting
    # use, so a value that works there can be pasted into a URL here.  Stored in
    # analysis pixels, hence the halving for the usual 640x360 camera.
    #
    # Guarded on the column actually existing: this connection is read-only and
    # never runs the schema upgrade, so a viewer can be looking at a database the
    # back end has not migrated yet.  Filtering on a missing column would throw
    # and the caller degrades that to "no results" -- an empty page that looks
    # like a quiet night rather than an un-upgraded database.
    minTravel = one("minTravel")
    travelOk = _hasTravelColumns(ctx)
    if minTravel and travelOk:
        # round(inf) raises OverflowError, so this needs the same guard as
        # every other numeric filter; the append-then-pop dance goes with it.
        value = _boundedNumber(minTravel, float, -_kMaxNumber, _kMaxNumber)
        if value is not None:
            where.append("(o.maxCx >= 0 AND "
                         "((o.maxCx - o.minCx) + (o.maxCy - o.minCy)) >= ?)")
            args.append(int(round(value / 2.0)))

    # Free-text: match across the common textual columns.
    q = one("q")
    if q:
        like = "%" + q + "%"
        where.append("(o.camLoc LIKE ? OR o.type LIKE ? OR a.faceName LIKE ? "
                     "OR a.subType LIKE ?)")
        args.extend([like, like, like, like])

    try:
        limit = min(_kSearchMaxLimit, max(1, int(one("limit", "100"))))
    except ValueError:
        limit = 100
    try:
        offset = max(0, int(one("offset", "0")))
    except ValueError:
        offset = 0

    whereSql = ("WHERE " + " AND ".join(where)) if where else ""
    # Travel comes back doubled, i.e. in 1280x720 reference units, so every
    # surface that shows this number agrees.  NULL when the DB predates the
    # columns, which the row builder renders as "no value" rather than 0.
    travelSel = ("((o.maxCx - o.minCx) + (o.maxCy - o.minCy)) * 2"
                 if travelOk else "NULL")
    sql = (
        "SELECT o.uid, o.camLoc, o.timeStart, o.timeStop, o.type, "
        "o.confidence, a.faceName, a.faceConf, a.faceDetConf, a.gender, "
        "a.age, a.subType, a.detConf, a.nudity, a.nudityDetail, "
        "%s, %s "
        "FROM objects o LEFT JOIN objectAttributes a ON o.uid = a.objUid "
        "%s ORDER BY o.timeStart DESC LIMIT ? OFFSET ?"
        % (travelSel, "o.maxCx" if travelOk else "NULL", whereSql)
    )

    # The open is inside the guard too.  It used to sit outside, so a
    # database that was momentarily unopenable (first run, mid-reset, a
    # transient lock) raised straight past all of this careful degradation
    # and came back as a bare 500.
    try:
        conn = _connectRo(ctx.objDbPath)
    except Exception as e:
        ctx.logger.warning("objdb2 open failed: %s" % e)
        rows = []
    else:
        try:
            try:
                rows = conn.execute(sql, args + [limit, offset]).fetchall()
            except sqlite3.DatabaseError as e:
                # Don't let one unreadable page take down the whole results
                # list -- degrade to "no results" (the UI handles zero).
                ctx.logger.warning("objdb2 search query failed: %s" % e)
                rows = []
        finally:
            conn.close()

    results = []
    for r in rows:
        results.append({
            "id":           r["uid"],
            "camera":       r["camLoc"],
            "timeStart":    r["timeStart"],
            "timeStop":     r["timeStop"],
            "type":         r["type"],
            "confidence":   r["confidence"],
            "faceName":     r["faceName"],
            "faceConf":     r["faceConf"],
            "faceDetConf":  r["faceDetConf"],
            "gender":       r["gender"],
            "age":          r["age"],
            "subType":      r["subType"],
            "detConf":      r["detConf"],
            "nudity":       bool(r["nudity"]),
            "nudityDetail": r["nudityDetail"],
            # None (not 0) when this database has no travel columns yet, or the
            # object has no motion rows to measure -- "unknown" and "did not
            # move" are different answers and the UI shows them differently.
            "travel":       _travelOf(r),
        })
    return results


###############################################################################
def _travelOf(row):
    """-> travel in 1280x720 px for a result row, or None if not measurable."""
    try:
        travel, maxCx = row[15], row[16]
    except (IndexError, KeyError):
        return None
    if travel is None or maxCx is None or maxCx < 0:
        return None
    return int(travel)


def _isBareName(name):
    """ -> True if `name` is a single filename component, not a path.

    Used to keep a caller-supplied rule name from escaping the rule directory
    once WebRuleSearch concatenates an extension onto it and unpickles the
    result.  Checked with ntpath as well as the native module so the rule
    holds even if this ever runs somewhere "C:evil" or "a\\b" would not
    otherwise read as a path.
    """
    if not name or name in (".", ".."):
        return False
    for mod in (os.path, ntpath, posixpath):
        if mod.basename(name) != name:
            return False
        if mod.isabs(name):
            return False
    # "C:evil" is drive-relative: basename() leaves it alone on posix but it
    # is still not a bare name on Windows.
    if ntpath.splitdrive(name)[0]:
        return False
    return True


###############################################################################
# Rule search
#
# The other half of search: instead of the ad-hoc field filters above, run one
# of the Search screen's rules.  The evaluation itself lives in WebRuleSearch
# (imported lazily -- it pulls in DataManager, and with it cv2/numpy/PIL); the
# job here is to turn a MatchingClipInfo into the same row dict _searchObjects
# produces, so the browser renders both kinds of result identically.
###############################################################################

# Fields _searchObjects returns that a clip-level rule result has no value for
# until we look its detections up.
_kEmptyDetectionFields = {
    "type": None, "subType": None, "confidence": None, "detConf": None,
    "faceName": None, "faceConf": None, "faceDetConf": None,
    "gender": None, "age": None, "nudity": False, "nudityDetail": None,
}

# Filled in separately from the fields above: travel is computed from columns
# rather than selected straight, and is absent on databases the back end has not
# migrated yet, so it needs its own guard.
_kTravelField = "travel"


def _annotateRuleRows(ctx, rows):
    """ Fill in detection detail for rule results, in one batched query.

    A rule result names the objects that matched (objList); we show the first
    one's type/face/etc so the cards read the same as filter results.  Done as
    a single IN query per page rather than per row.
    """
    ids = [r["id"] for r in rows if r["id"] is not None]
    if not ids:
        return
    byId = {}
    # Same reasoning as the search query: only reference the centroid columns
    # when they exist, so an un-migrated database degrades to "no travel shown"
    # rather than failing the whole detail lookup.
    travelOk = _hasTravelColumns(ctx)
    travelSel = ("((o.maxCx - o.minCx) + (o.maxCy - o.minCy)) * 2"
                 if travelOk else "NULL")
    maxCxSel = "o.maxCx" if travelOk else "NULL"
    try:
        conn = _connectRo(ctx.objDbPath)
    except Exception:
        return
    try:
        # Chunked to stay well under SQLite's bound-variable limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rowsIn = conn.execute(
                "SELECT o.uid, o.type, o.confidence, a.faceName, a.faceConf, "
                "a.faceDetConf, a.gender, a.age, a.subType, a.detConf, "
                "a.nudity, a.nudityDetail, %s AS travel, %s AS maxCxOk "
                "FROM objects o "
                "LEFT JOIN objectAttributes a ON o.uid = a.objUid "
                "WHERE o.uid IN (%s)"
                % (travelSel, maxCxSel, ",".join("?" * len(chunk))), chunk)
            for row in rowsIn:
                byId[row["uid"]] = row
    except Exception:
        # Detail is a nicety; a corrupt page must not lose the results.
        ctx.logger.warning("rule result detail lookup failed:\n%s"
                           % traceback.format_exc())
        return
    finally:
        conn.close()

    for r in rows:
        row = byId.get(r["id"])
        if row is None:
            continue
        for key in _kEmptyDetectionFields:
            r[key] = row[key]
        r["nudity"] = bool(row["nudity"])
        travel, maxCx = row["travel"], row["maxCxOk"]
        r[_kTravelField] = (None if travel is None or maxCx is None or maxCx < 0
                            else int(travel))


def _ruleRows(ctx, clips):
    """ MatchingClipInfo list -> row dicts shaped like _searchObjects output. """
    rows = []
    for clip in clips:
        objList = list(getattr(clip, "objList", None) or [])
        row = {
            "id":        objList[0] if objList else None,
            "camera":    clip.camLoc,
            "timeStart": clip.startTime,
            "timeStop":  clip.stopTime,
            # Rule results are clips, not detections, so playback and
            # thumbnails key off (camera, time) when there is no object id.
            "previewMs": clip.previewMs,
            "objCount":  len(objList),
            "hasClip":   bool(clip.hasFootage),
        }
        row.update(_kEmptyDetectionFields)
        rows.append(row)
    _annotateRuleRows(ctx, rows)
    return rows


def _searchByRule(ctx, params, one):
    """ Run a Search-screen rule over the requested range.

    @param  one  The single-value param accessor from _apiSearch.
    @return (rows, total, error)
    """
    # Lazy: keeps DataManager's dependency stack out of a viewer session that
    # only ever uses the field filters.  Matches how FaceEnrollment is loaded.
    from . import WebRuleSearch

    ruleName = one("rule")
    # WebRuleSearch turns this straight into ruleDir + name + ".query" and
    # unpickles whatever is there, and os.path.join neither collapses ".."
    # nor ignores an absolute component -- "C:/x/evil" drops ruleDir
    # entirely, and "//host/share/evil" survives as a UNC path, which would
    # have this process deserialize a file off an attacker's SMB server.
    # pickle.load executes code while loading, so that is remote code
    # execution for anyone holding a session.  A rule name is a bare
    # filename: anything with a separator or a drive in it is not one.
    if not _isBareName(ruleName):
        ctx.logger.warning("rejected rule name %r" % (ruleName,))
        return [], 0, "Unknown rule."
    nowMs = int(time.time() * 1000)

    def msParam(name):
        # Bounded like every other numeric filter: "1e999" and "1e308" both
        # raised OverflowError out of here, and a NEGATIVE value reached
        # WebRuleSearch._localDates, where datetime.date.fromtimestamp raises
        # OSError on Windows for anything before the epoch.
        v = one(name)
        if v is None:
            return None
        return _boundedNumber(v, int, 0, _kMaxNumber)

    startMs = msParam("startMs")
    endMs = msParam("endMs")
    if startMs is None and endMs is None:
        # No range given: today, like the Search screen's date picker.
        today = datetime.date.today()
        startMs = int(time.mktime(today.timetuple()) * 1000)
        endMs = nowMs
    elif startMs is None:
        startMs = endMs - 24 * 60 * 60 * 1000
    elif endMs is None:
        endMs = nowMs

    clips, error = WebRuleSearch.runRuleSearch(
        ctx, ruleName, one("camera"), startMs, endMs)
    if error:
        return [], 0, error

    try:
        limit = max(1, min(int(one("limit", "100")), _kSearchMaxLimit))
    except ValueError:
        limit = 100
    try:
        offset = max(0, int(one("offset", "0")))
    except ValueError:
        offset = 0

    return (_ruleRows(ctx, clips[offset:offset + limit]), len(clips), None)


def _facets(ctx):
    """ Distinct values for the search form (cameras, types, face names). """
    out = {"cameras": [], "types": [], "faceNames": []}
    try:
        conn = _connectRo(ctx.objDbPath)
    except Exception as e:
        ctx.logger.warning("objdb2 open failed for facets: %s" % e)
        return out
    try:
        out["cameras"] = [row[0] for row in conn.execute(
            "SELECT DISTINCT camLoc FROM objects "
            "WHERE camLoc IS NOT NULL ORDER BY camLoc")]
        out["types"] = [row[0] for row in conn.execute(
            "SELECT DISTINCT type FROM objects "
            "WHERE type IS NOT NULL ORDER BY type")]
        out["faceNames"] = [row[0] for row in conn.execute(
            "SELECT DISTINCT faceName FROM objectAttributes "
            "WHERE faceName IS NOT NULL AND faceName <> '' ORDER BY faceName")]
    except Exception:
        ctx.logger.error("facets query failed:\n%s" % traceback.format_exc())
    finally:
        conn.close()
    # Cameras also come from the clip DB (a camera may have clips but the
    # object rows for it may have aged out); merge so live cams still appear.
    try:
        cconn = _connectRo(ctx.clipDbPath)
        try:
            clipCams = [row[0] for row in cconn.execute(
                "SELECT DISTINCT camLoc FROM clips WHERE camLoc IS NOT NULL")]
        finally:
            cconn.close()
        out["cameras"] = sorted(set(out["cameras"]) | set(clipCams))
    except Exception:
        pass
    return out


def _clipGapMs(row, ms):
    """ How far `ms` falls outside a clip's span; 0 if it is inside. """
    if ms < row["firstMs"]:
        return row["firstMs"] - ms
    if ms > row["lastMs"]:
        return ms - row["lastMs"]
    return 0


def _nearestWithinDuration(before, after, ms):
    """ The closer of two candidate clips, if it is close enough to `ms`.

    "Close enough" is one clip-duration: a 60 s recording stands in for a
    moment up to 60 s outside it, a 1 s one for a moment 1 s outside.  The
    tolerance scales with the clip, so a camera recording in long segments
    gets a proportionally larger benefit of the doubt, and neither candidate
    gets the unlimited reach the old query had.

    @return the chosen row, or None if neither is within its own duration.
    """
    best = None
    bestGap = None
    for row in (before, after):
        if row is None:
            continue
        gap = _clipGapMs(row, ms)
        if gap > max(0, row["lastMs"] - row["firstMs"]):
            continue
        if bestGap is None or gap < bestGap:
            best, bestGap = row, gap
    return best


def _resolveClip(ctx, camLoc, ms):
    """ Find the recorded clip covering (or close to) camLoc @ ms.

    A clip that does not cover `ms` is accepted only when `ms` is within one
    clip-duration of it.  There used to be no limit at all: the fallback took
    the nearest clip on the camera however far away it was, and every caller
    treated the result as the real footage.  _apiSearch's hasClip therefore
    meant "this camera has some video somewhere", the player streamed
    unrelated footage, and _enrollFromDetection harvested faces out of it and
    filed them under a real person's name.

    @return (absPath, firstMs, lastMs) or None.
    """
    try:
        conn = _connectRo(ctx.clipDbPath)
    except Exception as e:
        ctx.logger.warning("clipdb open failed resolving %s@%s: %s"
                           % (camLoc, ms, e))
        return None
    try:
        try:
            row = conn.execute(
                "SELECT filename, firstMs, lastMs FROM clips "
                "WHERE camLoc=? AND firstMs<=? AND lastMs>=? "
                "ORDER BY firstMs DESC LIMIT 1", (camLoc, ms, ms)).fetchone()
            if row is None:
                # Nothing covers it.  Probe the nearest clip on each side, by
                # its own end and start -- both served by an index
                # (IDX_CLIPS_CAMLOC_LASTMS, IDX_CLIPS_CAMLOC_FIRSTMS).  The
                # old single query ordered by ABS(firstMs - ?), which no index
                # can satisfy: it scanned every row for the camera into a temp
                # B-tree, measured at 22 ms a call against ~0.5 ms for both
                # probes here -- and _apiSearch pays it once per result row.
                before = conn.execute(
                    "SELECT filename, firstMs, lastMs FROM clips "
                    "WHERE camLoc=? AND lastMs<=? "
                    "ORDER BY lastMs DESC LIMIT 1", (camLoc, ms)).fetchone()
                after = conn.execute(
                    "SELECT filename, firstMs, lastMs FROM clips "
                    "WHERE camLoc=? AND firstMs>=? "
                    "ORDER BY firstMs ASC LIMIT 1", (camLoc, ms)).fetchone()
                row = _nearestWithinDuration(before, after, ms)
        except sqlite3.DatabaseError as e:
            # A single unreadable page (e.g. disk-image corruption in a hot
            # region of clipdb) must not take down the whole search/clip
            # request -- degrade to "no clip available" like a real gap.
            ctx.logger.warning(
                "clipdb read error resolving %s@%s: %s" % (camLoc, ms, e))
            return None
    finally:
        conn.close()
    if row is None:
        return None
    absPath = os.path.abspath(os.path.join(ctx.videoDir, row["filename"]))
    # Guard against a maliciously crafted filename escaping the video root.
    root = os.path.abspath(ctx.videoDir)
    if not absPath.startswith(root + os.sep) and absPath != root:
        return None
    if not os.path.isfile(absPath):
        return None
    return (absPath, row["firstMs"], row["lastMs"])


def _objectClip(ctx, objId):
    """ Resolve the clip for a detection id, using its midpoint time. """
    try:
        conn = _connectRo(ctx.objDbPath)
    except Exception as e:
        ctx.logger.warning("objdb2 open failed resolving object %s: %s"
                           % (objId, e))
        return None
    try:
        try:
            row = conn.execute(
                "SELECT camLoc, timeStart, timeStop FROM objects WHERE uid=?",
                (objId,)).fetchone()
        except sqlite3.DatabaseError as e:
            ctx.logger.warning(
                "objdb2 read error resolving object %s: %s" % (objId, e))
            return None
    finally:
        conn.close()
    if row is None:
        return None
    mid = (row["timeStart"] + row["timeStop"]) // 2
    clip = _resolveClip(ctx, row["camLoc"], mid)
    if clip is None:
        return None
    absPath, firstMs, lastMs = clip
    return (absPath, firstMs, lastMs, mid, row["camLoc"])


def _cachedThumb(out):
    """ An unexpired, non-empty cached thumbnail at `out`, or None. """
    if os.path.isfile(out) and (time.time() - os.path.getmtime(out)
                                < _kThumbTtlSecs) and os.path.getsize(out) > 0:
        return out
    return None


def _detectionStartMs(ctx, objId):
    """ The timeStart for a detection uid, or None. """
    try:
        conn = _connectRo(ctx.objDbPath)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT timeStart FROM objects WHERE uid=?",
                           (objId,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return row["timeStart"] if row else None


def _thumbnailForObject(ctx, objId):
    """ Return a cached JPEG path for a detection, extracting a frame from the
    recorded clip on first request.  Returns None if no clip is available.
    """
    # Keyed on uid AND the detection's own start time.  uid is a counter that
    # restarts when the object store is rebuilt (as it was on 2026-09-02), so
    # it does not always name the same detection; on uid alone, a thumbnail
    # cached for the old "uid 42" would be served, with no error, for an
    # unrelated new detection that reused the number.  _thumbnailAt below
    # already content-addresses on (camera, time) for the same reason.
    startMs = _detectionStartMs(ctx, objId)
    if startMs is None:
        return None
    out = os.path.join(ctx.thumbDir, "%d_%d.jpg" % (int(objId), startMs))
    cached = _cachedThumb(out)
    if cached:
        return cached

    info = _objectClip(ctx, objId)
    if info is None:
        return None
    absPath, firstMs, lastMs, mid, _cam = info
    return _extractThumb(ctx, absPath, max(0.0, (mid - firstMs) / 1000.0), out)


def _thumbnailAt(ctx, camLoc, ms):
    """ Return a cached JPEG path for a camera at a moment in time.

    Rule search results identify a clip by (camera, time) and can carry an
    empty object list -- a duration- or region-only rule matches motion, not a
    detection row -- so they cannot always go through _thumbnailForObject.
    This is the same extract keyed on time instead of a detection id, and
    mirrors what /api/clip already accepts.
    """
    key = hashlib.sha1(("%s|%d" % (camLoc, ms)).encode("utf-8")).hexdigest()
    out = os.path.join(ctx.thumbDir, "t%s.jpg" % key[:24])
    cached = _cachedThumb(out)
    if cached:
        return cached

    clip = _resolveClip(ctx, camLoc, ms)
    if clip is None:
        return None
    absPath, firstMs, _lastMs = clip
    return _extractThumb(ctx, absPath, max(0.0, (ms - firstMs) / 1000.0), out)


def _extractThumb(ctx, absPath, offset, out):
    """ Pull a single scaled frame out of `absPath` at `offset` seconds.

    @return  `out` on success, None on failure.
    """
    try:
        from appCommon.InstallPaths import getFfmpegExe
        ffmpeg = getFfmpegExe()
    except Exception:
        ctx.logger.error("ffmpeg unavailable for thumbnails")
        return None

    # We only get here when the cache is empty or STALE, so a file from an
    # earlier extraction may already be sitting at `out`.  Remove it first:
    # ffmpeg opens its output only after the input succeeds, so a run that
    # fails early (the clip has since been deleted by retention, say) leaves
    # that file untouched -- and the isfile()/getsize() test below would then
    # read someone else's old thumbnail as this run's success and keep
    # serving it forever.  Measured: rc=-2, file byte-identical, test passed.
    try:
        os.remove(out)
    except OSError:
        pass

    cmd = [ffmpeg, "-nostdin", "-loglevel", "error",
           "-ss", "%.3f" % offset, "-i", absPath,
           "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
           "-y", out]
    try:
        kwargs = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
        proc = subprocess.run(cmd, timeout=20,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        ctx.logger.error("thumbnail extract failed:\n%s"
                         % traceback.format_exc())
        return None
    if proc.returncode != 0:
        # A clip the recorder has only just closed is not reliably readable
        # yet, and the viewer asks for thumbnails of exactly those while the
        # user watches recent events come in.  It fixes itself -- the same
        # file extracts cleanly once it settles -- and the caller already
        # falls back to a placeholder, so say it quietly.  Anything older
        # than this genuinely should have worked, and still gets a warning.
        try:
            age = time.time() - os.path.getmtime(absPath)
        except OSError:
            age = _kFreshClipSecs + 1
        note = (ctx.logger.debug if age < _kFreshClipSecs
                else ctx.logger.warning)
        note("ffmpeg returned %d extracting a thumbnail from %s (source is "
             "%.0fs old)" % (proc.returncode, absPath, age))
        return None
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        _pruneThumbs(ctx)
        return out
    return None


def _videoCodec(absPath):
    """ -> "hevc", "h264", or None, without spawning anything.

    Walks the top-level MP4 boxes to `moov` and looks for the sample-entry
    fourcc inside it.  Reading only moov (rather than scanning the file)
    keeps mdat payload bytes from producing a false match.  Verified against
    ffmpeg on all seventeen cameras' current recordings.
    """
    try:
        with open(absPath, "rb") as f:
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                size = struct.unpack(">I", hdr[:4])[0]
                kind = hdr[4:8]
                if size == 1:                    # 64-bit extended size
                    size = struct.unpack(">Q", f.read(8))[0]
                    skip = size - 16
                elif size == 0:                  # box runs to end of file
                    return _codecFromMoov(f.read()) if kind == b"moov" else None
                else:
                    skip = size - 8
                if skip < 0:
                    return None
                if kind == b"moov":
                    return _codecFromMoov(f.read(skip))
                f.seek(skip, 1)
    except Exception:
        return None


def _codecFromMoov(moov):
    """ The video codec named by a moov box, or None. """
    for tag, name in ((b"hev1", "hevc"), (b"hvc1", "hevc"), (b"hvcC", "hevc"),
                      (b"avc1", "h264"), (b"avcC", "h264")):
        if tag in moov:
            return name
    return None


def _clipCacheKey(absPath, variant):
    """ A cache name that changes if the source file does. """
    try:
        st = os.stat(absPath)
        stamp = "%d-%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = "0-0"
    digest = hashlib.sha1(("%s|%s|%s" % (absPath, stamp, variant))
                          .encode("utf-8")).hexdigest()
    return "%s-%s.mp4" % (variant, digest[:24])


def _runFfmpeg(ctx, args, out, what, quiet=False):
    """ Run one ffmpeg conversion to `out`.  -> True on success.

    @param  quiet  Log a failure at debug rather than error.  Used for an
                   attempt we are prepared to retry another way, so a machine
                   without a usable GPU encoder does not report an error for
                   something that then succeeds in software.
    """
    try:
        from appCommon.InstallPaths import getFfmpegExe
        ffmpeg = getFfmpegExe()
    except Exception:
        ctx.logger.error("ffmpeg unavailable for %s" % what)
        return False

    # Build into a temporary name and rename on success, so a crashed or
    # timed-out run can never leave a half-written file that later requests
    # would serve as a complete clip.
    tmp = out + ".part"
    try:
        os.remove(tmp)
    except OSError:
        pass
    # -f mp4 explicitly: the temp name ends in ".part", so ffmpeg cannot infer
    # the container from the extension and refuses to open the muxer.
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error"] + args + \
          ["-f", "mp4", "-y", tmp]
    try:
        kwargs = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
        proc = subprocess.run(cmd, timeout=_kTranscodeSecs,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, **kwargs)
    except Exception:
        note = ctx.logger.debug if quiet else ctx.logger.error
        note("%s failed:\n%s" % (what, traceback.format_exc()))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    if proc.returncode != 0 or not os.path.isfile(tmp) \
            or os.path.getsize(tmp) == 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        note = ctx.logger.debug if quiet else ctx.logger.error
        note("%s: ffmpeg returned %d%s"
             % (what, proc.returncode, (" -- " + err[-300:]) if err else ""))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    try:
        os.replace(tmp, out)
    except OSError:
        ctx.logger.error("%s: could not finalise %s" % (what, out))
        return False
    return True


def _playableClip(ctx, absPath, wantH264):
    """ A path to this clip that the requesting browser can actually play.

    H.264 -- fifteen of the seventeen cameras -- is returned untouched, so
    the common path costs one header read and nothing else.  HEVC is
    converted once and cached:

      wantH264  the browser has no HEVC decoder (Firefox, and Chrome without
                hardware support): transcode to H.264, downscaled to
                _kTranscodeHeight since a 4K stream is far more than the
                viewer needs.  Measured ~6.6 s for a 59 s clip.
      else      the browser has HEVC but these files are tagged 'hev1',
                which browsers reject; a stream copy retagged 'hvc1' fixes
                that in ~0.1 s with no re-encode and no quality loss.

    @return  a path to serve, or None if the conversion failed.
    """
    codec = _videoCodec(absPath)
    if codec != "hevc":
        return absPath

    variant = "h264" if wantH264 else "hvc1"
    out = os.path.join(ctx.clipCacheDir, _clipCacheKey(absPath, variant))
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out

    with ctx.convertLock:
        # Someone may have built it while we waited for the lock.
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        # Audio is copied in both cases: it is already AAC, which every
        # browser that can play the video can also play.
        if wantH264:
            what = "H.264 transcode of %s" % os.path.basename(absPath)
            started = time.time()
            if not _transcodeH264(ctx, absPath, out, what):
                return None
        else:
            what = "hvc1 remux of %s" % os.path.basename(absPath)
            args = ["-i", absPath, "-c", "copy", "-tag:v", "hvc1",
                    "-movflags", "+faststart"]
            started = time.time()
            if not _runFfmpeg(ctx, args, out, what):
                return None
        ctx.logger.info("%s took %.1f s -> %.1f MB"
                        % (what, time.time() - started,
                           os.path.getsize(out) / 1e6))
        _pruneClipCache(ctx)
    return out


def _transcodeH264(ctx, absPath, out, what):
    """ Produce a browser-playable H.264 copy of an HEVC clip.

    Uses the GPU when this machine has a working one -- 6 s against 49 s
    software on the clip this was measured with, which is the difference
    between a viewer that plays and one the browser gives up on.  The first
    failure latches software mode for the life of the process, so a machine
    without NVENC pays the failed attempt once rather than per clip.

    Called with ctx.convertLock held, which is also what makes the
    _hwTranscodeOk update safe.

    @return True on success.
    """
    global _hwTranscodeOk

    tail = ["-c:a", "copy", "-movflags", "+faststart"]
    if _hwTranscodeOk is not False:
        hwArgs = (_kHwDecodeArgs + ["-i", absPath,
                  "-vf", "scale_cuda=-2:%d" % _kTranscodeHeight]
                  + _kHwEncodeArgs + tail)
        # Quiet on the first attempt: a machine with no usable GPU encoder
        # should not report an error for something that then succeeds.
        if _runFfmpeg(ctx, hwArgs, out, what + " (GPU)",
                      quiet=(_hwTranscodeOk is None)):
            if _hwTranscodeOk is None:
                _hwTranscodeOk = True
                ctx.logger.info("GPU transcoding is available; using it")
            return True
        if _hwTranscodeOk is None:
            _hwTranscodeOk = False
            ctx.logger.info("GPU transcoding is not available here; "
                            "falling back to software for this session")

    swArgs = ["-i", absPath, "-vf", "scale=-2:%d" % _kTranscodeHeight,
              "-c:v", "libx264", "-preset", _kTranscodePreset,
              "-crf", _kTranscodeCrf] + tail
    return _runFfmpeg(ctx, swArgs, out, what)


def _pruneClipCache(ctx):
    """ Keep the converted-clip cache under _kClipCacheMax bytes.

    Bounded by total size rather than a file count, the way _pruneThumbs is:
    these are tens of megabytes each, not kilobytes.  Oldest go first.
    """
    try:
        files = []
        total = 0
        for name in os.listdir(ctx.clipCacheDir):
            path = os.path.join(ctx.clipCacheDir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
            total += st.st_size
        if total <= _kClipCacheMax:
            return
        files.sort()
        for _mtime, size, path in files:
            if total <= _kClipCacheMax:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
    except Exception:
        pass


def _pruneThumbs(ctx):
    """ Keep the thumbnail cache bounded (delete oldest beyond the cap). """
    try:
        files = [os.path.join(ctx.thumbDir, f)
                 for f in os.listdir(ctx.thumbDir) if f.endswith(".jpg")]
        if len(files) <= _kThumbCacheMax:
            return
        files.sort(key=os.path.getmtime)
        for f in files[:len(files) - _kThumbCacheMax]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


###############################################################################
# Face enrollment from camera footage
#
# The record viewer lets you add a detection's face to the recognition
# baseline.  The harvest/save core lives in FaceEnrollment (shared with the
# desktop Search window's "Add face to baseline" action); here we only resolve
# the detection's clip and delegate.
###############################################################################

def _enrollFromDetection(ctx, objId, name, gender):
    """ Harvest face crops for a detection and enroll them under `name`.

    @return dict for the JSON response; {'error': ...} on failure.
    """
    from backEnd.FaceEnrollment import enrollFaceFromClipFile

    info = _objectClip(ctx, objId)
    if info is None:
        return {"error": "no recorded clip covers this detection"}
    absPath, firstMs, lastMs, mid, camLoc = info
    return enrollFaceFromClipFile(absPath, firstMs, mid, camLoc,
                                  name, gender, ctx.logger)


###############################################################################
# HTTP request handler
###############################################################################

class _Handler(BaseHTTPRequestHandler):

    server_version = "SighthoundLAN/2.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    @property
    def ctx(self):
        return self.server.ctx

    def log_message(self, fmt, *a):
        try:
            self.ctx.logger.debug("%s - %s" % (self.address_string(), fmt % a))
        except Exception:
            pass

    def _clientIp(self):
        try:
            return self.client_address[0]
        except Exception:
            return "?"

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _sessionUser(self):
        return self.ctx.sessionUser(self._cookies().get("sv_session"))

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              extraHeaders=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # One Cache-Control, not two.  Sending the default AND the caller's
        # put both on the wire, where they combine to "no-store, max-age=300"
        # and the most restrictive wins -- so the stylesheet, the script and
        # every thumbnail were re-fetched on every page load despite asking
        # to be cached.  Confirmed against the running server.
        extraHeaders = dict(extraHeaders or {})
        cacheControl = "no-store"
        for key in list(extraHeaders):
            if key.lower() == "cache-control":
                cacheControl = extraHeaders.pop(key)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cacheControl)
        for k, v in extraHeaders.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _sendJson(self, obj, code=200, extraHeaders=None):
        self._send(code, json.dumps(obj),
                   "application/json; charset=utf-8", extraHeaders)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- verbs -----------------------------------------------------------

    def do_HEAD(self):
        self._route()

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in ("/", ""):
                return self._home()
            if path == "/api/login" and self.command == "POST":
                return self._apiLogin()
            if path == "/api/logout" and self.command == "POST":
                return self._apiLogout()
            if path == "/api/session":
                return self._apiSession()
            if path.startswith("/api/"):
                if self._sessionUser() is None:
                    return self._sendJson({"error": "unauthorized"}, 401)
                if path == "/api/facets":
                    return self._apiFacets()
                if path == "/api/rules":
                    return self._apiRules()
                if path == "/api/search":
                    return self._apiSearch(parsed)
                if path == "/api/thumb":
                    return self._apiThumb(parsed)
                if path == "/api/clip":
                    return self._apiClip(parsed)
                if path == "/api/enrollnames":
                    from backEnd.FaceEnrollment import listBaselinePeople
                    return self._sendJson({"people": listBaselinePeople()})
                if path == "/api/enroll" and self.command == "POST":
                    return self._apiEnroll()
                return self._sendJson({"error": "not found"}, 404)
            return self._static(path)
        except ConnectionError:
            # The client went away mid-response -- _serveVideo's own loop
            # already treats that as unremarkable, but the same exceptions
            # raised from send_response/end_headers landed here and were
            # logged as internal errors with a full traceback.  There is
            # nothing to send back on a socket that is gone.  ConnectionError
            # rather than a list of subclasses: Windows adds
            # ConnectionAbortedError to the two that were named here.
            self.close_connection = True
        except Exception:
            try:
                self.ctx.logger.error("request error %s:\n%s"
                                      % (self.path, traceback.format_exc()))
            except Exception:
                pass
            try:
                self._send(500, "internal error")
            except Exception:
                pass

    # -- pages -----------------------------------------------------------

    def _home(self):
        if self._sessionUser() is None:
            return self._redirect("/login.html")
        return self._serveFile("index.html")

    def _static(self, path):
        name = path.lstrip("/")
        publicFiles = {"login.html", "style.css", "app.js", "favicon.ico"}
        if name not in publicFiles and name != "index.html":
            return self._send(404, "not found")
        if name not in publicFiles and self._sessionUser() is None:
            return self._redirect("/login.html")
        return self._serveFile(name)

    def _serveFile(self, name):
        full = os.path.join(_kWebRootDir, name)
        if not os.path.isfile(full):
            return self._send(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                data = f.read()
        except Exception:
            return self._send(500, "read error")
        cache = "no-store" if name.endswith(".html") else "max-age=60"
        self._send(200, data, ctype, {"Cache-Control": cache})

    # -- api: auth -------------------------------------------------------

    def _readBody(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > 1 << 20:
            # Refuse to buffer it, but still consume exactly what was
            # declared.  Returning without reading left those bytes in front
            # of the next request on this keep-alive connection, which broke
            # the connection outright rather than merely rejecting the
            # oversized request.
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.ctx.logger.warning("discarded a %d byte body on %s"
                                    % (length, self.path))
            return b""
        return self.rfile.read(length)

    def _apiLogin(self):
        ip = self._clientIp()
        if self.ctx.loginLocked(ip):
            return self._sendJson({"error": "too many attempts, try later"},
                                  429)
        body = self._readBody()
        user = passw = ""
        ctype = self.headers.get("Content-Type", "")
        try:
            if "application/json" in ctype:
                d = json.loads(body.decode("utf-8") or "{}")
                user = str(d.get("user", ""))
                passw = str(d.get("pass", ""))
            else:
                d = urllib.parse.parse_qs(body.decode("utf-8"))
                user = d.get("user", [""])[0]
                passw = d.get("pass", [""])[0]
        except Exception:
            pass
        token = self.ctx.login(user, passw)
        self.ctx.noteLoginResult(ip, token is not None)
        if token is None:
            self.ctx.logger.info("failed login for %r from %s" % (user, ip))
            return self._sendJson({"error": "invalid credentials"}, 401)
        self.ctx.logger.info("login ok for %r from %s" % (user, ip))
        cookie = ("sv_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                  % (token, _kSessionTtlSecs))
        return self._sendJson({"ok": True, "user": user},
                              extraHeaders={"Set-Cookie": cookie})

    def _apiLogout(self):
        self.ctx.logout(self._cookies().get("sv_session"))
        cookie = "sv_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        return self._sendJson({"ok": True},
                              extraHeaders={"Set-Cookie": cookie})

    def _apiSession(self):
        user = self._sessionUser()
        return self._sendJson({"authed": user is not None, "user": user})

    # -- api: data -------------------------------------------------------

    def _apiEnroll(self):
        body = self._readBody()
        try:
            d = json.loads(body.decode("utf-8") or "{}")
            objId = int(d.get("id"))
            name = str(d.get("name", ""))
            gender = str(d.get("gender", "")).upper()
        except Exception:
            return self._sendJson({"error": "bad request"}, 400)
        try:
            result = _enrollFromDetection(self.ctx, objId, name, gender)
        except Exception:
            self.ctx.logger.error("enroll failed:\n%s"
                                  % traceback.format_exc())
            result = {"error": "enroll failed (see WebServer.log)"}
        return self._sendJson(result, 200 if result.get("ok") else 422)

    def _apiFacets(self):
        return self._sendJson(_facets(self.ctx))

    def _apiRules(self):
        # Cheap: WebRuleSearch keeps its expensive imports function-local, so
        # listing rules on every page load costs a directory read.
        from . import WebRuleSearch
        return self._sendJson({"rules": WebRuleSearch.listRules(
            self.ctx.ruleDir, self.ctx.logger)})

    def _apiSearch(self, parsed):
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        def one(name, default=None):
            v = params.get(name)
            if not v:
                return default
            v = v[0].strip()
            return v if v != "" else default

        # Two alternative ways to search, offered side by side in the form: a
        # Search-screen rule, or the ad-hoc detection filters.  A chosen rule
        # wins, since it brings its own notion of what to match.
        if one("rule"):
            rows, total, error = _searchByRule(self.ctx, params, one)
            payload = {"results": rows, "count": len(rows),
                       "total": total, "mode": "rule"}
            if error:
                payload["error"] = error
            return self._sendJson(payload)

        results = _searchObjects(self.ctx, params)
        for row in results:
            row["hasClip"] = _resolveClip(
                self.ctx, row["camera"],
                (row["timeStart"] + row["timeStop"]) // 2) is not None
        return self._sendJson({"results": results, "count": len(results),
                               "mode": "filters"})

    def _apiThumb(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        # By detection id, or -- for rule results, whose matching clip may have
        # no detection row at all -- by camera and time, as /api/clip accepts.
        if "cam" in params and "ms" in params:
            try:
                path = _thumbnailAt(self.ctx, params["cam"][0],
                                    int(float(params["ms"][0])))
            except ValueError:
                return self._send(400, "bad time")
        else:
            try:
                objId = int(params.get("id", ["0"])[0])
            except ValueError:
                return self._send(400, "bad id")
            path = _thumbnailForObject(self.ctx, objId)
        if not path:
            return self._placeholder()
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            return self._placeholder()
        self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=300"})

    def _placeholder(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="320" '
               'height="180"><rect width="100%" height="100%" fill="#1e2430"/>'
               '<text x="50%" y="50%" fill="#5b6472" font-family="sans-serif" '
               'font-size="14" text-anchor="middle" dominant-baseline="middle">'
               'no clip on disk</text></svg>')
        self._send(200, svg, "image/svg+xml", {"Cache-Control": "max-age=60"})

    def _apiClip(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        absPath = None
        if "id" in params:
            try:
                info = _objectClip(self.ctx, int(params["id"][0]))
            except ValueError:
                info = None
            if info:
                absPath = info[0]
        elif "cam" in params and "ms" in params:
            try:
                clip = _resolveClip(self.ctx, params["cam"][0],
                                    int(float(params["ms"][0])))
                if clip:
                    absPath = clip[0]
            except ValueError:
                pass
        if not absPath or not os.path.isfile(absPath):
            return self._send(404, "clip not available")
        # h264=1 means the browser told us it has no HEVC decoder; see
        # _playableClip.  Non-HEVC clips come back unchanged either way.
        wantH264 = params.get("h264", ["0"])[0] in ("1", "true", "yes")
        servePath = _playableClip(self.ctx, absPath, wantH264)
        if servePath is None:
            return self._send(503, "clip could not be prepared for playback")
        return self._serveVideo(servePath)

    # -- range-capable video streaming -----------------------------------

    def _serveVideo(self, absPath):
        try:
            size = os.path.getsize(absPath)
        except OSError:
            return self._send(404, "clip not available")
        ctype = mimetypes.guess_type(absPath)[0] or "video/mp4"
        rangeHdr = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rangeHdr and rangeHdr.startswith("bytes="):
            partial = True
            spec = rangeHdr[6:].split(",")[0]
            s, _, e = spec.partition("-")
            try:
                if s.strip():
                    start = int(s)
                    end = int(e) if e.strip() else size - 1
                else:
                    start = max(0, size - int(e))   # suffix range bytes=-N
                    end = size - 1
            except ValueError:
                partial = False
                start, end = 0, size - 1
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        # Content-Length is already on the wire.  Any path that stops short
        # of it has to close the connection: this is HTTP/1.1 with keep-alive,
        # so a client still counting up to Content-Length will read the NEXT
        # response's headers and body as though they were more video.
        # (Reproduced: promised 5000 bytes, sent 100, and the reply to the
        # following request was swallowed whole.)
        try:
            with open(absPath, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 256 * 1024
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        # Fewer bytes on disk than getsize() reported before
                        # the headers went out -- retention deleted or
                        # replaced the clip mid-stream.
                        self.close_connection = True
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except ConnectionError:
            # The base class, not the two subclasses this used to name.
            # Windows raises ConnectionAbortedError (WinError 10053) when the
            # browser abandons a video request -- clicking another clip, or
            # giving up during a transcode -- and that is exactly as routine
            # as a broken pipe, but it was falling through to the handler
            # below and logging an ERROR with a traceback for it.
            self.close_connection = True
        except Exception:
            self.ctx.logger.error("video stream error:\n%s"
                                  % traceback.format_exc())
            self.close_connection = True


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ctx):
        self.ctx = ctx
        super().__init__(addr, _Handler)


###############################################################################
# Controller (process main loop)
###############################################################################

class _Controller(object):
    """ Owns the HTTP server lifecycle and processes back-end commands. """

    def __init__(self, msgQ, cmdQ, ctx, port):
        self._msgQ = msgQ
        self._cmdQ = cmdQ
        self._ctx = ctx
        self._port = port
        self._httpd = None
        self._serveThread = None
        self._shutdown = False
        self._verified = False
        self._instance = uuid.uuid4().hex
        self._statusNum = random.randint(0, 500000)

    # -- http lifecycle --------------------------------------------------

    def _startHttp(self):
        if self._httpd is not None or self._port is None or self._port <= 0:
            return
        try:
            self._httpd = _HttpServer(("0.0.0.0", self._port), self._ctx)
        except Exception:
            self._ctx.logger.error("cannot bind port %d:\n%s"
                                   % (self._port, traceback.format_exc()))
            self._httpd = None
            self._verified = False
            return
        self._serveThread = threading.Thread(
            target=self._httpd.serve_forever, name="lanhttp", daemon=True)
        self._serveThread.start()
        self._verified = True
        self._ctx.logger.info("LAN record viewer listening on 0.0.0.0:%d"
                              % self._port)

    def _stopHttp(self):
        if self._httpd is not None:
            self._ctx.logger.info("stopping LAN record viewer")
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
            self._serveThread = None
        self._verified = False

    # -- status file (read by Options -> Remote Access) ------------------

    def _writeStatus(self):
        s = {
            "number":        self._statusNum,
            "port":          self._port if self._port else -1,
            "instance":      self._instance,
            "verified":      self._verified,
            "certificateId": "",
        }
        self._statusNum += 1
        spath = os.path.join(self._ctx.webDir, _kStatusFile)
        tmp = spath + (".%04x" % random.randint(0, 0xffff))
        try:
            os.makedirs(self._ctx.webDir, exist_ok=True)
            with open(tmp, "wb") as h:
                pickle.dump(s, h, 0)
            # No remove-then-replace: os.replace already overwrites atomically
            # on Windows (MoveFileEx with MOVEFILE_REPLACE_EXISTING).  The
            # remove only created a window -- on every single rewrite, once a
            # minute -- in which Options -> Remote Access could look for the
            # status file and find nothing there.
            os.replace(tmp, spath)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass

    def _deleteStatus(self):
        try:
            os.remove(os.path.join(self._ctx.webDir, _kStatusFile))
        except Exception:
            pass

    # -- commands --------------------------------------------------------

    def _handle(self, msg):
        msgId = msg[0]
        if msgId in (MessageIds.msgIdQuit, MessageIds.msgIdQuitWithResponse):
            self._ctx.logger.info("shutdown requested")
            self._shutdown = True
        elif msgId == MessageIds.msgIdWebServerSetPort:
            newPort = int(msg[1])
            if newPort != self._port:
                self._ctx.logger.info("port %d -> %d" % (self._port, newPort))
                self._stopHttp()
                self._port = newPort
                if newPort > 0:
                    self._startHttp()
                self._writeStatus()
        elif msgId == MessageIds.msgIdWebServerSetAuth:
            self._ctx.logger.info("credentials updated")
            self._ctx.setAuth(msg[1])
        elif msgId == MessageIds.msgIdWebServerEnablePortOpener:
            # LAN-only rebuild never opens router ports; accept + ignore so the
            # existing Options checkbox does not error.
            self._ctx.logger.info("port opener flag ignored (LAN-only build)")
        elif msgId == MessageIds.msgIdSetDebugConfig:
            pass
        elif msgId == MessageIds.msgIdWsgiPortChanged:
            pass
        else:
            self._ctx.logger.debug("ignoring message id %s" % (msgId,))

    def run(self):
        self._ctx.logger.info("=== LAN WebServer starting (pid %d, port %s) ==="
                              % (os.getpid(), self._port))
        if self._port and self._port > 0:
            self._startHttp()
        self._writeStatus()
        nextPing = 0.0
        while not self._shutdown:
            try:
                msg = self._cmdQ.get(timeout=0.25)
            except queue.Empty:
                msg = None
            except (EOFError, OSError):
                break
            if msg is not None:
                try:
                    self._handle(msg)
                except Exception:
                    # _handle indexes msg[0] and int()s msg[1]; neither the
                    # IndexError from an empty list nor the ValueError from a
                    # bad port was caught here, so a single malformed message
                    # escaped run(), reached runWebServer's handler and ended
                    # the process.  The back end's watchdog would restart it,
                    # but losing the viewer over one bad message is wrong.
                    self._ctx.logger.error("bad control message %r:\n%s"
                                           % (msg, traceback.format_exc()))
            now = time.time()
            if now >= nextPing:
                nextPing = now + _kPingSecs
                if self._msgQ is not None:
                    try:
                        self._msgQ.put([MessageIds.msgIdWebServerPing])
                    except Exception:
                        pass
                self._writeStatus()
        self._stopHttp()
        self._deleteStatus()
        self._ctx.logger.info("=== LAN WebServer stopped ===")


###############################################################################
# Process entry point (spawned by BackEndProcessJumper.startWebServer)
###############################################################################

def runWebServer(msgQ, cmdQ, logDir, webDir, port, auth,
                 videoDir, clipDbPath, objDbPath, ruleDir=None):
    """ Child-process entry point for the LAN record viewer.

    @param msgQ        Queue to the back end (liveness pings).
    @param cmdQ        Queue of control messages from the back end.
    @param logDir      Directory for WebServer.log.
    @param webDir      Transient dir (status file + thumbnail cache).
    @param port        HTTP port, or -1 to stay off until enabled.
    @param auth        Stored auth expression (see make_auth), or "".
    @param videoDir    Root of recorded clips (the 'archive' folder).
    @param clipDbPath  Path to the clip database (read-only access).
    @param objDbPath   Path to the object/detection database (read-only).
    @param ruleDir     <userLocalDataDir>/rules, holding the saved rule/query
                       pickles, so the viewer can search by the same rules as
                       the desktop Search screen (read-only).  Optional, to
                       keep this entry point backward compatible.
    """
    logger = getLogger(_kWebServerLog, logDir, _kLogSize)
    try:
        os.makedirs(webDir, exist_ok=True)
    except Exception:
        pass
    ctx = _Context(logger, webDir, videoDir, clipDbPath, objDbPath, auth,
                   ruleDir)
    try:
        _Controller(msgQ, cmdQ, ctx, int(port)).run()
    except Exception:
        logger.critical("web server crashed:\n%s" % traceback.format_exc())
