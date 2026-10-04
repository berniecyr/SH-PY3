r"""What each camera actually detected, by type, over a period.

Reads objdb2 -- one row per TRACKED OBJECT, which is the unit the rest of the
app counts in too, so these numbers line up with what Search shows.

Two things to know before reading the output:

  `object` is not a thing that was seen, it is a thing that was NOT identified.
  It is a track the motion gate promoted and the classifier either never got to
  or refused to name.  It is ~92 % of all rows, so it gets its own column
  rather than being folded into the total -- watch it for gate-noise
  regressions after any sensitivity or threshold change.

  Sub-types come from objectAttributes and only exist where the detector named
  one (dog, car, cat, bird...).  They are a strict subset of the rows above.

Usage:  SystemHealthDetectionReport.py [--since "YYYY-MM-DD HH:MM"]
"""
import collections
import os
import sqlite3
import sys
import time

# Default only; the GUI passes the app's real storage location instead.
OBJDB = r"C:\Users\Bernie\AppData\Local\Sighthound Video Py3\videos\objdb2"

# Ordered as they matter operationally, with the unidentified bucket last.
kTypeOrder = ("person", "animal", "vehicle", "object")

# Sub-type columns are chosen by volume; the rest are summed into "other" so a
# long tail of one-off labels cannot make the table unreadable.
_kMaxSubTypeCols = 12

# Reading a 355 MB live database.  Deliberately NOT SystemHealthCamReport.snap()
# -- that backs the whole file up into memory, which is right for the 70 MB
# clipdb and absurd for this one when the queries are two GROUP BYs.  A
# read-only connection cannot corrupt anything and does not block the writer;
# the busy timeout covers the moment a checkpoint holds the file.
_kBusyTimeoutMs = 5000


class _Cancelled(Exception):
    """Raised out of buildReport when the progress callback asks to stop."""


def openRead(path):
    """-> a read-only connection to a live database."""
    uri = "file:" + path.replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.execute("PRAGMA busy_timeout=%d" % _kBusyTimeoutMs)
    return db


def _step(progress, done, total, what):
    if progress is not None and not progress(done, total, what):
        raise _Cancelled()


def buildReport(since=None, objdb=None, progress=None):
    """The report as a string.

    @param  since     Epoch seconds; only objects starting at or after this.
    @param  objdb     Path to objdb2; defaults to the module constant.
    @param  progress  callable(done, total, what) -> False to cancel.
    """
    path = objdb or OBJDB
    sinceMs = int(since * 1000) if since else 0

    _step(progress, 0, 4, "opening the database")
    db = openRead(path)
    try:
        _step(progress, 1, 4, "counting by type")
        byType = db.execute(
            "SELECT camLoc, type, COUNT(*) FROM objects "
            "WHERE timeStart >= ? GROUP BY camLoc, type",
            (sinceMs,)).fetchall()

        _step(progress, 2, 4, "counting by sub-type")
        bySub = db.execute(
            "SELECT o.camLoc, a.subType, COUNT(*) "
            "FROM objects o JOIN objectAttributes a ON a.objUid = o.uid "
            "WHERE o.timeStart >= ? AND a.subType IS NOT NULL "
            "  AND a.subType <> '' "
            "GROUP BY o.camLoc, a.subType",
            (sinceMs,)).fetchall()

        _step(progress, 3, 4, "reading the retained span")
        retained = db.execute(
            "SELECT MIN(timeStart), MAX(timeStop) FROM objects").fetchone()
    finally:
        db.close()
    _step(progress, 4, 4, "")

    return _format(since, path, byType, bySub, retained)


def _stamp(ms):
    if not ms:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000.0))


def _format(since, path, byType, bySub, retained):
    out = []
    add = out.append

    perCam = collections.defaultdict(collections.Counter)
    for cam, typ, n in byType:
        perCam[cam][typ or "object"] += n
    grand = collections.Counter()
    for counts in perCam.values():
        grand.update(counts)

    add("DETECTIONS BY CAMERA")
    add("generated %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("window    %s -> now" % (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(since))
                                 if since else "everything retained"))
    add("database  %s" % path)
    add("retained  %s -> %s   (the database is trimmed, so a wider window"
        " cannot show more)" % (_stamp(retained[0]), _stamp(retained[1])))
    add("objects   %d in %d cameras" % (sum(grand.values()), len(perCam)))
    add("")

    if not perCam:
        add("No objects in this window.")
        return "\n".join(out)

    # Types present but not in the fixed order still have to appear, or the
    # row totals would silently disagree with the column sum.
    extra = sorted(set(grand) - set(kTypeOrder))
    cols = [t for t in kTypeOrder if grand.get(t)] + extra

    add("=" * (24 + 10 * len(cols) + 12))
    add("BY TYPE -- one row per tracked object")
    add("=" * (24 + 10 * len(cols) + 12))
    head = "%-24s" % "camera"
    for t in cols:
        head += "%10s" % t
    head += "%12s" % "TOTAL"
    add(head)
    add("-" * len(head))
    for cam in sorted(perCam, key=lambda c: -sum(perCam[c].values())):
        counts = perCam[cam]
        row = "%-24.24s" % cam
        for t in cols:
            row += "%10d" % counts.get(t, 0)
        row += "%12d" % sum(counts.values())
        add(row)
    add("-" * len(head))
    row = "%-24s" % "TOTAL"
    for t in cols:
        row += "%10d" % grand.get(t, 0)
    row += "%12d" % sum(grand.values())
    add(row)
    add("")
    if grand.get("object"):
        share = 100.0 * grand["object"] / max(sum(grand.values()), 1)
        add("`object` means UNIDENTIFIED, not a fifth kind of thing: a track"
            " the motion gate")
        add("promoted that the classifier never named.  It is %.1f%% of this"
            " window -- that is the" % share)
        add("number to watch after any sensitivity or threshold change.")
    add("")
    return "\n".join(out + _subTypeTable(bySub))

def _subTypeTable(bySub):
    """-> the sub-type section as a list of lines (empty if nothing is named)."""
    if not bySub:
        return ["BY SUB-TYPE -- nothing in this window carried a sub-type."]

    perCam = collections.defaultdict(collections.Counter)
    grand = collections.Counter()
    for cam, sub, n in bySub:
        perCam[cam][sub] += n
        grand[sub] += n

    cols = [s for s, _ in grand.most_common(_kMaxSubTypeCols)]
    tail = sorted(set(grand) - set(cols))
    if tail:
        cols.append("other")

    out = []
    add = out.append
    width = 24 + 9 * len(cols) + 12
    add("=" * width)
    add("BY SUB-TYPE -- only objects the detector named; a subset of the above")
    add("=" * width)
    head = "%-24s" % "camera"
    for s in cols:
        head += "%9.8s" % s
    head += "%12s" % "TOTAL"
    add(head)
    add("-" * len(head))

    def cell(counts, name):
        if name != "other":
            return counts.get(name, 0)
        return sum(n for s, n in counts.items() if s in tail)

    for cam in sorted(perCam, key=lambda c: -sum(perCam[c].values())):
        counts = perCam[cam]
        row = "%-24.24s" % cam
        for s in cols:
            row += "%9d" % cell(counts, s)
        row += "%12d" % sum(counts.values())
        add(row)
    add("-" * len(head))
    row = "%-24s" % "TOTAL"
    for s in cols:
        row += "%9d" % cell(grand, s)
    row += "%12d" % sum(grand.values())
    add(row)
    if tail:
        add("")
        add("other: %s" % ", ".join(tail))
    return out


def main():
    args = sys.argv[1:]
    since = None
    while args:
        a = args.pop(0)
        if a == "--since":
            since = time.mktime(time.strptime(args.pop(0), "%Y-%m-%d %H:%M"))
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sys.stderr.write("unknown argument: %s\n" % a)
            return 2
    if not os.path.isfile(OBJDB):
        sys.stderr.write("no such database: %s\n" % OBJDB)
        return 2
    try:
        sys.stdout.write(buildReport(since))
        sys.stdout.write("\n")
    except _Cancelled:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
