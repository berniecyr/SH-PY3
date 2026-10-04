"""Shared-memory audio relay between the back end and the front end.

The camera's audio is captured once in the back-end process (one RTSP session)
and written into a small memory-mapped ring buffer per camera.  The front-end
monitor view reads that ring buffer and plays it, so live audio needs NO extra
camera connection — important because many cameras allow only 2 concurrent RTSP
sessions (already used by the backend's video capture and recording-audio).

Format: a fixed 64-byte header followed by a circular PCM body.
  header: magic 'SVA1', write_total (monotonic bytes ever written, int64),
          rate (int32), channels (int32), ring_size (int64).
PCM is signed 16-bit little-endian interleaved (sounddevice 'int16').

The writer only ever advances write_total; the reader tracks its own consumed
position and resynchronises if it falls more than the ring behind (overrun) or
gets too far ahead.  A torn read/write at most causes a brief audible glitch,
which is acceptable for a live monitor.
"""

import mmap
import os
import struct

_kMagic       = b'SVA1'
_kHeaderSize  = 64
_kHeaderFmt   = '<4sqiiq'           # magic, write_total, rate, channels, ring_size
_kDefaultRate = 44100
_kDefaultCh   = 2
_kRingSeconds = 4                   # ring capacity


def _frame_bytes(channels):
    return 2 * channels             # int16


class AudioRingWriter:
    """Back-end writer for one camera's live-audio ring buffer."""

    def __init__(self, path, rate=_kDefaultRate, channels=_kDefaultCh,
                 ring_seconds=_kRingSeconds):
        self._path     = path
        self._rate     = rate
        self._channels = channels
        self._ring     = int(ring_seconds * rate * _frame_bytes(channels))
        self._total    = 0
        self._mm       = None
        self._fh       = None

        size = _kHeaderSize + self._ring
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        # Allocate (or re-allocate) the backing file to the exact size.
        with open(path, 'wb') as f:
            f.write(b'\x00' * size)
        self._fh = open(path, 'r+b')
        self._mm = mmap.mmap(self._fh.fileno(), size)
        self._writeHeader()

    def _writeHeader(self):
        self._mm[0:_kHeaderSize] = struct.pack(
            _kHeaderFmt, _kMagic, self._total, self._rate, self._channels,
            self._ring).ljust(_kHeaderSize, b'\x00')

    def write(self, data):
        """Append PCM bytes to the ring and publish the new write_total."""
        if self._mm is None or not data:
            return
        ring = self._ring
        pos  = self._total % ring
        n    = len(data)
        base = _kHeaderSize
        if pos + n <= ring:
            self._mm[base + pos:base + pos + n] = data
        else:
            first = ring - pos
            self._mm[base + pos:base + ring] = data[:first]
            self._mm[base:base + (n - first)] = data[first:]
        self._total += n
        # Publish the updated counter last so a reader never sees data past it.
        self._mm[4:12] = struct.pack('<q', self._total)

    def close(self):
        try:
            if self._mm is not None:
                # Zero the magic so a reader treats the camera as "audio off".
                self._mm[0:4] = b'\x00\x00\x00\x00'
                self._mm.flush()
                self._mm.close()
        except Exception:
            pass
        self._mm = None
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            if self._path and os.path.isfile(self._path):
                os.remove(self._path)
        except Exception:
            pass


class AudioRingReader:
    """Front-end reader for one camera's live-audio ring buffer.

    Returns (rate, channels) via open(); read() yields newly-written PCM, kept
    close to live by skipping ahead if it falls too far behind.
    """

    # Target/maximum latency the reader keeps between itself and the writer.
    _kTargetLeadSec = 0.20
    _kMaxLeadSec    = 0.60

    def __init__(self, path):
        self._path     = path
        self._fh       = None
        self._mm       = None
        self._ring     = 0
        self._rate     = _kDefaultRate
        self._channels = _kDefaultCh
        self._read     = None      # our consumed byte total (None until primed)

    def open(self):
        """Open the ring file.  Returns (rate, channels) or None if unavailable."""
        try:
            self._fh = open(self._path, 'rb')
        except OSError:
            return None
        try:
            size = os.fstat(self._fh.fileno()).st_size
            if size <= _kHeaderSize:
                self.close()
                return None
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self.close()
            return None
        magic, total, rate, channels, ring = struct.unpack(
            _kHeaderFmt, self._mm[0:struct.calcsize(_kHeaderFmt)])
        if magic != _kMagic or ring <= 0 or rate <= 0 or channels <= 0:
            self.close()
            return None
        self._ring     = ring
        self._rate     = rate
        self._channels = channels
        self._read     = None
        return (rate, channels)

    def _writeTotal(self):
        return struct.unpack('<q', self._mm[4:12])[0]

    def read(self):
        """Return newly available PCM bytes (possibly b''), staying near live."""
        if self._mm is None:
            return b''
        # Camera audio stopped -> magic cleared.
        if self._mm[0:4] != _kMagic:
            return b''
        frame = _frame_bytes(self._channels)
        total = self._writeTotal()
        if self._read is None:
            lead = int(self._kTargetLeadSec * self._rate) * frame
            self._read = max(0, total - lead)

        avail = total - self._read
        if avail <= 0:
            return b''
        # Fell behind the ring (overrun) or drifted too far -> jump near live.
        max_lead = int(self._kMaxLeadSec * self._rate) * frame
        if avail > self._ring or avail > max_lead:
            target = int(self._kTargetLeadSec * self._rate) * frame
            self._read = total - target
            avail = total - self._read

        ring = self._ring
        pos  = self._read % ring
        base = _kHeaderSize
        if pos + avail <= ring:
            data = bytes(self._mm[base + pos:base + pos + avail])
        else:
            first = ring - pos
            data = bytes(self._mm[base + pos:base + ring]) + \
                   bytes(self._mm[base:base + (avail - first)])
        self._read += avail
        return data

    def close(self):
        try:
            if self._mm is not None:
                self._mm.close()
        except Exception:
            pass
        self._mm = None
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
