"""Exact frame presentation times read from an MP4's sample tables.

`ClipReader.getMsList` historically returned a synthetic uniform ladder,
`int(i*1000/fps)` built from cv2's frame count and average frame rate.  That is
an estimate: it assumes every frame is evenly spaced.  Recorded segments come
from RTSP cameras by stream copy, so a camera that stalls mid-segment leaves a
real gap in the timestamps and the estimate walks away from the truth -- measured
at up to 1.4s over a 60s segment on this fleet, which is enough to cut a saved
sub-clip in the wrong place or to push a lookup past the end of the ladder.

This module reads the times the file actually carries, out of the `stbl` sample
tables in `moov`.  It touches only the header (a few KB, always at the front of
these files), so it neither decodes nor spawns anything: ~0.2ms per clip against
the ~63ms a `cv2.VideoCapture` open costs.

Deliberately narrow: non-fragmented MP4 only, which is all the recorder writes.
Anything else returns None so the caller can fall back.
"""

import struct


# A file whose first box is none of these is not an MP4 we should be reading.
_kTopLevelBoxes = frozenset((b'ftyp', b'moov', b'mdat', b'free', b'skip',
                             b'wide', b'pnot', b'styp', b'uuid', b'meta'))

# Guard against a corrupt count field asking us to build a huge list.  24h at
# 60fps is ~5.2M samples; recorded segments are 60s, so this is pure paranoia.
_kMaxSamples = 6000000


###############################################################
def _boxes(f, end):
    """Yield (type, payloadStart, boxEnd) for each box up to end.

    Stops cleanly -- rather than raising -- at a truncated or nonsensical box,
    so a partially written file degrades to "no usable index" instead of an
    exception in a caller that has no idea what an atom is.

    @param  f    Open binary file, positioned at the first box.
    @param  end  Absolute offset one past the last byte available to this level.
    """
    while True:
        start = f.tell()
        if start + 8 > end:
            return
        header = f.read(8)
        if len(header) < 8:
            return
        size, typ = struct.unpack('>I4s', header)
        payload = start + 8
        if size == 1:
            # 64-bit extended size follows the type.
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack('>Q', ext)[0]
            payload = start + 16
        elif size == 0:
            # Runs to the end of the enclosing container.
            size = end - start
        if size < (payload - start) or start + size > end:
            return
        yield typ, payload, start + size
        f.seek(start + size)


###############################################################
def _findBox(f, start, end, name):
    """Return (payloadStart, boxEnd) of the first child box named `name`."""
    f.seek(start)
    for typ, payload, boxEnd in _boxes(f, end):
        if typ == name:
            return payload, boxEnd
    return None


###############################################################
def _readTimescaleAndDuration(f, start):
    """Read timescale and duration out of an mvhd or mdhd box.

    The two boxes carry these fields at the same offsets; only the box version
    changes their widths.
    """
    f.seek(start)
    version = f.read(1)[0]
    f.read(3)                                   # flags
    if version == 1:
        f.read(16)                              # creation + modification (64)
        timescale = struct.unpack('>I', f.read(4))[0]
        duration = struct.unpack('>Q', f.read(8))[0]
    else:
        f.read(8)                               # creation + modification (32)
        timescale = struct.unpack('>I', f.read(4))[0]
        duration = struct.unpack('>I', f.read(4))[0]
    return timescale, duration


###############################################################
def _readRuns(f, start, end, signed):
    """Read a run-length table -- stts or ctts -- as [(count, value), ...]."""
    f.seek(start)
    version = f.read(1)[0]
    f.read(3)                                   # flags
    entryCount = struct.unpack('>I', f.read(4))[0]
    if entryCount * 8 > end - f.tell():
        return None
    raw = f.read(entryCount * 8)
    if len(raw) < entryCount * 8:
        return None
    # ctts offsets are signed only from version 1; stts is always unsigned.
    fmt = '>Ii' if (signed and version == 1) else '>II'
    return [struct.unpack_from(fmt, raw, i * 8) for i in range(entryCount)]


###############################################################
def _readEditList(f, start, end):
    """Read edts/elst as [(segmentDuration, mediaTime, mediaRate), ...].

    Every file this fleet records carries an elst, and every one observed so far
    has mediaTime 0 -- i.e. it is a no-op.  It is read anyway: an edit list that
    ever did shift the media would move every offset we hand back, silently and
    with nothing in any log to say so.
    """
    f.seek(start)
    version = f.read(1)[0]
    f.read(3)                                   # flags
    entryCount = struct.unpack('>I', f.read(4))[0]
    width = 20 if version == 1 else 12
    if entryCount * width > end - f.tell():
        return []
    entries = []
    for _ in range(entryCount):
        if version == 1:
            segDur, mediaTime = struct.unpack('>Qq', f.read(16))
        else:
            segDur, mediaTime = struct.unpack('>Ii', f.read(8))
        rate = struct.unpack('>I', f.read(4))[0]
        entries.append((segDur, mediaTime, rate))
    return entries


###############################################################
def _expandRuns(runs, limit):
    """Flatten [(count, delta), ...] into a running sum, one entry per sample."""
    out = []
    acc = 0
    for count, delta in runs:
        if count < 0 or len(out) + count > limit:
            return None
        if delta == 0:
            out.extend([acc] * count)
        else:
            out.extend(range(acc, acc + count * delta, delta))
            acc += count * delta
    return out


###############################################################
def _findTrackByHandler(f, moovStart, moovEnd, handler):
    """Return (trakStart, trakEnd, mdia) for the first trak with this handler.

    Selecting by handler matters more than it looks.  On the low-frame-rate
    cameras here the AUDIO track holds MORE samples than the video track, so any
    "pick the biggest track" shortcut silently indexes the audio and reports a
    ladder several seconds longer than the video really is.

    @param  handler  Four-byte handler type, e.g. b'vide' or b'soun'.
    """
    f.seek(moovStart)
    traks = [(s, e) for typ, s, e in _boxes(f, moovEnd) if typ == b'trak']
    for trakStart, trakEnd in traks:
        mdia = _findBox(f, trakStart, trakEnd, b'mdia')
        if mdia is None:
            continue
        hdlr = _findBox(f, mdia[0], mdia[1], b'hdlr')
        if hdlr is None:
            continue
        f.seek(hdlr[0])
        f.read(8)                               # version/flags + pre_defined
        if f.read(4) == handler:
            return trakStart, trakEnd, mdia
    return None


###############################################################
def _findVideoTrack(f, moovStart, moovEnd):
    """Return (trakStart, trakEnd, mdia) for the trak whose handler is 'vide'."""
    return _findTrackByHandler(f, moovStart, moovEnd, b'vide')


###############################################################
def _findMoov(f):
    """Locate the moov box, rejecting anything not shaped like an MP4.

    @param  f    Open binary file.
    @return info (moovStart, moovEnd, topLevelBoxes), or None if this isn't a
                 file whose offsets we should trust.  topLevelBoxes is handed
                 back so a caller can also test for fragmentation without
                 re-walking the file.
    """
    f.seek(0, 2)
    fileEnd = f.tell()
    if fileEnd < 16:
        return None
    f.seek(0)

    # Reject anything not shaped like an MP4 before we trust any offset.
    first = next(_boxes(f, fileEnd), None)
    if first is None or first[0] not in _kTopLevelBoxes:
        return None

    f.seek(0)
    topLevel = list(_boxes(f, fileEnd))
    moov = next(((s, e) for typ, s, e in topLevel if typ == b'moov'), None)
    if moov is None:
        return None
    return moov[0], moov[1], topLevel


###############################################################
def _videoTrackIndex(path):
    """videoTrackIndex() without the exception guard."""
    with open(path, 'rb') as f:
        found = _findMoov(f)
        if found is None:
            return None
        moovStart, moovEnd, topLevel = found
        if any(typ == b'moof' for typ, _, _ in topLevel):
            return None                         # fragmented; samples aren't here
        if _findBox(f, moovStart, moovEnd, b'mvex') is not None:
            return None                         # fragmented

        movieTimescale = 1000
        mvhd = _findBox(f, moovStart, moovEnd, b'mvhd')
        if mvhd is not None:
            movieTimescale = _readTimescaleAndDuration(f, mvhd[0])[0] or 1000

        track = _findVideoTrack(f, moovStart, moovEnd)
        if track is None:
            return None
        trakStart, trakEnd, mdia = track

        mdhd = _findBox(f, mdia[0], mdia[1], b'mdhd')
        if mdhd is None:
            return None
        timescale, mediaDuration = _readTimescaleAndDuration(f, mdhd[0])
        if timescale <= 0:
            return None

        minf = _findBox(f, mdia[0], mdia[1], b'minf')
        if minf is None:
            return None
        stbl = _findBox(f, minf[0], minf[1], b'stbl')
        if stbl is None:
            return None
        sttsBox = _findBox(f, stbl[0], stbl[1], b'stts')
        if sttsBox is None:
            return None

        sttsRuns = _readRuns(f, sttsBox[0], sttsBox[1], signed=False)
        if not sttsRuns:
            return None
        times = _expandRuns(sttsRuns, _kMaxSamples)
        if not times:
            return None

        cttsBox = _findBox(f, stbl[0], stbl[1], b'ctts')
        if cttsBox is not None:
            cttsRuns = _readRuns(f, cttsBox[0], cttsBox[1], signed=True)
            if cttsRuns:
                offsets = []
                for count, value in cttsRuns:
                    offsets.extend([value] * count)
                if len(offsets) >= len(times):
                    # With B-frames decode order is not presentation order.
                    times = sorted(t + offsets[i] for i, t in enumerate(times))

        # Edit list: an initial empty edit (mediaTime -1) delays presentation;
        # the first real edit says which media time maps to presentation zero.
        delayMs = 0
        shift = 0
        edts = _findBox(f, trakStart, trakEnd, b'edts')
        if edts is not None:
            elst = _findBox(f, edts[0], edts[1], b'elst')
            if elst is not None:
                for segDur, mediaTime, _rate in _readEditList(f, elst[0],
                                                              elst[1]):
                    if mediaTime < 0:
                        delayMs += segDur * 1000 // max(1, movieTimescale)
                    else:
                        shift = mediaTime
                        break

        ptsMs = []
        for t in times:
            ticks = t - shift
            if ticks < 0:
                ticks = 0
            ptsMs.append(ticks * 1000 // timescale + delayMs)

        durTicks = mediaDuration - shift
        if durTicks < 0:
            durTicks = 0
        durationMs = durTicks * 1000 // timescale + delayMs

        return ptsMs, durationMs, timescale


###############################################################
def videoTrackIndex(path):
    """Exact presentation times of a file's video track.

    @param  path  Path to a video file.
    @return info  (ptsMsList, durationMs, timescale), where ptsMsList holds one
                  ascending timestamp per frame in ms from the start of
                  presentation -- or None if this is not a parseable,
                  non-fragmented MP4 with a video track.
    """
    try:
        return _videoTrackIndex(path)
    except Exception:
        return None


###############################################################
def hasAudioTrack(path):
    """Does this file declare an audio track?

    The alternative -- and what ClipReader used to do unconditionally -- is a
    full `ffmpeg -i` process spawn for one boolean, measured at 1.1s against
    this archive.  The answer is right there in the header: a `soun` handler in
    one of moov's traks.  Reading it costs ~3ms and touches only the first few
    KB of the file.

    Fragmentation is irrelevant here (unlike videoTrackIndex, which needs the
    sample tables): a fragmented file still declares its tracks in moov.

    @param  path      Path to a video file.
    @return hasAudio  True/False, or None if this isn't a parseable MP4 -- in
                      which case the caller should fall back to probing.
    """
    try:
        with open(path, 'rb') as f:
            found = _findMoov(f)
            if found is None:
                return None
            moovStart, moovEnd, _ = found
            return _findTrackByHandler(f, moovStart, moovEnd, b'soun') is not None
    except Exception:
        return None
