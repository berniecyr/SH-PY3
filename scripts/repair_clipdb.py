#! /usr/bin/env python
#*****************************************************************************
#
# repair_clipdb.py
#     Lossless rebuild of a SQLite database whose INDEX b-trees are damaged.
#
#*****************************************************************************

"""Rebuild a damaged clipdb (or objdb) into a fresh file, losslessly.

WHY THIS EXISTS
Twice in four days (2026-08-04, 2026-08-08) the clipdb lost one index b-tree
while every table b-tree stayed intact:

    2026-08-04  IDX_CLIPS_FILENAME_CAMLOC  rootpage 5   48,261 rows recovered
    2026-08-08  IDX_CLIPS_PREVFILE         rootpage 6   40,869 rows recovered

Both times NOTHING was lost, because the damage was confined to a derived
structure.  The obvious repairs do not work: REINDEX and DROP INDEX both have
to read the broken b-tree first, and SQLite refuses to write to a file it
considers malformed.  So the fix is to copy the rows out and rebuild.

This is deliberately a MANUAL tool, not something the app runs by itself.  A
blind rebuild destroys the evidence that makes the next diagnosis cheap (which
tree failed, how many rows still read, and -- as it turned out -- that the
damaged page contained OpenCV log text, which is what identified the cause).

RE-REGISTERING ORPHANS
Rebuilding recovers the rows that still exist.  It does NOT bring back clips
recorded while the database was broken -- those have no row at all, because the
insert is what was failing.  On 2026-08-08 that was 2,695 files / 12.6 GB in
three hours, and once the flag is lowered DiskCleaner's orphan sweep would
delete every one of them.  So the repair also walks the archive, finds .mp4
files with no row, and registers them: start time comes from the filename
(`<cam>/<date>/<YYYY-MM-DD-HHMMSS>.mp4`, local time) and length from ffmpeg.

USAGE
    python scripts/repair_clipdb.py                 # repair + re-register
    python scripts/repair_clipdb.py <db> [<out>]    # or an explicit file
    python scripts/repair_clipdb.py --check <db>    # report only, change nothing
    python scripts/repair_clipdb.py --reregister    # orphans only, no rebuild
    python scripts/repair_clipdb.py --no-reregister # rebuild only

STOP THE APP FIRST.  The script refuses to touch a database that another
process still holds open.
"""

import bisect
import concurrent.futures
import json
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import sys
import time


###############################################################################
def _defaultDbPath():
    local = os.environ.get('LOCALAPPDATA', '')
    return os.path.join(local, 'Sighthound Video Py3', 'videos', 'clipdb')


###############################################################################
def _integrity(path):
    """@return (ok, [problem lines]) without modifying anything."""
    try:
        con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'),
                              uri=True)
    except Exception as e:
        return False, ['cannot open: %r' % e]
    try:
        rows = [r[0] for r in con.execute('PRAGMA integrity_check(20)')]
    except Exception as e:
        rows = ['integrity_check failed: %r' % e]
    finally:
        con.close()
    return rows == ['ok'], rows


###############################################################################
def _inUse(path):
    """True when another process still has the database open.

    A rebuild while the app is writing would silently lose whatever it wrote
    after the copy started, so this is a hard stop rather than a warning.
    """
    for suffix in ('-wal', '-shm'):
        if os.path.exists(path + suffix):
            # A -shm present with a live owner cannot be renamed on Windows.
            try:
                os.rename(path + suffix, path + suffix + '.probe')
                os.rename(path + suffix + '.probe', path + suffix)
            except OSError:
                return True
    return False


###############################################################################
# Rowids fetched per attempt when recovering a damaged TABLE.  Big enough that a
# healthy stretch costs few queries, small enough that isolating one bad page
# does not drag a huge range through bisection.
_kRecoverBlock = 4096


def _copyRowidRange(s, d, name, quoted, ph, lo, hi):
    """Copy rowids [lo, hi], bisecting around pages that will not read.

    @return  (rowsCopied, rowidSlotsLost)
    """
    try:
        rows = s.execute('select %s from "%s" where rowid between ? and ?'
                         % (quoted, name), (lo, hi)).fetchall()
    except sqlite3.DatabaseError:
        if lo >= hi:
            return 0, 1          # single unreadable slot; nothing finer to try
        mid = (lo + hi) // 2
        okA, lostA = _copyRowidRange(s, d, name, quoted, ph, lo, mid)
        okB, lostB = _copyRowidRange(s, d, name, quoted, ph, mid + 1, hi)
        return okA + okB, lostA + lostB
    ok = lost = 0
    for row in rows:
        try:
            d.execute('insert into "%s" values (%s)' % (name, ph), row)
            ok += 1
        except Exception:
            lost += 1
    return ok, lost


def _copyTable(s, d, name, quoted, ph):
    """Copy one table: sequentially if it reads cleanly, by rowid if it does not.

    The sequential scan stays the fast path and the normal case -- an INDEX
    b-tree can be damaged without a table scan ever touching it, which is what
    the 2026-08-04 and 2026-08-08 incidents were, and why both were lossless.

    When the damage is in the TABLE b-tree instead, that scan dies partway; and
    if the bad page holds the LOWEST rowids it dies on the very first read, so
    the loop this replaced kept nothing at all.  Worse, the failure surfaced
    from execute() rather than fetchone(), outside the guard, so it propagated
    out of rebuild() and the repair aborted.  Measured 2026-09-02 on clipdb:
    rootpage 2 damaged at uid 89285-89498, which IS the table's minimum uid.

    Sweeping by rowid and bisecting around the bad pages recovers everything
    that still reads -- 191,022 of 191,236 rows in that incident, against 0
    before.

    @return  (rowsCopied, rowsLost)
    """
    ok = lost = 0
    try:
        cur = s.execute('select %s from "%s"' % (quoted, name))
        while True:
            row = cur.fetchone()
            if row is None:
                return ok, lost
            try:
                d.execute('insert into "%s" values (%s)' % (name, ph), row)
                ok += 1
            except Exception:
                lost += 1
    except sqlite3.DatabaseError:
        pass

    # Damaged table b-tree.  Start the table over so the two passes cannot
    # double-count, then take what the rowid sweep can reach.
    d.execute('delete from "%s"' % name)
    d.commit()
    try:
        bounds = s.execute('select min(rowid), max(rowid) from "%s"' % name).fetchone()
    except Exception:
        return 0, 0              # WITHOUT ROWID, or too damaged to bound
    if not bounds or bounds[0] is None:
        return 0, 0
    lo, hi = bounds
    ok = lost = 0
    u = lo
    while u <= hi:
        end = min(u + _kRecoverBlock - 1, hi)
        a, b = _copyRowidRange(s, d, name, quoted, ph, u, end)
        ok += a
        lost += b
        u = end + 1
    return ok, lost


def rebuild(src, dst):
    """Copy every readable row into a fresh database and rebuild the indexes.

    @return  dict of table -> (rowsCopied, rowsLost)
    """
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect('file:%s?mode=ro' % src.replace('\\', '/'), uri=True)
    d = sqlite3.connect(dst)
    try:
        schema = list(s.execute(
            "select type,name,sql from sqlite_master where sql is not null"))
        tables = [r for r in schema if r[0] == 'table']
        indexes = [r for r in schema if r[0] == 'index']

        # Tables first, with no indexes attached: inserts then never consult a
        # damaged tree and pay no index maintenance.
        for _t, _n, sql in tables:
            d.execute(sql)
        d.commit()

        counts = {}
        for _t, name, _sql in tables:
            cols = [r[1] for r in s.execute('pragma table_info("%s")' % name)]
            ph = ','.join('?' * len(cols))
            quoted = ','.join('"%s"' % c for c in cols)
            ok, lost = _copyTable(s, d, name, quoted, ph)
            d.commit()
            counts[name] = (ok, lost)

        for _t, name, sql in indexes:
            try:
                d.execute(sql)
            except Exception as e:
                print('    could not rebuild index %s: %s' % (name, e))
        d.commit()
        return counts
    finally:
        s.close()
        d.close()


###############################################################################
#                          re-registering orphan clips
###############################################################################

# archive/<camera folder>/<date>/<YYYY-MM-DD-HHMMSS>.mp4, and the substream
# gap-fill's promoted segments, which carry a "-sub" suffix and are ordinary
# registered clips (1,053 of them in the DB on 2026-08-08).  They are orphaned
# by a broken database exactly like main-stream segments, so they get the same
# treatment -- but they are NEVER chained, matching how the app registers them.
_kStampRe = re.compile(r'^(\d{4}-\d{2}-\d{2}-\d{6})(-sub)?\.mp4$', re.I)

# A segment written in the last couple of minutes may still be being finalized
# (the recorder re-timestamps and remuxes after the file first appears), so its
# length would be wrong.  Leave those for the running app to register normally.
_kSettleSecs = 180.0

# Segments are a minute; anything wildly outside that is not one of ours.
_kMaxClipSecs = 15 * 60

# Consecutive segments are chained so the timeline reads as one range rather
# than hundreds of separate ones.  The recorder leaves small gaps between
# segments, so allow a little slack.
_kChainGapMs = 10000


###########################################################
def _ffmpegExe():
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        from appCommon.InstallPaths import getFfmpegExe
        return getFfmpegExe()
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


###########################################################
def _durationMs(exe, path):
    """Length of a media file in ms, or None.

    Deliberately a copy of StreamReader._probe_duration_ms rather than an
    import: this tool has to run when the app's own imports may be the thing
    that is broken.
    """
    try:
        proc = subprocess.run([exe, '-hide_banner', '-i', path],
                              capture_output=True, timeout=30)
        for line in proc.stderr.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if line.startswith('Duration:'):
                stamp = line.split('Duration:', 1)[1].split(',', 1)[0].strip()
                if stamp.startswith('N/A'):
                    return None
                hh, mm, rest = stamp.split(':')
                return int((int(hh) * 3600 + int(mm) * 60 + float(rest)) * 1000)
    except Exception:
        pass
    return None


###########################################################
def _archiveRoot(dbPath):
    """Where the .mp4 files actually live, per the app's own preferences."""
    dataDir = os.path.dirname(os.path.dirname(dbPath))
    try:
        with open(os.path.join(dataDir, 'backEndPrefs'), 'rb') as f:
            videoDir = pickle.load(f).get('videoDir', '')
        if videoDir and os.path.isdir(os.path.join(videoDir, 'archive')):
            return os.path.join(videoDir, 'archive')
    except Exception:
        pass
    guess = os.path.join(os.path.dirname(dbPath), 'archive')
    return guess if os.path.isdir(guess) else None


###########################################################
def findOrphans(dbPath, archiveRoot):
    """Archive files with no row in clips.

    @return  (orphans, knownCount) where each orphan is
             {'rel','path','camLoc','firstMs'} and camLoc carries the DB's own
             capitalisation (the folder name is lowercased on disk).
    """
    con = sqlite3.connect('file:%s?mode=ro' % dbPath.replace('\\', '/'),
                          uri=True)
    try:
        known = set(r[0] for r in con.execute('select filename from clips'))
        # camLoc as the DB spells it, keyed by the lowercase folder name.
        camLocs = {}
        sizes = {}
        for loc, in con.execute('select distinct camLoc from clips'):
            camLocs[loc.lower()] = loc
        for loc, w, h in con.execute(
                'select camLoc, procWidth, procHeight from clips '
                'group by camLoc'):
            sizes[loc] = (w or 640, h or 360)
    finally:
        con.close()

    now = time.time()
    orphans = []
    for camDir in sorted(os.listdir(archiveRoot)):
        camPath = os.path.join(archiveRoot, camDir)
        if not os.path.isdir(camPath):
            continue
        camLoc = camLocs.get(camDir.lower(), camDir)
        for dayDir in sorted(os.listdir(camPath)):
            dayPath = os.path.join(camPath, dayDir)
            if not os.path.isdir(dayPath):
                continue
            for name in sorted(os.listdir(dayPath)):
                m = _kStampRe.match(name)
                if not m:
                    continue
                rel = '%s/%s/%s' % (camDir, dayDir, name)
                if rel in known:
                    continue
                full = os.path.join(dayPath, name)
                try:
                    if now - os.path.getmtime(full) < _kSettleSecs:
                        continue        # still being written/finalized
                except OSError:
                    continue
                try:
                    firstMs = int(time.mktime(time.strptime(
                        m.group(1), '%Y-%m-%d-%H%M%S')) * 1000)
                except ValueError:
                    continue
                orphans.append({'rel': rel, 'path': full, 'camLoc': camLoc,
                                'firstMs': firstMs, 'isSub': bool(m.group(2)),
                                'size': sizes.get(camLoc, (640, 360))})
    return orphans, len(known)


###########################################################
def reregister(dbPath, orphans, progress=None):
    """Insert a row for each orphan.  @return (added, skipped, clamped)."""
    if not orphans:
        return 0, 0, 0

    exe = _ffmpegExe()
    done = [0]

    def measure(o):
        ms = _durationMs(exe, o['path'])
        done[0] += 1
        if progress and done[0] % 100 == 0:
            progress(done[0], len(orphans))
        return o, ms

    good, skipped = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for o, ms in pool.map(measure, orphans):
            if not ms or ms <= 0 or ms > _kMaxClipSecs * 1000:
                skipped += 1
                continue
            o['lastMs'] = o['firstMs'] + ms
            good.append(o)

    # A segment's FILE can be longer than the wall-clock span it covers,
    # because finalizing pads holes with black.  Taken literally that makes a
    # clip end after the next one starts, and ClipManager.getFileAt warns and
    # picks an ARBITRARY row when two match a time.  So clamp each new clip to
    # the start of whatever comes next on that camera -- existing rows
    # included.  Only ever shortens; the padding was never real footage.
    starts = {}
    con = sqlite3.connect('file:%s?mode=ro' % dbPath.replace('\\', '/'),
                          uri=True)
    try:
        for loc, first in con.execute('select camLoc, firstMs from clips'):
            starts.setdefault(loc, []).append(first)
    finally:
        con.close()
    for o in good:
        starts.setdefault(o['camLoc'], []).append(o['firstMs'])
    for v in starts.values():
        v.sort()

    clamped = 0
    keep = []
    for o in good:
        v = starts[o['camLoc']]
        i = bisect.bisect_right(v, o['firstMs'])
        if i < len(v) and v[i] < o['lastMs']:
            o['lastMs'] = v[i]
            clamped += 1
        if o['lastMs'] <= o['firstMs']:
            skipped += 1          # entirely covered by the next clip
            continue
        keep.append(o)
    good = keep

    # Chain each camera's consecutive segments so the availability bar shows a
    # continuous range.  Only ever links re-registered clips to each other --
    # rows that survived the corruption are never modified.
    for c in good:
        c['prevFile'] = c['nextFile'] = ''
    byCam = {}
    for o in good:
        if not o['isSub']:      # gap-fill clips are registered unchained
            byCam.setdefault(o['camLoc'], []).append(o)
    for clips in byCam.values():
        clips.sort(key=lambda c: c['firstMs'])
        for i in range(len(clips) - 1):
            a, b = clips[i], clips[i + 1]
            if 0 <= b['firstMs'] - a['lastMs'] <= _kChainGapMs:
                a['nextFile'] = b['rel']
                b['prevFile'] = a['rel']

    con = sqlite3.connect(dbPath)
    try:
        con.executemany(
            'insert into clips (filename, camLoc, firstMs, lastMs, prevFile, '
            'nextFile, isCache, procWidth, procHeight) '
            'values (?,?,?,?,?,?,1,?,?)',
            [(o['rel'], o['camLoc'], o['firstMs'], o['lastMs'], o['prevFile'],
              o['nextFile'], o['size'][0], o['size'][1]) for o in good])
        con.commit()
    finally:
        con.close()
    return len(good), skipped, clamped


###########################################################
def _doReregister(dbPath):
    """Find and register orphans, reporting as it goes.  @return count added."""
    root = _archiveRoot(dbPath)
    if not root:
        print('could not locate the archive folder; skipping re-registration.')
        return 0
    print('\nscanning %s for unregistered clips...' % root)
    orphans, known = findOrphans(dbPath, root)
    print('  %d registered, %d unregistered' % (known, len(orphans)))
    if not orphans:
        return 0

    byCam = {}
    for o in orphans:
        byCam[o['camLoc']] = byCam.get(o['camLoc'], 0) + 1
    for cam, n in sorted(byCam.items()):
        print('    %-22s %5d' % (cam, n))

    print('  measuring durations (this takes a minute)...')
    added, skipped, clamped = reregister(
        dbPath, orphans,
        progress=lambda d, t: print('    %d/%d' % (d, t)))
    print('  registered %d clip(s)%s%s' %
          (added,
           ', skipped %d unreadable' % skipped if skipped else '',
           ', %d trimmed to the next clip' % clamped if clamped else ''))
    return added


###############################################################################
def main(argv):
    args = [a for a in argv[1:] if not a.startswith('--')]
    checkOnly = '--check' in argv
    reregisterOnly = '--reregister' in argv
    noReregister = '--no-reregister' in argv

    src = args[0] if args else _defaultDbPath()
    if not os.path.isfile(src):
        print('no such database: %s' % src)
        return 2

    print('database: %s (%.1f MB)' % (src, os.path.getsize(src) / 1e6))
    ok, rows = _integrity(src)
    print('integrity_check: %s' % ('ok' if ok else 'FAILED'))
    for r in rows[:8]:
        if r != 'ok':
            print('    %s' % r)
    if checkOnly:
        # Report the orphan count too -- it is the number that says how much
        # footage is currently invisible to Search.
        root = _archiveRoot(src)
        if root:
            try:
                orphans, known = findOrphans(src, root)
                print('registered clips: %d, unregistered files: %d'
                      % (known, len(orphans)))
            except Exception as e:
                print('could not scan the archive: %r' % e)
        print('--check given: stopping without changing anything.')
        return 0 if ok else 1

    if _inUse(src):
        print('\nREFUSING: another process still has this database open.\n'
              'Stop Sighthound Video (including its camera processes) and '
              'run this again.')
        return 3

    if ok:
        print('nothing to repair.')
        # A healthy database with orphans is a different situation: those files
        # may be ones the app deliberately abandoned, so registering them is
        # opt-in rather than automatic.
        if reregisterOnly:
            added = _doReregister(src)
            print('\ndone.  %d clip(s) are visible in Search again.' % added)
        else:
            print('(pass --reregister to also register any unregistered '
                  'archive files.)')
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = args[1] if len(args) > 1 else src + '.rebuilt-' + stamp
    print('\nrebuilding into %s' % out)
    counts = rebuild(src, out)
    total = sum(c[0] for c in counts.values())
    lostAny = sum(c[1] for c in counts.values())
    for name, (n, lost) in sorted(counts.items()):
        print('  %-18s %7d rows%s'
              % (name, n, '  (%d unreadable)' % lost if lost else ''))

    ok2, rows2 = _integrity(out)
    print('rebuilt integrity_check: %s' % ('ok' if ok2 else 'STILL FAILING'))
    if not ok2:
        print('  leaving the original untouched; rebuilt file kept for '
              'inspection at %s' % out)
        return 4

    # Keep the damaged original: it is the only evidence of the next cause.
    backup = src + '.corrupt-' + stamp
    os.rename(src, backup)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(src + suffix):
            try:
                os.rename(src + suffix, backup + suffix)
            except OSError:
                pass
    shutil.move(out, src)
    print('\nrepaired.  %d rows total, %d unreadable.' % (total, lostAny))
    print('damaged original kept at %s -- keep it, it is the evidence for '
          'diagnosing why this happened.' % backup)

    # Now the other half.  The rebuild recovered the rows that survived; the
    # clips recorded WHILE the database was broken have no row at all, and
    # clearing the flag below is what makes them deletable.  Register them
    # first, so lowering the flag is safe.
    added = 0
    if not noReregister:
        try:
            added = _doReregister(src)
        except Exception as e:
            print('re-registration failed (%r).  The rebuilt database is fine; '
                  'leaving the corruption flag UP so nothing gets swept.' % e)
            return 5

    # Lower the flag so DiskCleaner resumes its orphan sweep.  The app also
    # clears it on a clean startup check; doing it here means the operator does
    # not have to wait for a restart to get normal disk management back.
    try:
        dataDir = os.path.dirname(os.path.dirname(src))
        flag = os.path.join(dataDir, 'db-corruption-detected')
        if os.path.isfile(flag):
            os.remove(flag)
            print('cleared %s -- the orphan sweep will resume.' % flag)
    except Exception as e:
        print('could not clear the corruption flag: %r' % e)
    print('\ndone.  %d rows recovered, %d clip(s) re-registered.'
          % (total, added))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
