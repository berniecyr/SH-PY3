#! /usr/local/bin/python

#*****************************************************************************
#
# ShmHeal.py
#     Self-heal for a poisoned SQLite WAL-index (-shm) sidecar file.
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

"""
## @file
Self-heal for a poisoned SQLite WAL-index (-shm) sidecar.

Background (incident 2026-07-25/26): a native crash storm left a camera
process dead while it held the memory-mapped clipdb-shm, poisoning the
WAL-index ON DISK.  From then on every new connection failed with
"sqlite3.DatabaseError: file is not a database" even though the main database
file (and its WAL) were perfectly healthy -- and the supervisor restarted the
crashing camera processes all night.

The -shm file holds no durable data: it is a rebuildable index over the WAL.
When an open fails with that specific signature while the MAIN file has a
valid SQLite header, deleting the -shm and retrying the open recovers
everything (SQLite rebuilds the index from the WAL).  That is exactly what
this helper does -- with deliberately narrow conditions so it can never mask
real corruption.
"""

import os

# The first 16 bytes of every valid SQLite database file.
_kSqliteMagic = b'SQLite format 3\x00'

# The only error signature this heal applies to.  "database disk image is
# malformed" and friends mean REAL page corruption -- deleting the shm would
# not help and must not delay the real corruption handling.
_kHealableError = 'file is not a database'


def healPoisonedWalIndex(dbPath, logger=None, error=None):
    """Delete a poisoned -shm sidecar so SQLite can rebuild the WAL-index.

    Narrow by design; ALL of these must hold, else nothing is touched:
      - the reported error (if given) is "file is not a database"
      - a -shm sidecar exists for the database
      - the MAIN database file starts with the valid SQLite magic header
        (i.e. the database itself does not look corrupt)
      - the -shm can actually be deleted (fails while another live process
        still holds it mapped -- in which case healing wouldn't work anyway)

    @param  dbPath  Path of the main database file.
    @param  logger  Optional logger; the heal (or why it was skipped) is
                    logged so this never silently hides trouble.
    @param  error   The exception that made the caller consider healing, or
                    None to skip the message check (probe-style callers).
    @return healed  True if the -shm was removed and a retry makes sense.
    """
    def _log(fn, msg):
        if logger is not None:
            try:
                fn(msg)
            except Exception:
                pass

    if error is not None and _kHealableError not in str(error).lower():
        return False

    shmPath = dbPath + '-shm'
    try:
        if not os.path.isfile(shmPath):
            return False
        with open(dbPath, 'rb') as f:
            if f.read(len(_kSqliteMagic)) != _kSqliteMagic:
                _log(logger.warning, 'shm-heal: %s has an invalid header -- '
                     'real corruption, not healing' % dbPath)
                return False
        os.remove(shmPath)
    except Exception as e:
        _log(logger.warning, 'shm-heal: could not heal %s: %r (another '
             'process may still hold the shm mapped)' % (dbPath, e))
        return False

    _log(logger.warning, 'shm-heal: removed poisoned WAL-index %s -- the '
         'database file itself is valid; retrying open' % shmPath)
    return True
