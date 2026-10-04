# -*- coding: utf-8 -*-
"""Post-change soak check for the 2026-09-05/06 review sweep.

Run any time; run it AGAIN after 20:00 local, because the thumbnail change is
indistinguishable before then (local and UTC dates agree until the UTC day
rolls at 20:00 local, so old and new code produce the same folder name).

    venv\\Scripts\\python.exe scripts\\soakCheck.py

Exit code 0 if everything looks right, 1 if anything wants a look.
"""
import collections
import datetime
import os
import pickle
import sqlite3
import sys

# Same idiom as testDetectionReplay.py / testDetectionSampling.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP = r"C:\Users\Bernie\AppData\Local\Sighthound Video Py3"
LOGS = os.path.join(APP, "logs")
LOGFILES = ("BackEndApp.log", "Response.log", "NetworkMessageServer.log",
            "WebServer.log", "DiskCleaner.log", "DetectionService.log")

PROBLEMS = []


def ok(label, good, detail=""):
    print(("  ok    " if good else "  LOOK  ") + label
          + (("  -- " + detail) if detail else ""))
    if not good:
        PROBLEMS.append(label)


def today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def linesFromToday(path):
    """Lines from today onward, by line offset -- not a string compare, which
    would also admit continuation lines of older tracebacks."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    stamp = today()
    for i, line in enumerate(lines):
        if line.startswith(stamp):
            return lines[i:]
    return []


print("SOAK CHECK  %s local (UTC%s)"
      % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
         datetime.datetime.now().astimezone().strftime("%z")))
print("=" * 66)

# ---------------------------------------------------------------- 1. logs
print("\n1. errors today")
for name in LOGFILES:
    lines = linesFromToday(os.path.join(LOGS, name))
    errs = [l for l in lines if " - ERROR - " in l or " - CRITICAL - " in l]
    tbs = [l for l in lines if l.startswith("Traceback")]
    ok("%-26s %d lines" % (name, len(lines)), not errs and not tbs,
       "%d error/critical, %d traceback" % (len(errs), len(tbs)))
    for l in errs[:2]:
        print("          " + l.rstrip()[:110])

# ------------------------------------------------------ 2. things we fixed
print("\n2. the specific failures these changes were meant to stop")
allToday = []
for name in LOGFILES:
    allToday += linesFromToday(os.path.join(LOGS, name))
camDir = os.path.join(LOGS, "cameras")
camToday = []
if os.path.isdir(camDir):
    for f in os.listdir(camDir):
        if f.endswith(".log"):
            camToday += linesFromToday(os.path.join(camDir, f))

blocked = [l for l in allToday if "send queue is BLOCKED" in l]
ok("clip delivery never blocked", not blocked, "%d line(s)" % len(blocked))

streamErr = [l for l in allToday if "video stream error" in l]
ok("no spurious video stream errors", not streamErr,
   "%d line(s)" % len(streamErr))

dropped = [l for l in allToday if "queued past its deadline" in l]
ok("detection service dropped nothing", not dropped,
   "%d line(s)" % len(dropped))

badMsg = [l for l in allToday if "bad control message" in l]
ok("no malformed control messages", not badMsg, "%d line(s)" % len(badMsg))

abandoned = [l for l in allToday if "abandoning it" in l
             or "never arrived after" in l]
ok("no abandoned detector shutdowns", not abandoned,
   "%d line(s)" % len(abandoned))

phantom = collections.Counter()
for l in camToday:
    i = l.find("phantom=")
    if i >= 0:
        phantom[l[i:].split()[0]] += 1
ok("phantom votes still zero", set(phantom) <= {"phantom=0"},
   ", ".join("%s x%d" % (k, v) for k, v in phantom.most_common()) or "no stats lines yet")

gpu = [l for l in allToday if "GPU transcoding is available" in l]
sw = [l for l in allToday if "GPU transcoding is not available" in l]
if gpu or sw:
    ok("HEVC transcoding on the GPU", bool(gpu) and not sw,
       "gpu=%d software=%d" % (len(gpu), len(sw)))

# ------------------------------------------- 3. thumbnails, the real test
print("\n3. thumbnails: are NEW ones filed by local date?")
prefs = pickle.load(open(os.path.join(APP, "backEndPrefs"), "rb"))
from appCommon.CommonStrings import kVideoFolder
ARCH = os.path.join(prefs["videoDir"], kVideoFolder)

def _lastRestart():
    """When the back end last came up -- the only sensible cutoff.

    A fixed lookback window is wrong: thumbnails written before the change
    went live are legitimately UTC-named, and a 26-hour window sweeps in the
    whole of the previous evening, which is exactly the span where local and
    UTC disagree.  That reports thousands of false failures.
    """
    newest = None
    for name in ("DetectionService.log", "WebServer.log"):
        for line in linesFromToday(os.path.join(LOGS, name)):
            if "starting" in line and "===" in line:
                try:
                    t = datetime.datetime.strptime(line[:19],
                                                   "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if newest is None or t > newest:
                    newest = t
    return newest


restart = _lastRestart()
if restart is None:
    print("     (no restart found in today's logs; using midnight)")
    restart = datetime.datetime.now().replace(hour=0, minute=0, second=0,
                                              microsecond=0)
print("     counting only thumbnails written since the restart at %s"
      % restart.strftime("%H:%M"))
cutoffMs = restart.timestamp() * 1000
tally = collections.Counter()
examples = []
total = 0
for cam in os.listdir(ARCH):
    cd = os.path.join(ARCH, cam)
    if not os.path.isdir(cd):
        continue
    for day in os.listdir(cd):
        td = os.path.join(cd, day, "thumbs")
        if not os.path.isdir(td):
            continue
        for f in os.listdir(td):
            stem = os.path.splitext(f)[0]
            if not stem.isdigit():
                continue
            total += 1
            ms = int(stem)
            if ms < cutoffMs:
                continue                       # written before the change
            loc = datetime.datetime.fromtimestamp(ms / 1000)
            utc = datetime.datetime.fromtimestamp(ms / 1000,
                                                  datetime.timezone.utc)
            ls, us = loc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m-%d")
            if ls == us:
                tally["cannot tell (local==utc)"] += 1
            elif day == ls:
                tally["LOCAL (correct)"] += 1
            elif day == us:
                tally["UTC (change not live)"] += 1
                if len(examples) < 2:
                    examples.append("%s/%s/thumbs/%s -> local %s"
                                    % (cam, day, f, loc.strftime("%m-%d %H:%M")))
            else:
                tally["neither"] += 1
print("     thumbnails on disk: %d" % total)
for k, v in tally.most_common():
    print("     recent, %-26s %d" % (k + ":", v))
for e in examples:
    print("       " + e)

decisive = tally["LOCAL (correct)"] + tally["UTC (change not live)"]
if decisive == 0:
    print("\n     NOT YET TESTABLE.  Every recent thumbnail was written when the")
    print("     local and UTC dates agreed.  Re-run this after 20:00 local,")
    print("     once the UTC day has rolled, for the decisive answer.")
else:
    ok("new thumbnails use the LOCAL date",
       tally["UTC (change not live)"] == 0,
       "%d local, %d utc" % (tally["LOCAL (correct)"],
                             tally["UTC (change not live)"]))

# ------------------------------------------------------------ 4. baseline
print("\n4. counters (compare against the previous run)")
db = os.path.join(APP, "videos", "clipdb")
c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
clips = c.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
c.close()
o = sqlite3.connect("file:%s?mode=ro"
                    % os.path.join(APP, "videos", "objdb2").replace("\\", "/"),
                    uri=True)
objs = o.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
cls = o.execute("SELECT COUNT(*) FROM objects WHERE type IS NOT NULL "
                "AND type NOT IN ('object','unknown')").fetchone()[0]
o.close()
print("     clips        %d" % clips)
print("     detections   %d objects, %d classified (%.2f%%)"
      % (objs, cls, 100.0 * cls / max(objs, 1)))
print("     thumbnails   %d" % total)
print("     (2026-09-06 08:58 baseline: 72802 clips, 120589 objects,")
print("      5874 classified 4.87%, 259788 thumbnails)")

print("")
if PROBLEMS:
    print("%d item(s) want a look: %s" % (len(PROBLEMS), "; ".join(PROBLEMS)))
    sys.exit(1)
print("nothing wants a look.")
