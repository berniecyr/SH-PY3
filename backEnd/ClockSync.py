#! /usr/local/bin/python

#*****************************************************************************
#
# ClockSync.py
#     Measures how far this computer's clock has drifted from real time.
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
Measures the offset between this computer's clock and real (NTP) time.

Every clip and event time in the app is stamped from the PC clock, while the
cameras burn their own (NTP-synced) clock into the picture.  A PC clock that
drifts therefore makes recorded times silently disagree with what the cameras
show, with nothing else looking broken.  This module provides the measurement;
DiskCleaner polls it and the back end warns the user.

Speaks SNTP (RFC 4330) directly over UDP so it needs no admin rights, no
external binary and no third-party package.  Deliberately best-effort: a
machine with no internet access returns None rather than raising, so a warning
is never manufactured out of a network failure.
"""

# Python imports...
import socket
import struct
import time


# Constants...

# Public NTP pools, tried in order until we have enough samples.
_kNtpServers = ('pool.ntp.org', 'time.windows.com')

_kNtpPort = 123

# Per-request timeout.  Kept short: this runs on the cleanup thread, and a
# missing answer just means we skip the check this cycle.
_kNtpTimeoutSecs = 3.0

# Samples to collect before we stop asking.  The median of several readings
# rejects the occasional badly-delayed packet.
_kNtpSamples = 3

# Dropped replies tolerated per server before moving on to the next one.
_kNtpMaxMisses = 2

# Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
_kNtpToUnixDelta = 2208988800

_kNtpPacketLen = 48


##############################################################################
def _querySntpOnce(host, timeoutSecs=_kNtpTimeoutSecs):
    """Perform one SNTP round trip against a server.

    @param  host         Hostname of the NTP server.
    @param  timeoutSecs  How long to wait for the reply.
    @return offset       Clock offset in seconds -- POSITIVE means this
                         computer is BEHIND real time -- or None on failure.
                         (Same sign convention as "w32tm /stripchart".)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeoutSecs)
        # LI=0 (no warning), VN=3, Mode=3 (client); rest of the packet zeroed.
        packet = b'\x1b' + 47 * b'\0'
        t1 = time.time()
        sock.sendto(packet, (host, _kNtpPort))
        data, _ = sock.recvfrom(256)
        t4 = time.time()
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass

    if data is None or len(data) < _kNtpPacketLen:
        return None

    # Bytes 32..39 = receive timestamp (t2), 40..47 = transmit timestamp (t3).
    rxSecs, rxFrac = struct.unpack('!II', data[32:40])
    txSecs, txFrac = struct.unpack('!II', data[40:48])
    if txSecs == 0:
        return None     # Kiss-o'-death / unsynchronized server.

    t2 = (rxSecs - _kNtpToUnixDelta) + (rxFrac / 4294967296.0)
    t3 = (txSecs - _kNtpToUnixDelta) + (txFrac / 4294967296.0)

    # Standard NTP offset, which cancels out the network round-trip:
    #   theta = ((t2 - t1) + (t3 - t4)) / 2
    return ((t2 - t1) + (t3 - t4)) / 2.0


##############################################################################
def measureClockOffset(servers=_kNtpServers, samples=_kNtpSamples,
                       timeoutSecs=_kNtpTimeoutSecs, logger=None):
    """Measure how far this computer's clock is from real time.

    Walks the server list until enough samples are gathered, then returns the
    MEDIAN so one delayed packet can't skew the answer.

    @param  servers      Iterable of NTP hostnames to try, in order.
    @param  samples      How many good readings to collect before stopping.
    @param  timeoutSecs  Per-request timeout.
    @param  logger       Optional logger for the per-server detail.
    @return offset       Offset in seconds -- POSITIVE means this computer is
                         BEHIND real time -- or None if no server answered.
    """
    offsets = []
    for host in servers:
        # Tolerate a couple of dropped packets before writing a server off --
        # pool.ntp.org rotates addresses and an occasional request goes
        # unanswered, which shouldn't cost us the whole day's check.
        misses = 0
        while len(offsets) < samples and misses < _kNtpMaxMisses:
            offset = _querySntpOnce(host, timeoutSecs)
            if offset is None:
                misses += 1
                continue
            offsets.append(offset)
        if len(offsets) >= samples:
            break

    if not offsets:
        if logger is not None:
            logger.info("Clock check: no NTP server could be reached")
        return None

    offsets.sort()
    median = offsets[len(offsets) // 2]
    if logger is not None:
        logger.debug("Clock check: %d NTP sample(s) %s -> median %.3fs" %
                     (len(offsets), ["%.3f" % o for o in offsets], median))
    return median
