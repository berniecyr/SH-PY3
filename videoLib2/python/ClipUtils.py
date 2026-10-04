"""ClipUtils — extract, trim, and annotate clips.

The archive segments are already H.264 (the recorder stream-copies), so plain
trims/extracts are done with ffmpeg stream copy: no decode, no encode, no
frame buffering.  Only requests that change the pixels (bounding boxes,
timestamps, fps thinning, resizes, GIF/JPEG output) decode frames — and those
are streamed straight into an ffmpeg encoder (NVENC on the GPU when available,
libx264 otherwise) instead of being accumulated in memory.
"""

import datetime
import os
import re
import subprocess
import sys
import tempfile

# Never let an ffmpeg child pop its own console window: a console child with no
# parent console ALLOCATES A NEW, VISIBLE ONE, and the front end runs detached
# under pythonw with no console at all.  See ClipReader for the full note.
_kNoWindow = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def _get_ffmpeg():
    """Return path to ffmpeg binary, preferring imageio-ffmpeg's bundled copy.

    An installed build ships that same binary under a Sighthound name (so the
    recorders are identifiable in Task Manager); InstallPaths knows which one
    is there.  Falls back to imageio-ffmpeg directly for a source checkout.
    """
    try:
        from appCommon.InstallPaths import getFfmpegExe
        return getFfmpegExe()
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


# Generous ceilings: stream copies run at disk speed; encodes at encoder speed.
_kCopyTimeoutSecs = 600
_kEncodeTimeoutSecs = 1800

_kDurationRe = re.compile(rb'Duration:\s*(\d+):(\d+):(\d+)\.(\d+)')


def _run_ffmpeg(args, timeout=_kCopyTimeoutSecs):
    """Run ffmpeg -y <args>; True on exit code 0."""
    cmd = [_get_ffmpeg(), '-y'] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              creationflags=_kNoWindow)
    except Exception:
        return False
    return proc.returncode == 0


def _probe_duration_ms(path):
    """Container duration in ms parsed from `ffmpeg -i` stderr, or -1."""
    try:
        proc = subprocess.run([_get_ffmpeg(), '-hide_banner', '-i', path],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=30,
                              creationflags=_kNoWindow)
        m = _kDurationRe.search(proc.stderr or b'')
        if not m:
            return -1
        h, mnt, s, frac = m.groups()
        ms = ((int(h) * 60 + int(mnt)) * 60 + int(s)) * 1000
        ms += int(frac.ljust(3, b'0')[:3])
        return ms
    except Exception:
        return -1


def _creation_time_args(creationMs):
    """ffmpeg args stamping the QuickTime create-date atoms from an absolute
    epoch-ms, or [] when creationMs is None.

    A single global `-metadata creation_time=` sets BOTH the container
    (MediaCreateDate / mvhd) and each track (TrackCreateDate / tkhd) -- verified.
    The value is the LOCAL wall-clock time with a trailing 'Z', so ffmpeg stores
    those exact digits with no timezone shift and readers display them verbatim
    -- i.e. the atoms read back matching the local yyyy-mm-dd-hhmmss filename
    (naive local instead gets shifted to UTC on write).
    """
    if creationMs is None:
        return []
    iso = datetime.datetime.fromtimestamp(
        creationMs / 1000.0).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ['-metadata', 'creation_time=' + iso]


def stampMp4CreationTime(path, creationMs, logFn=None):
    """Best-effort: rewrite `path` in place adding QuickTime create-date atoms
    (MediaCreateDate + TrackCreateDate) from creationMs (absolute epoch-ms).

    A fast stream-copy remux (no re-encode); path-agnostic, so it works on any
    finished mp4 regardless of how it was produced.  On ANY failure `path` is
    left exactly as it was.  Returns True only if the stamped file replaced the
    original.
    """
    if creationMs is None or not path or not os.path.isfile(path):
        return False
    tmp = path + '.meta.tmp.mp4'
    args = (['-i', path, '-map', '0', '-c', 'copy', '-movflags', '+faststart'] +
            _creation_time_args(creationMs) + [tmp])
    try:
        if _run_ffmpeg(args) and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    if logFn is not None:
        try:
            logFn("stampMp4CreationTime: failed for %s" % path)
        except Exception:
            pass
    return False


def _concat_copy(segments, outPath, withAudio):
    """Stream-copy (no re-encode) segment windows into one MP4.

    segments — list of (srcPath, inpoint_s, outpoint_s) in playback order.
    Uses the ffmpeg concat demuxer; video cuts snap to the keyframe at or
    before each inpoint (recorder segments start on keyframes, so only the
    very first window can start early).

    @return  Output duration in ms (>=0) on success, else -1.
    """
    segments = [s for s in segments if s[2] > s[1] and os.path.isfile(s[0])]
    if not segments:
        return -1

    lines = ['ffconcat version 1.0']
    for srcPath, inpoint, outpoint in segments:
        p = os.path.abspath(srcPath).replace('\\', '/').replace("'", r"'\''")
        lines.append("file '%s'" % p)
        if inpoint > 0:
            lines.append('inpoint %.3f' % inpoint)
        lines.append('outpoint %.3f' % outpoint)

    listFd, listPath = tempfile.mkstemp(suffix='.txt', prefix='svconcat_')
    try:
        with os.fdopen(listFd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

        maps = ['-map', '0:v:0'] + (['-map', '0:a?'] if withAudio else ['-an'])
        args = (['-f', 'concat', '-safe', '0', '-i', listPath] + maps +
                ['-c', 'copy', '-avoid_negative_ts', 'make_zero',
                 '-movflags', '+faststart', outPath])
        if not _run_ffmpeg(args):
            return -1
        if not os.path.isfile(outPath) or os.path.getsize(outPath) == 0:
            return -1
        return _probe_duration_ms(outPath)
    finally:
        try:
            os.remove(listPath)
        except Exception:
            pass


def _mux_audio_segments(videoPath, outPath, segs):
    """Mux audio from one or more source segments onto an existing video file.

    The video is stream-copied (no re-encode); audio from each segment is
    decoded, optionally concatenated in order, and encoded to AAC.  Source
    files with no audio track, or any FFmpeg failure, cause this to return
    False so the caller can fall back to the video-only result.

    @param  videoPath  Existing video-only file (becomes output stream 0:v).
    @param  outPath    Destination file to write (video + audio).
    @param  segs       List of (srcPath, startSec, durSec) audio windows, in
                       playback order.
    @return            True if outPath was written with audio, else False.
    """
    segs = [s for s in segs if s[2] > 0 and os.path.isfile(s[0])]
    if not segs:
        return False

    cmd = [_get_ffmpeg(), '-y', '-i', videoPath]
    for srcPath, ss, dur in segs:
        cmd += ['-ss', '%.3f' % ss, '-t', '%.3f' % dur, '-i', srcPath]

    if len(segs) == 1:
        cmd += ['-map', '0:v:0', '-map', '1:a:0?']
    else:
        labels = ''.join('[%d:a]' % (i + 1) for i in range(len(segs)))
        cmd += ['-filter_complex',
                '%sconcat=n=%d:v=0:a=1[aout]' % (labels, len(segs)),
                '-map', '0:v:0', '-map', '[aout]']

    cmd += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart', '-shortest', outPath]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=180,
                              creationflags=_kNoWindow)
    except Exception:
        return False
    return (proc.returncode == 0 and os.path.isfile(outPath)
            and os.path.getsize(outPath) > 0)


try:
    import cv2
    import numpy as np
    _cv2_available = True
except ImportError:
    _cv2_available = False

try:
    from PIL import Image
    _pil_available = True
except ImportError:
    _pil_available = False


# NVENC availability is probed once per process with a tiny test encode; a
# mid-stream failure (session exhaustion) also flips this off for the rest of
# the process so retries take the libx264 path.
_nvencUsable = None


def _nvenc_ok():
    global _nvencUsable
    if _nvencUsable is None:
        _nvencUsable = _run_ffmpeg(
            ['-f', 'lavfi', '-i', 'color=black:s=64x64:r=10:d=0.2',
             '-c:v', 'h264_nvenc', '-f', 'null', '-'], timeout=30)
    return _nvencUsable


def _targetBitrateKbps(w, h, fps):
    """A sane H.264 target bitrate for exported clips.  NVENC's old
    '-cq 23 -b:v 0' (quality target, NO cap) over-allocated to ~40 Mbps at 4K —
    10x+ the ~3 Mbps HEVC source and huge/slow to write.  ~0.03 bits/px/frame
    gives good review quality at a sane size; floored/ceiled for other resolutions."""
    kbps = int((w * h * (fps or 24.0) * 0.03) / 1000.0)
    return max(1500, min(kbps, 12000))


def _open_pipe_encoder(outPath, fps, w, h, useNvenc):
    """Start an ffmpeg process encoding raw BGR frames from stdin to H.264."""
    tgt = _targetBitrateKbps(w, h, fps)
    maxr, buf = (tgt * 3) // 2, tgt * 2
    if useNvenc:
        # Bitrate-targeted VBR with a cap, instead of uncapped cq=23 -> ~40Mbps.
        vcodec = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr',
                  '-b:v', '%dk' % tgt, '-maxrate', '%dk' % maxr,
                  '-bufsize', '%dk' % buf]
    else:
        # CRF 23 is already reasonable; the cap just prevents high-motion spikes.
        vcodec = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                  '-maxrate', '%dk' % maxr, '-bufsize', '%dk' % buf]
    cmd = ([_get_ffmpeg(), '-y',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-s', '%dx%d' % (w, h), '-r', '%.6f' % fps, '-i', 'pipe:0'] +
           vcodec +
           ['-pix_fmt', 'yuv420p', '-movflags', '+faststart', outPath])
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=_kNoWindow)


def _open_capture(path):
    """Open a video file for decode, trying GPU (D3D11VA) first.

    Hardware decode can open successfully yet fail on the first read, so a
    probe frame validates it; on any trouble we fall back to software.  Frames
    are delivered as normal host BGR Mats either way.
    """
    if not _cv2_available:
        return None
    try:
        cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_HW_ACCELERATION,
                                cv2.VIDEO_ACCELERATION_D3D11])
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return cap
        cap.release()
    except Exception:
        pass
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    return cap if cap.isOpened() else None


# Codecs NVDEC (ffmpeg cuvid) can hardware-decode for export.
_kExportCuvid = {'hevc': 'hevc_cuvid', 'h265': 'hevc_cuvid',
                 'h264': 'h264_cuvid', 'avc': 'h264_cuvid'}


def _cuvidForPath(path):
    """cuvid decoder name for the file's video codec, or None.  Probed once via
    ffmpeg's stream banner."""
    try:
        r = subprocess.run([_get_ffmpeg(), '-hide_banner', '-i', path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=15, creationflags=_kNoWindow)
        s = r.stderr.decode('utf-8', 'replace').lower()
        vline = next((ln for ln in s.splitlines() if 'video:' in ln), '')
        for key, dec in _kExportCuvid.items():
            if key in vline:
                return dec
    except Exception:
        pass
    return None


class _ExportFrameSource:
    """Full-resolution BGR frame source for export.

    Prefers NVDEC (ffmpeg cuvid, on the GPU) — the export's cv2 D3D11VA path
    measured ~84ms/frame at 4K (slower even than software), while NVDEC is
    ~33ms AND offloads the CPU from the live cameras.  Falls back to cv2
    (_open_capture) if the codec isn't cuvid-decodable or NVDEC produces no
    frame, so behavior is unchanged where GPU decode is unavailable.

    read() -> writable BGR ndarray, or None at EOF.
    """

    def __init__(self, path, start_frame, fps, w, h):
        self._proc = None
        self._cap = None
        self._w = int(w)
        self._h = int(h)
        self._fb = self._w * self._h * 3
        self._pending = None

        decoder = _cuvidForPath(path) if (w and h) else None
        if decoder:
            try:
                cmd = [_get_ffmpeg(), '-hide_banner', '-loglevel', 'error',
                       '-nostdin']
                if start_frame > 0:
                    cmd += ['-ss', '%.4f' % (start_frame / (fps or 24.0))]
                cmd += ['-c:v', decoder, '-i', path,
                        '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=_kNoWindow)
                first = self._readRaw(proc)
                if first is not None:
                    self._proc = proc
                    self._pending = first
                    return
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

        # Fallback: cv2 (existing behavior).
        self._cap = _open_capture(path)
        if self._cap is not None and start_frame > 0:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    def _readRaw(self, proc):
        need = self._fb
        chunks = []
        got = 0
        rd = proc.stdout.read
        while got < need:
            c = rd(need - got)
            if not c:
                return None
            chunks.append(c)
            got += len(c)
        return b''.join(chunks)

    def read(self):
        if self._proc is not None:
            if self._pending is not None:
                raw, self._pending = self._pending, None
            else:
                raw = self._readRaw(self._proc)
            if raw is None:
                return None
            return np.frombuffer(raw, np.uint8).reshape(
                self._h, self._w, 3).copy()
        if self._cap is None:
            return None
        ok, bgr = self._cap.read()
        return bgr if ok else None

    def close(self):
        if self._proc is not None:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# Colour names emitted by DataManager._getLabelColorForType → BGR tuples
_kColorMap = {
    'yellow': (0,   255, 255),
    'orange': (0,   165, 255),
    'pink':   (147,  20, 255),
    'green':  (0,   200,   0),
    'blue':   (255,   0,   0),
}


def remuxClip(fileList, filePath, desiredFirstMs, desiredLastMs,
              configDir, extras, logFn=None, progressFn=None):
    """Extract a trimmed clip from one or more source MP4 files.

    fileList       — list of (abs_path, file_start_abs_ms) tuples, in order
    filePath       — output path (.mp4 or .gif)
    desiredFirstMs — inclusive start in absolute epoch-ms
    desiredLastMs  — inclusive end   in absolute epoch-ms
    configDir      — unused (was C-extension config dir)
    extras         — dict: enableTimestamps, drawBoxes, boxList,
                     use12HrTime, useUSDate, format, fps, maxSize
    logFn          — unused
    Returns number of frames written (>= 0) or -1 on failure.

    When nothing alters the pixels (no boxes/timestamps/fps/resize, MP4 out —
    the response-clip case), the sources are stream-copied: no decode, no
    re-encode, constant memory.  Otherwise frames are decoded, annotated, and
    streamed into an H.264 encoder (GPU NVENC when available).
    """
    draw_boxes  = extras.get('drawBoxes', False)
    enable_ts   = extras.get('enableTimestamps', False)
    out_format  = extras.get('format', 'mp4').lower()
    fps_limit   = extras.get('fps', 0) or 0
    max_size    = extras.get('maxSize', None)   # (maxW, maxH), 0 = unconstrained

    sorted_files = sorted((f for f in fileList if os.path.exists(f[0])),
                          key=lambda x: x[1])
    if not sorted_files:
        return -1
    if desiredLastMs <= desiredFirstMs:
        # "Export Frame" passes first == last (a single still).  Give the render
        # loop a small window so it can grab the frame at/after the requested
        # time; the image branch writes the first one and returns.  A zero/neg
        # window is only an error for time-based outputs (mp4/gif).
        if out_format in ('jpg', 'jpeg', 'png'):
            desiredLastMs = desiredFirstMs + 2000
        else:
            return -1

    needsDecode = (draw_boxes or enable_ts or fps_limit > 0 or bool(max_size)
                   or out_format != 'mp4')
    if not needsDecode:
        return _remuxClipCopy(sorted_files, filePath,
                              desiredFirstMs, desiredLastMs)
    return _renderClip(sorted_files, filePath, desiredFirstMs, desiredLastMs,
                       extras)


def _clipWindows(sorted_files, desiredFirstMs, desiredLastMs, clamp=False):
    """Per-file (path, startAbsMs, winStartAbsMs, winEndAbsMs) windows.

    Each file's window ends where the next file begins (files may have gaps
    between them; those gaps carry no content in either stream).

    With clamp=True each window is also clamped to the file's actual probed
    duration.  The concat demuxer trusts an outpoint even past EOF (advancing
    the output timeline by outpoint-inpoint), so an unclamped window spanning
    a recording gap would insert a frozen-frame hole into the clip; clamping
    compresses gaps out, matching the historical decode-based behavior.
    """
    wins = []
    for idx, (path, file_start_ms) in enumerate(sorted_files):
        next_start = (sorted_files[idx + 1][1]
                      if idx + 1 < len(sorted_files) else None)
        win_start = max(desiredFirstMs, file_start_ms)
        win_end = (desiredLastMs if next_start is None
                   else min(desiredLastMs, next_start))
        if clamp:
            file_dur = _probe_duration_ms(path)
            if file_dur > 0:
                win_end = min(win_end, file_start_ms + file_dur)
        if win_end <= win_start:
            continue
        wins.append((path, file_start_ms, win_start, win_end))
    return wins


def _remuxClipCopy(sorted_files, filePath, desiredFirstMs, desiredLastMs):
    """Build the clip by pure stream copy.  Returns ~frame count or -1."""
    wins = _clipWindows(sorted_files, desiredFirstMs, desiredLastMs,
                        clamp=True)
    if not wins:
        return -1
    segments = [(path, (ws - fs) / 1000.0, (we - fs) / 1000.0)
                for path, fs, ws, we in wins]
    expected_ms = sum(we - ws for _, _, ws, we in wins)

    out_dir = os.path.dirname(os.path.abspath(filePath))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Preferred: one pass copying video AND audio together (keeps A/V sync
    # exactly as the camera produced it).  Fails when segments have mixed
    # stream layouts (e.g. some lost audio) — then fall back to video-only
    # copy + the audio mux used historically.
    dur_ms = _concat_copy(segments, filePath, withAudio=True)
    if dur_ms < 0:
        tmp_video = filePath + '.video.mp4'
        dur_ms = _concat_copy(segments, tmp_video, withAudio=False)
        if dur_ms < 0:
            try:
                os.remove(tmp_video)
            except Exception:
                pass
            return -1
        # The first window's video may start up to a GOP before its inpoint
        # (keyframe snap) — extend the first audio window back by the same
        # amount so A/V stays aligned.
        delta_s = max(0.0, (dur_ms - expected_ms) / 1000.0)
        segs = []
        for i, (path, fs, ws, we) in enumerate(wins):
            ss = (ws - fs) / 1000.0
            dur = (we - ws) / 1000.0
            if i == 0 and delta_s > 0:
                shift = min(delta_s, ss)
                ss -= shift
                dur += shift
            segs.append((path, ss, dur))
        if _mux_audio_segments(tmp_video, filePath, segs):
            try:
                os.remove(tmp_video)
            except Exception:
                pass
        else:
            try:
                if os.path.exists(filePath):
                    os.remove(filePath)
                os.replace(tmp_video, filePath)
            except Exception:
                return -1

    # Approximate frame count for the (boolean-checked) return value.
    fps = 25.0
    if _cv2_available:
        try:
            cap = cv2.VideoCapture(wins[0][0], cv2.CAP_FFMPEG)
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS) or fps
            cap.release()
        except Exception:
            pass
    return max(1, int(dur_ms * fps / 1000.0))


def _renderClip(sorted_files, filePath, desiredFirstMs, desiredLastMs, extras):
    """Decode + annotate + encode path (boxes/timestamps/fps/resize/GIF/JPEG).

    Streams frames straight into the encoder — nothing is accumulated except
    the (already frame-capped) GIF buffer.
    """
    if not _cv2_available:
        return -1

    draw_boxes  = extras.get('drawBoxes', False)
    enable_ts   = extras.get('enableTimestamps', False)
    box_list    = extras.get('boxList', [])
    use_12hr    = extras.get('use12HrTime', False)
    use_us_date = extras.get('useUSDate', False)
    out_format  = extras.get('format', 'mp4').lower()
    fps_limit   = extras.get('fps', 0) or 0
    max_size    = extras.get('maxSize', None)

    # GIF: Pillow's encoder does ~50ms/frame, so limit to ~100 frames to
    # keep write time under ~5 seconds even without a progress dialog.
    if out_format == 'gif':
        duration_s = max(1.0, (desiredLastMs - desiredFirstMs) / 1000.0)
        if not fps_limit:
            # target at most 100 frames; floor at 2fps, cap at 8fps
            fps_limit = max(2, min(8, int(100 / duration_s)))
        if not max_size:
            max_size = (480, 0)

    # Source metadata from the first openable file.
    source_fps = None
    source_w = None
    source_h = None
    for path, _ in sorted_files:
        cap = _open_capture(path)
        if cap is None:
            continue
        source_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        source_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        break
    if source_fps is None or not source_w or not source_h:
        return -1

    keep_every = 1
    out_fps = float(source_fps)
    if fps_limit > 0 and fps_limit < source_fps:
        keep_every = max(1, int(round(source_fps / fps_limit)))
        out_fps = source_fps / keep_every

    # Output dimensions (even, for yuv420p).
    out_w, out_h = source_w, source_h
    if max_size:
        max_w, max_h = max_size
        if max_h and out_h > max_h:
            scale = max_h / float(out_h)
            out_w = max(1, int(out_w * scale))
            out_h = max_h
        if max_w and out_w > max_w:
            scale = max_w / float(out_w)
            out_h = max(1, int(out_h * scale))
            out_w = max_w
    out_w = max(2, out_w - (out_w % 2))
    out_h = max(2, out_h - (out_h % 2))

    # Fast time→boxes lookup.
    box_times = sorted(set(e[0] for e in box_list)) if box_list else []
    boxes_by_time = {}
    for entry in box_list:
        boxes_by_time.setdefault(entry[0], []).append(entry[1])

    out_dir = os.path.dirname(os.path.abspath(filePath))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    isMp4 = out_format not in ('gif', 'jpg', 'jpeg', 'png')
    tmp_video = filePath + '.video.mp4'
    encoder = None
    useNvenc = False
    gif_frames = []
    first_abs_ms = None
    last_abs_ms = None
    n = 0
    kept_idx = 0

    global _nvencUsable
    try:
        if isMp4:
            useNvenc = _nvenc_ok()
            encoder = _open_pipe_encoder(tmp_video, out_fps, out_w, out_h,
                                         useNvenc)

        for path, file_start_ms in sorted_files:
            fps = source_fps
            rel_first_ms = max(0, desiredFirstMs - file_start_ms)
            start_frame = int(rel_first_ms * fps / 1000.0)

            # NVDEC (GPU) full-res decode when available, else cv2 — same source
            # camera so every file shares source_w/source_h.
            src = _ExportFrameSource(path, start_frame, fps, source_w, source_h)
            frame_idx = start_frame

            try:
                while True:
                    abs_ms = file_start_ms + int(frame_idx * 1000.0 / fps)
                    if abs_ms > desiredLastMs:
                        break
                    bgr = src.read()
                    if bgr is None:
                        break
                    frame_idx += 1
                    if abs_ms < desiredFirstMs:
                        continue
                    kept_idx += 1
                    if keep_every > 1 and (kept_idx - 1) % keep_every != 0:
                        continue

                    if out_w != source_w or out_h != source_h:
                        bgr = cv2.resize(bgr, (out_w, out_h))

                    if draw_boxes and box_times:
                        best_t = min(box_times, key=lambda t: abs(t - abs_ms))
                        if abs(best_t - abs_ms) < 300:
                            for box_str in boxes_by_time[best_t]:
                                _draw_box(bgr, box_str, source_w, source_h,
                                          out_w, out_h)
                    if enable_ts:
                        _draw_timestamp(bgr, abs_ms, use_12hr, use_us_date)

                    if first_abs_ms is None:
                        first_abs_ms = abs_ms
                    last_abs_ms = abs_ms

                    if isMp4:
                        if not bgr.flags['C_CONTIGUOUS']:
                            bgr = bgr.copy()
                        encoder.stdin.write(bgr.tobytes())
                    elif out_format in ('jpg', 'jpeg', 'png'):
                        if encoder is not None:
                            encoder.kill()
                        return _write_image(bgr, filePath)
                    else:  # gif
                        gif_frames.append(bgr)
                    n += 1
            finally:
                src.close()   # always kill the NVDEC/cv2 source, even on error

        if n == 0 or first_abs_ms is None:
            if encoder is not None:
                try:
                    encoder.stdin.close()
                except Exception:
                    pass
                encoder.kill()
            return -1

        if not isMp4:  # gif
            return _write_gif(gif_frames, filePath, out_fps)

        encoder.stdin.close()
        rc = encoder.wait(timeout=_kEncodeTimeoutSecs)
        encoder = None
        if rc != 0 or not os.path.isfile(tmp_video) \
                or os.path.getsize(tmp_video) == 0:
            return -1

        # Mux the original audio over the rendered window.
        segs = []
        for path, fs, ws, we in _clipWindows(sorted_files, first_abs_ms,
                                             last_abs_ms):
            segs.append((path, (ws - fs) / 1000.0, (we - ws) / 1000.0))
        if _mux_audio_segments(tmp_video, filePath, segs):
            try:
                os.remove(tmp_video)
            except Exception:
                pass
        else:
            try:
                if os.path.exists(filePath):
                    os.remove(filePath)
                os.replace(tmp_video, filePath)
            except Exception:
                return -1
        return n
    except (BrokenPipeError, OSError):
        # Encoder died mid-stream (e.g. NVENC session refusal).  Disable NVENC
        # for this process so the caller's retry lands on libx264.
        if useNvenc:
            _nvencUsable = False
        return -1
    except Exception:
        return -1
    finally:
        if encoder is not None:
            try:
                encoder.stdin.close()
            except Exception:
                pass
            try:
                encoder.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------

def _draw_box(bgr, box_str, src_w, src_h, out_w, out_h):
    """Parse 'drawbox=x:y:w:h:procW:procH:uid:color:t=0' and draw onto bgr."""
    m = re.match(r'drawbox=(\d+):(\d+):(\d+):(\d+):(\d+):(\d+):\d+:(\w+):t=0',
                 box_str)
    if not m:
        return
    bx, by, bw, bh, proc_w, proc_h = (int(m.group(i)) for i in range(1, 7))
    color_name = m.group(7)
    if proc_w == 0 or proc_h == 0:
        return

    # proc coords → source coords → output coords
    sx  = bx * src_w / float(proc_w)
    sy  = by * src_h / float(proc_h)
    sw  = bw * src_w / float(proc_w)
    sh  = bh * src_h / float(proc_h)
    ox  = int(sx * out_w / float(src_w))
    oy  = int(sy * out_h / float(src_h))
    ow  = int(sw * out_w / float(src_w))
    oh  = int(sh * out_h / float(src_h))

    color = _kColorMap.get(color_name, (0, 200, 0))
    cv2.rectangle(bgr, (ox, oy), (ox + ow, oy + oh), color, 2)


def _draw_timestamp(bgr, abs_ms, use_12hr, use_us_date):
    """Draw a timestamp in the bottom-left corner of bgr (in-place)."""
    dt = datetime.datetime.fromtimestamp(abs_ms / 1000.0)
    if use_us_date == 'us' or use_us_date is True:
        date_str = dt.strftime('%m/%d/%Y')
    elif use_us_date == 'intl':
        date_str = dt.strftime('%d/%m/%Y')
    else:
        date_str = dt.strftime('%Y-%m-%d')
    time_str = dt.strftime('%I:%M:%S %p') if use_12hr  else dt.strftime('%H:%M:%S')
    label    = date_str + '  ' + time_str

    h, w = bgr.shape[:2]
    font  = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.4, w / 1280.0)
    thick = 1
    x, y  = 8, h - 8
    cv2.putText(bgr, label, (x + 1, y + 1), font, scale, (0, 0, 0), thick + 1, cv2.LINE_AA)
    cv2.putText(bgr, label, (x,     y    ), font, scale, (255, 255, 255), thick, cv2.LINE_AA)


def _write_image(frame, filePath):
    """Write a single frame as JPEG or PNG (extension determines format)."""
    ok = cv2.imwrite(filePath, frame)
    return 1 if ok else -1


def _write_gif(frames, filePath, fps):
    if not _pil_available:
        return -1
    duration_ms = max(20, int(1000.0 / fps))
    pil_frames  = [Image.fromarray(f[:, :, ::-1]) for f in frames]
    pil_frames[0].save(
        filePath, format='GIF',
        append_images=pil_frames[1:],
        save_all=True, duration=duration_ms, loop=0,
    )
    return len(frames)


# ---------------------------------------------------------------------------

def remuxSubClip(srcPath, dstPath, startOffset, stopOffset, configDir, logFn=None):
    """Extract a sub-range from a single source file by stream copy.

    startOffset / stopOffset — relative ms from start of the file.
    Returns actualStartOffset (>= 0) on success, or -1 on failure.

    The cut snaps to the keyframe at or before startOffset (content is never
    lost, the clip just starts a little earlier); the returned offset reports
    the ACTUAL achieved start so callers' database times stay truthful.
    """
    if not os.path.exists(srcPath):
        return -1
    if stopOffset <= startOffset:
        return -1

    out_dir = os.path.dirname(os.path.abspath(dstPath))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    src_dur_ms = _probe_duration_ms(srcPath)
    # Clamp to real content so an outpoint past EOF can't inflate the
    # timeline (see _clipWindows).
    if src_dur_ms > 0:
        stopOffset = min(stopOffset, src_dur_ms)
        if stopOffset <= startOffset:
            return -1
    seg = [(srcPath, max(0, startOffset) / 1000.0, stopOffset / 1000.0)]

    dur_ms = _concat_copy(seg, dstPath, withAudio=True)
    if dur_ms < 0:
        # Mixed/absent audio — copy video only, then mux audio the old way.
        tmp_video = dstPath + '.video.mp4'
        dur_ms = _concat_copy(seg, tmp_video, withAudio=False)
        if dur_ms < 0:
            try:
                os.remove(tmp_video)
            except Exception:
                pass
            return -1
        stop_eff = min(stopOffset, src_dur_ms) if src_dur_ms > 0 else stopOffset
        actual_start = max(0, int(stop_eff - dur_ms))
        aseg = [(srcPath, actual_start / 1000.0, dur_ms / 1000.0)]
        if _mux_audio_segments(tmp_video, dstPath, aseg):
            try:
                os.remove(tmp_video)
            except Exception:
                pass
        else:
            try:
                if os.path.exists(dstPath):
                    os.remove(dstPath)
                os.replace(tmp_video, dstPath)
            except Exception:
                return -1

    stop_eff = min(stopOffset, src_dur_ms) if src_dur_ms > 0 else stopOffset
    return max(0, int(stop_eff - dur_ms))


def getRealClipSize(clipPath, logFn=None):
    """Return native (width, height) of a clip, or (0, 0) on failure."""
    if not _cv2_available:
        return (0, 0)
    cap = cv2.VideoCapture(clipPath)
    if not cap.isOpened():
        return (0, 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return (w, h)


# ---------------------------------------------------------------------------

def _encodeStills(imagePaths, outPath, fps, w, h, useNvenc):
    """Stream stills into one encoder pass.  Returns frames written or -1."""
    global _nvencUsable
    encoder = _open_pipe_encoder(outPath, fps, w, h, useNvenc)
    n = 0
    try:
        for p in imagePaths:
            img = cv2.imread(p)
            if img is None:
                continue
            if img.shape[1] != w or img.shape[0] != h:
                img = cv2.resize(img, (w, h))
            if not img.flags['C_CONTIGUOUS']:
                img = img.copy()
            encoder.stdin.write(img.tobytes())
            n += 1
        encoder.stdin.close()
        rc = encoder.wait(timeout=_kEncodeTimeoutSecs)
        encoder = None
        if n > 0 and rc == 0 and os.path.isfile(outPath) \
                and os.path.getsize(outPath) > 0:
            return n
        return -1
    except (BrokenPipeError, OSError):
        if useNvenc:
            _nvencUsable = False   # session refused; whole process uses x264
        return -1
    except Exception:
        return -1
    finally:
        if encoder is not None:
            try:
                encoder.stdin.close()
            except Exception:
                pass
            try:
                encoder.kill()
            except Exception:
                pass


def makeSummaryVideo(imagePaths, outPath, fps=10, logFn=None):
    """Stitch a list of JPEG/PNG stills into a small H.264 montage.

    Each image becomes one output frame at `fps`.  Frames are streamed straight
    into the encoder (GPU NVENC when available, libx264 fallback) — nothing is
    accumulated in memory.  All frames are normalized to the first readable
    frame's (even) dimensions, so a mid-run thumbnail-size change can't break
    the encode.  Silent (no audio).

    @param  imagePaths  Ordered list of image file paths.
    @param  outPath     Destination .mp4.
    @param  fps         Output frame rate.
    @return n           Frames written (>0) on success, or -1.
    """
    if not _cv2_available or not imagePaths:
        return -1

    # First readable frame sets the canvas size (even dims for yuv420p).
    w = h = None
    for p in imagePaths:
        img = cv2.imread(p)
        if img is not None:
            h, w = img.shape[:2]
            break
    if not w or not h:
        return -1
    w = max(2, w - (w % 2))
    h = max(2, h - (h % 2))

    out_dir = os.path.dirname(os.path.abspath(outPath))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = outPath + '.tmp.mp4'

    try:
        modes = [True, False] if _nvenc_ok() else [False]
        for useNvenc in modes:
            n = _encodeStills(imagePaths, tmp, fps, w, h, useNvenc)
            if n > 0:
                os.replace(tmp, outPath)
                return n
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        return -1
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
