r"""Per-camera health: what went wrong, when, and what footage is missing.

Two independent views of the same run, because neither alone is trustworthy:

  INCIDENTS come from logs\cameras\*.log.  They say what the app noticed.
  COVERAGE  comes from the clipdb.  It says what actually landed on disk.

A camera can log nothing and still have holes (the recorder died quietly), and
it can log constantly while recording fine (analysis flaps, stream-copy keeps
running).  Read both columns before blaming anything.

Coverage windows never end at "now": clip registration lags recording by
minutes (a segment is finalized, probed, re-timestamped, then inserted), so the
tail of every camera looks like a gap that isn't one.  LAG_SECS trims it.

Usage:  camhealth.py [--since HH:MM] [--gap SECS] [camera ...]
"""
import collections
import glob
import os
import re
import sqlite3
import sys
import time

LOGDIR = r"C:\Users\Bernie\AppData\Local\Sighthound Video Py3\logs\cameras"
CLIPDB = r"C:\Users\Bernie\AppData\Local\Sighthound Video Py3\videos\clipdb"

# Registration trails recording; anything newer than this is "not yet", not
# "missing".  See the module docstring.
LAG_SECS = 240
# Consecutive segments jitter by well under a second; a real hole is bigger.
GAP_SECS = 5.0

# Detail lines printed per camera.  The default window is now several days,
# so an uncapped list buries the summary table it is meant to explain.
EVENT_CAP = 12

# Ordered most- to least-severe.  Each entry is (label, regex).  The label is
# what gets counted and printed, so keep them short enough to tabulate.
PATTERNS = [
    ("recorder-stall",   re.compile(r"nothing reached the archive in")),
    ("stream-timeout",   re.compile(r"Stream timeout")),
    ("recover",          re.compile(r"re-opening stream in-process \(attempt (\d)")),
    # Both spellings: the message was reworded 2026-08-09 to name the real
    # reason, and old logs still carry the misleading original.
    ("recorder-restart", re.compile(r"recorder is on a different uri|"
                                    r"restarting the recorder:")),
    ("remux-start",      re.compile(r"remux: starting")),
    ("rung-drop",        re.compile(r"falls back to (\w+)")),
    ("short-segments",   re.compile(r"-> SHORT")),
    ("decode-error",     re.compile(r"remux ffmpeg: .*(error while decoding|"
                                    r"Invalid data|corrupt|missing picture)")),
    ("connect-fail",     re.compile(r"remux ffmpeg: .*(Connection refused|"
                                    r"timed out|401 Unauthorized|"
                                    r"Immediate exit requested)")),
    # "no frames!" moved to DEBUG on 2026-09-02 -- it is an expected outcome
    # for a track shorter than the analysis cadence, not an error -- and is now
    # a field on the per-camera "detect ...s:" stats line.  Matching the old
    # text here would silently report zero and read as "fixed".
    # NOTE the semantics changed with it: this counts REPORTING INTERVALS that
    # saw at least one miss, not individual misses.  The exact per-interval
    # count is on the line itself.
    ("no-frames",        re.compile(r"noFrames=(?!0\b)\d+")),
    ("large-delay",      re.compile(r"Large inter-frame delay")),
    ("ts-anomaly",       re.compile(r"Timestamp anomaly")),
    # Camera clock / outage accounting.  A camera whose packet timeline claims
    # more time than the file was open has JUMPED, not stalled -- treating that
    # as an outage fabricates black footage and inflates the registered span
    # (see FINDINGS item 5).  Kept separate from real outages on purpose.
    ("clock-jump",       re.compile(r"camera timestamps jumped")),
    ("fill-abandoned",   re.compile(r"overran its .*budget and was abandoned")),
    ("fill-disabled",    re.compile(r"gap fills are too slow")),
    ("span-capped",      re.compile(r"capping the span")),
    ("mtime-ceiling",    re.compile(r"no same-run successor")),
]

TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d\d\d)")
LAG = re.compile(r"analysis lag probe: demuxed (\d+), delivered (\d+), "
                 r"behind (\d+) frame\(s\) at ([\d.]+) fps = \+([\d.]+)s")
FPS = re.compile(r"profile: ([\d.]+) fps \|.*decode wait ([\d.]+) ms")
SEG = re.compile(r"remux: (\S+\.mp4) holds ([\d.]+)s")
# "had N outage(s) totalling Xs -> filled black" -- seconds of outage that ARE
# visible as black.  Contrast with fill-abandoned, whose line records only the
# budget, never the outage length, so those seconds can never be summed.
FILL = re.compile(r"had (\d+) outage\(s\) totalling ([\d.]+)s -> filled black")
# "claims Xs of packet timeline in a file written over Ys" -- the excess is how
# far the camera's clock ran away from real time in one segment.
JUMP = re.compile(r"claims ([\d.]+)s of packet timeline in a file "
                  r"written over ([\d.]+)s")


def parse_ts(line):
    m = TS.match(line)
    if not m:
        return None
    st = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return time.mktime(st) + int(m.group(2)) / 1000.0


def hhmmss(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def logs_for(cam, logdir=None):
    """Every log file for one camera, current AND rotated, oldest first.

    Rotation gives <cam>.log, <cam>.log.1 ... <cam>.log.N with .1 the most
    recent backup, so plain filename order is WRONG -- sort by the first
    timestamp actually inside each file.  Without this the tool can only ever
    see the current file and no multi-day question can be answered.
    """
    logdir = logdir or LOGDIR
    paths = glob.glob(os.path.join(logdir, cam + ".log")) +             glob.glob(os.path.join(logdir, cam + ".log.*"))
    dated = []
    for q in paths:
        first = None
        try:
            with open(q, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    first = parse_ts(line)
                    if first is not None:
                        break
        except Exception:
            continue
        dated.append((first if first is not None else 0.0, q))
    return [q for _, q in sorted(dated)]


def camera_names(logdir=None):
    """Camera base names, from current and rotated logs alike."""
    logdir = logdir or LOGDIR
    names = set()
    for q in glob.glob(os.path.join(logdir, "*.log*")):
        names.add(os.path.basename(q).split(".log")[0])
    return sorted(names)


def scan_camera(paths, since):
    """-> (counts, events, lags, fpsSamples, shortSegs, extra)

    `extra` carries the outage/clock accounting: black-filled seconds, the
    worst single clock excess, and a night/day split of the jumps.
    """
    counts = collections.Counter()
    events = []          # (t, label, line) for the severe ones
    lags, fpss, shorts = [], [], []
    extra = {"blackSecs": 0.0, "outages": 0, "worstJump": 0.0,
             "jumpNight": 0, "jumpDay": 0, "first": None, "last": None}
    for path in paths:
      with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            t = parse_ts(line)
            if t is None or (since and t < since):
                continue
            if extra["first"] is None or t < extra["first"]:
                extra["first"] = t
            if extra["last"] is None or t > extra["last"]:
                extra["last"] = t
            for label, rx in PATTERNS:
                if rx.search(line):
                    counts[label] += 1
                    if label not in ("ts-anomaly", "large-delay", "remux-start",
                                     "clock-jump", "span-capped",
                                     "mtime-ceiling"):
                        events.append((t, label, line.rstrip()))
                    break
            m = LAG.search(line)
            if m:
                lags.append(float(m.group(5)))
            m = FPS.search(line)
            if m:
                fpss.append((float(m.group(1)), float(m.group(2))))
            m = SEG.search(line)
            if m and float(m.group(2)) < 45.0:
                shorts.append((t, m.group(1), float(m.group(2))))
            m = FILL.search(line)
            if m:
                extra["outages"] += int(m.group(1))
                extra["blackSecs"] += float(m.group(2))
            m = JUMP.search(line)
            if m:
                excess = float(m.group(1)) - float(m.group(2))
                if excess > extra["worstJump"]:
                    extra["worstJump"] = excess
                hour = time.localtime(t).tm_hour
                if hour >= 19 or hour < 7:
                    extra["jumpNight"] += 1
                else:
                    extra["jumpDay"] += 1
    return counts, events, lags, fpss, shorts, extra


def snap(path):
    uri = "file:" + path.replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    s = sqlite3.connect(uri, uri=True)
    d = sqlite3.connect(":memory:")
    s.backup(d)
    s.close()
    return d


def coverage(since, gap_secs, clipdb=None):
    """-> {camLoc: (covered_secs, span_secs, [(start, end, secs), ...])}

    `clipdb` overrides the module default so a GUI caller can pass the app's
    real data dir rather than relying on the hardcoded path.
    """
    db = snap(clipdb or CLIPDB)
    rows = db.execute("SELECT camLoc, firstMs, lastMs FROM clips "
                      "ORDER BY camLoc, firstMs").fetchall()
    by = collections.defaultdict(list)
    for cam, a, b in rows:
        by[cam].append((a / 1000.0, b / 1000.0))

    horizon = time.time() - LAG_SECS
    out = {}
    for cam, spans in by.items():
        # Merge, then walk the seams.  Overlapping rows exist (they are their
        # own bug); merging keeps them from reading as negative gaps.
        merged = []
        for s, e in sorted(spans):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        start = max(since, merged[0][0]) if since else merged[0][0]
        holes = []
        for i in range(1, len(merged)):
            a, b = merged[i - 1][1], merged[i][0]
            if b - a > gap_secs and b > start:
                holes.append((a, b, b - a))
        covered = sum(min(e, horizon) - max(s, start)
                      for s, e in merged if min(e, horizon) > max(s, start))
        span = max(horizon - start, 0.0)
        out[cam] = (covered, span, holes)
    return out



class _Cancelled(Exception):
    """Raised out of buildReport when the progress callback asks to stop."""


def oldestLogTime(logdir=None):
    """Earliest timestamp across all retained logs, or None.

    Cheap by design -- logs_for() already stops at the first parseable line of
    each file, so this reads ~50 first-lines rather than 182 MB.  Measured at
    0.08 s for 17 cameras, which is what makes it usable as a dialog default.
    """
    oldest = None
    for cam in camera_names(logdir):
        paths = logs_for(cam, logdir)
        if not paths:
            continue
        try:
            with open(paths[0], "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    t = parse_ts(line)
                    if t is not None:
                        if oldest is None or t < oldest:
                            oldest = t
                        break
        except Exception:
            continue
    return oldest


def buildReport(since=None, gapSecs=GAP_SECS, want=None, logdir=None,
                clipdb=None, progress=None):
    """The report as a string, so callers other than the CLI can use it.

    @param  progress  optional callable(done, total, camName) invoked once per
                      camera; return False to cancel (raises _Cancelled).
    @param  logdir    override LOGDIR -- the GUI passes the app's real data dir
    @param  clipdb    override CLIPDB, likewise
    """
    want = want or []
    lines = []
    def _w(s=""):
        lines.append(s)
    cov = coverage(since, gapSecs, clipdb)
    cams = camera_names(logdir)
    if want:
        cams = [c for c in cams if any(w in c.lower() for w in want)]

    _w("camera health  %s%s\n"
          % (time.strftime("%Y-%m-%d %H:%M:%S"),
             "  since " + time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
             if since else "  (all retained logs)"))
    hdr = ("%-22s %5s %5s %5s %5s %5s %6s %6s %6s %6s %5s %5s %5s"
           % ("camera", "tmout", "rstrt", "stall", "derr", "short",
              "cover%", "gaps", "jumps", "worstS", "blackS", "aband", "capd"))
    _w(hdr)
    _w("-" * len(hdr))

    detail = []
    span_first = span_last = None
    for _i, cam in enumerate(cams):
        if progress is not None and not progress(_i, len(cams), cam):
            raise _Cancelled()
        paths = logs_for(cam, logdir)
        if not paths:
            continue
        counts, events, lags, fpss, shorts, extra = scan_camera(paths, since)
        c, sp, holes = cov.get(cam, (0.0, 0.0, []))
        pct = (100.0 * c / sp) if sp > 0 else float("nan")
        if extra["first"] is not None:
            span_first = min(span_first or extra["first"], extra["first"])
            span_last = max(span_last or extra["last"], extra["last"])
        _w("%-22s %5d %5d %5d %5d %5d %6.1f %6d %6d %6.0f %5.0f %5d %5d"
              % (cam,
                 counts["stream-timeout"],
                 counts["recorder-restart"] + counts["recover"],
                 counts["recorder-stall"],
                 counts["decode-error"] + counts["connect-fail"],
                 len(shorts),
                 pct, len(holes),
                 counts["clock-jump"], extra["worstJump"],
                 extra["blackSecs"], counts["fill-abandoned"],
                 counts["span-capped"]))
        if events or holes or shorts or counts["clock-jump"]:
            detail.append((cam, events, holes, shorts, fpss, extra, counts))

    # Legend, because two of these columns are easy to read backwards.
    _w()
    _w("  jumps  = camera clock ran AHEAD of real time (NOT an outage);"
          "  worstS = worst single excess, seconds")
    _w("  blackS = seconds of real outage recorded AS BLACK -- missing"
          " time you can SEE")
    _w("  aband  = fill gave up, outage smoothed away instead -- missing"
          " time you CANNOT see.  Its line records only the budget,")
    _w("           never the outage length, so those seconds cannot be"
          " summed; read cover% for what is really missing.")
    if span_first is not None:
        _w("  window read: %s .. %s"
              % (time.strftime("%Y-%m-%d %H:%M", time.localtime(span_first)),
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(span_last))))

    for cam, events, holes, shorts, fpss, extra, counts in detail:
        _w("\n=== %s ===" % cam)
        if counts["clock-jump"]:
            _w("  clock jumps %d  (night 19-07: %d, day: %d)  worst excess %.0fs"
                  % (counts["clock-jump"], extra["jumpNight"], extra["jumpDay"],
                     extra["worstJump"]))
        if extra["outages"] or counts["fill-abandoned"]:
            _w("  outages %d filled black totalling %.0fs; %d fill(s) abandoned%s"
                  % (extra["outages"], extra["blackSecs"],
                     counts["fill-abandoned"],
                     "; GAP FILLS DISABLED here" if counts["fill-disabled"] else ""))
        if counts["mtime-ceiling"]:
            _w("  run-boundary mtime ceiling used %d time(s)"
                  % counts["mtime-ceiling"])
        if fpss:
            # Summarised, not dumped: over a multi-day window the raw sample
            # list runs to hundreds of numbers per camera.
            f = sorted(x for x, _ in fpss)
            w = sorted(x for _, x in fpss)
            _w("  fps min/med/max %.1f/%.1f/%.1f over %d samples  |  decode wait med %.0f ms"
                  % (f[0], f[len(f)//2], f[-1], len(f), w[len(w)//2]))
        if len(events) > EVENT_CAP:
            _w("  ... %d earlier events not shown (EVENT_CAP=%d)"
                  % (len(events) - EVENT_CAP, EVENT_CAP))
        for t, label, line in events[-EVENT_CAP:]:
            _w("  %s  %-16s %s" % (hhmmss(t), label, line.split(" - ")[-1][:110]))
        if len(shorts) > EVENT_CAP:
            _w("  ... %d more short segments" % (len(shorts) - EVENT_CAP))
        for t, name, secs in shorts[-EVENT_CAP:]:
            _w("  %s  short-segment    %s held %.1fs" % (hhmmss(t), name, secs))
        if holes:
            tot = sum(x[2] for x in holes)
            _w("  ARCHIVE GAPS: %d totalling %.0fs (%.1f h); %d largest:"
                  % (len(holes), tot, tot / 3600.0, min(EVENT_CAP, len(holes))))
        for a, b, secs in sorted(holes, key=lambda h: -h[2])[:EVENT_CAP]:
            _w("  %s  ARCHIVE GAP      %s .. %s  (%.0fs)"
                  % (hhmmss(a), hhmmss(a), hhmmss(b), secs))
    return chr(10).join(lines)


def main():
    argv = sys.argv[1:]
    since, gap_secs, want = None, GAP_SECS, []
    i = 0
    while i < len(argv):
        if argv[i] == "--since":
            # Accept "HH:MM" (today) or "YYYY-MM-DD HH:MM" for a multi-day
            # window -- the retained logs go back several days, and the
            # interesting questions ("is this every night?") span them.
            arg = argv[i + 1]
            if "-" in arg:
                since = time.mktime(time.strptime(arg, "%Y-%m-%d %H:%M"))
            else:
                hh, mm = arg.split(":")
                lt = time.localtime()
                since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                     int(hh), int(mm), 0, 0, 0, -1))
            i += 2
        elif argv[i] == "--days":
            since = time.time() - float(argv[i + 1]) * 86400.0
            i += 2
        elif argv[i] == "--gap":
            gap_secs = float(argv[i + 1]); i += 2
        else:
            want.append(argv[i].lower()); i += 1

    try:
        print(buildReport(since, gap_secs, want))
    except _Cancelled:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
