r"""Every ERROR and WARNING in the logs -- grouped first, then in full.

Two sections, because the raw listing alone is unreadable and the summary alone
hides the individual occurrence you are hunting:

  SUMMARY  one row per distinct message SHAPE, with a count.  Measured on this
           fleet, 246 283 matching lines collapse to 301 shapes -- and a single
           shape ("Timestamp anomaly") is 71 % of the whole volume.  Without
           this you cannot see that one warning is drowning everything else.
  FULL     every matching line, in time order, tagged with its source log.

Both system logs (logs\*.log*) and camera logs (logs\cameras\*.log*) are read;
the line format is identical in both.  Rotated files are included -- .log.1 is
the most RECENT backup, so files are ordered by the first timestamp inside them
rather than by name.

Usage:  SystemHealthLogReport.py [--since "YYYY-MM-DD HH:MM"]
                                 [--errors | --warnings] [--no-detail]
"""
import collections
import glob
import os
import re
import sys
import time

# Default only; the GUI passes the app's real data dir instead.
LOGDIR = r"C:\Users\Bernie\AppData\Local\Sighthound Video Py3\logs"

kErrorLevel = "ERROR"
kWarningLevel = "WARNING"
kAllLevels = (kErrorLevel, kWarningLevel)

# Every log line in this app is
#   TS,ms - PID-TID - LEVEL - Module.py - function - message
# Verified against both a system log (Response.log) and a camera log.
LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \S+ - (ERROR|WARNING) - "
    r"(\S+) - (\S+) - (.*)$")

# Normalisation for the group key, most specific first: paths before hex
# before plain digits, or the digits rule would chew the paths up first.
_PATH = re.compile(r"[A-Za-z]:\[^\s'\"]+|/[^\s'\"]{6,}")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_NUM = re.compile(r"\d+")

# Group keys are truncated so that one enormous message (a stack trace on a
# single line) cannot split into thousands of near-identical shapes.
_kKeyLen = 200
# How much of the example message to show in the summary table.
_kExampleLen = 110


def normalise(msg):
    """-> the message with the varying parts replaced, for grouping."""
    s = _PATH.sub("<path>", msg)
    s = _HEX.sub("<x>", s)
    s = _NUM.sub("#", s)
    return s[:_kKeyLen]


def logFiles(logdir=None):
    """Every log file, system and per-camera, oldest content first.

    Same reasoning as SystemHealthCamReport.logs_for: rotation numbers files
    backwards, so sort by the first timestamp actually inside each one.
    """
    logdir = logdir or LOGDIR
    paths = glob.glob(os.path.join(logdir, "*.log*")) + \
            glob.glob(os.path.join(logdir, "cameras", "*.log*"))
    dated = []
    for q in paths:
        if os.path.isdir(q):
            continue
        first = ""
        try:
            with open(q, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = LINE.match(line)
                    if m:
                        first = m.group(1)
                        break
        except Exception:
            continue
        dated.append((first or "9999", q))
    return [q for _, q in sorted(dated)]


def sourceName(path):
    """-> 'Response' or 'cameras/09_Jungle', stripped of the rotation suffix."""
    base = os.path.basename(path).split(".log")[0]
    if os.path.basename(os.path.dirname(path)).lower() == "cameras":
        return "cameras/" + base
    return base


class _Cancelled(Exception):
    """Raised out of buildReport when the progress callback asks to stop."""


def _sinceStamp(since):
    """-> 'YYYY-MM-DD HH:MM:SS' for `since`, or '' for no lower bound.

    Timestamps are compared as STRINGS.  This format sorts lexicographically in
    time order, so it is exact, and it avoids 246 000 strptime calls -- which
    would cost more than the entire rest of the scan.
    """
    if not since:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))


def buildReport(since=None, levels=kAllLevels, logdir=None, progress=None,
                detail=True):
    """The report as a string.

    @param  since     Epoch seconds; only lines at or after this are counted.
    @param  levels    Which levels to include, e.g. ("ERROR",).
    @param  logdir    The logs directory; defaults to the module constant.
    @param  progress  callable(done, total, name) -> False to cancel.
    @param  detail    Include the full per-line listing after the summary.
    """
    levels = tuple(levels) or kAllLevels
    floor = _sinceStamp(since)
    files = logFiles(logdir)
    total = max(len(files), 1)

    groups = {}
    detailLines = []
    scanned = 0
    perLevel = collections.Counter()

    for i, path in enumerate(files):
        if progress is not None and not progress(i, total, sourceName(path)):
            raise _Cancelled()
        src = sourceName(path)
        try:
            f = open(path, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        scanned += 1
        with f:
            for line in f:
                m = LINE.match(line)
                if m is None:
                    continue
                ts, level, mod, func, msg = m.groups()
                if level not in levels or ts < floor:
                    continue
                perLevel[level] += 1
                key = (level, mod, func, normalise(msg))
                g = groups.get(key)
                if g is None:
                    # count, first, last, sources, example
                    groups[key] = g = [0, ts, ts, collections.Counter(), msg]
                g[0] += 1
                if ts < g[1]:
                    g[1] = ts
                if ts > g[2]:
                    g[2] = ts
                g[3][src] += 1
                if detail:
                    detailLines.append((ts, src, level, mod, func, msg))

    if progress is not None and not progress(total, total, ""):
        raise _Cancelled()

    return _format(since, levels, files, scanned, groups, perLevel,
                   detailLines, detail)


def _sources(counter):
    """-> 'cameras/09_Jungle,cameras/09_WestTerrace,+14' for the summary."""
    names = [n for n, _ in counter.most_common(2)]
    text = ",".join(names)
    if len(counter) > 2:
        text += ",+%d" % (len(counter) - 2)
    return text


def _format(since, levels, files, scanned, groups, perLevel, detailLines,
            detail):
    out = []
    add = out.append

    add("LOG ERRORS AND WARNINGS")
    add("generated %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("window    %s -> now" % (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(since))
                                 if since else "everything retained"))
    add("levels    %s" % ", ".join(levels))
    add("files     %d found, %d readable" % (len(files), scanned))
    counts = ", ".join("%s %d" % (lv, perLevel.get(lv, 0)) for lv in levels)
    add("matched   %d lines (%s) in %d distinct message shapes"
        % (sum(perLevel.values()), counts, len(groups)))
    add("")

    if not groups:
        add("Nothing matched.  If that is a surprise, check the start time"
            " against the oldest entry the logs still hold -- they rotate.")
        return "\n".join(out)

    add("=" * 118)
    add("SUMMARY -- distinct message shapes, most frequent first")
    add("Digits, hex values and paths are replaced before grouping, so one row"
        " covers every occurrence that differs only in its numbers.")
    add("=" * 118)
    add("%-7s %8s  %-17s %-17s %-28s %s"
        % ("LEVEL", "COUNT", "FIRST", "LAST", "SOURCE(S)", "MODULE.FUNC"))
    add("%-7s %8s  %-17s %-17s %-28s %s"
        % ("", "", "", "", "", "EXAMPLE"))
    add("-" * 118)
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1][0]):
        level, mod, func, _ = key
        count, first, last, srcs, example = g
        add("%-7s %8d  %-17s %-17s %-28.28s %s"
            % (level, count, first[5:], last[5:], _sources(srcs),
               "%s.%s" % (mod, func)))
        add("%-7s %8s  %-17s %-17s %-28s %s"
            % ("", "", "", "", "", example[:_kExampleLen]))
    add("")

    if not detail:
        add("(per-line listing omitted)")
        return "\n".join(out)

    add("=" * 118)
    add("ALL MATCHING LINES -- %d, in time order" % len(detailLines))
    add("=" * 118)
    for ts, src, level, mod, func, msg in sorted(detailLines):
        add("%s  %-7s %-22.22s %-34.34s %s"
            % (ts, level, src, "%s.%s" % (mod, func), msg))
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    since = None
    levels = kAllLevels
    detail = True
    while args:
        a = args.pop(0)
        if a == "--since":
            st = time.strptime(args.pop(0), "%Y-%m-%d %H:%M")
            since = time.mktime(st)
        elif a == "--errors":
            levels = (kErrorLevel,)
        elif a == "--warnings":
            levels = (kWarningLevel,)
        elif a == "--no-detail":
            detail = False
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sys.stderr.write("unknown argument: %s\n" % a)
            return 2
    try:
        sys.stdout.write(buildReport(since, levels, detail=detail))
        sys.stdout.write("\n")
    except _Cancelled:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
