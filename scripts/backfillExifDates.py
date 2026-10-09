#! /usr/local/bin/python

"""
## @file
Fill the EXIF date-taken fields (files.exifDate / files.exifTime) for media
records that were analysed before those fields existed.

    venv\\Scripts\\python.exe scripts\\backfillExifDates.py [path\\to\\usermedia.db]

Backs the database up next to itself first, then reads each record's EXIF.
Safe to run again: records already filled, or without an EXIF date, are left
alone.  New analyses fill the fields themselves.
"""

# Python imports...
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Local imports...
from backEnd.UserMediaDb import UserMediaDb, getUserMediaDbPath


def _counts(db):
    c = db._conn
    total = c.execute('SELECT COUNT(*) FROM files').fetchone()[0]
    dated = c.execute('SELECT COUNT(*) FROM files WHERE exifDate IS NOT NULL').fetchone()[0]
    return total, dated


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else getUserMediaDbPath()
    base, ext = os.path.splitext(path)
    backup = '%s.backup-%s%s' % (base, time.strftime('%Y%m%d-%H%M%S'), ext or '.db')
    # The backup API copies a consistent snapshot even while the app is open.
    src, dst = sqlite3.connect(path, timeout=15), sqlite3.connect(backup)
    src.backup(dst)
    ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
    dst.close(); src.close()
    print('Backup: %s (integrity %s)' % (backup, ok))
    if ok != 'ok':
        return 1

    db = UserMediaDb().open(path)
    total, dated = _counts(db)
    print('Before: %d records, %d with an EXIF date' % (total, dated))

    def progress(done, count):
        if done % 1000 == 0 or done == count:
            print('  checked %d of %d' % (done, count))

    checked, filled = db.backfillExifDates(progress)
    total, dated = _counts(db)
    print('Checked %d records: %d got a date and time, %d have no EXIF date'
          % (checked, filled, checked - filled))
    print('After: %d records, %d with an EXIF date' % (total, dated))
    db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
