"""OpenCV-backed ClipReader for Sighthound Video Py3 port."""

import logging
import math
import os
import subprocess
import sys
import threading
import time

try:
    import cv2
    import numpy as np
    from PIL import Image
    _cv2_available = True
except ImportError:
    _cv2_available = False

# OpenCV's VideoCapture discards audio, so audio playback is handled separately:
# FFmpeg decodes the clip's audio track to raw PCM which is streamed to the
# default output device via sounddevice (PortAudio).  sounddevice is optional —
# if it isn't installed, playback is silent but otherwise unaffected.
try:
    import sounddevice as _sd
    _sd_available = True
except Exception:
    _sd_available = False

# Reads real frame times out of an MP4's sample tables; see getMsList().
# hasAudioTrack answers the same question _file_has_audio() used to spawn
# ffmpeg for.
try:
    from videoLib2.python.Mp4Index import videoTrackIndex, hasAudioTrack
except ImportError:
    videoTrackIndex = None
    hasAudioTrack = None

_kAudioRate       = 44100
_kAudioChannels   = 2
_kAudioBlockBytes = 8192     # multiple of frame size (int16 * 2ch = 4 bytes)

# Codecs NVDEC (ffmpeg cuvid) can hardware-decode, keyed by the container's
# fourcc.  Used to GPU-decode clips whose CPU decode can't keep up at playback
# rate (notably 4K HEVC on this GTX 1650 Ti).
_kCuvidByCodec = {
    'hevc': 'hevc_cuvid', 'h265': 'hevc_cuvid',
    'hvc1': 'hevc_cuvid', 'hev1': 'hevc_cuvid',
    'h264': 'h264_cuvid', 'avc1': 'h264_cuvid', 'avc3': 'h264_cuvid',
}

# Whether NVDEC actually decodes here is settled per-clip by _GpuPipeDecoder
# .start() actually producing a frame (the -decoders list showing cuvid does
# NOT guarantee a working GPU/driver) — deliberately not globally cached, so a
# single transient failure can't disable GPU decode for the whole session.

# Cap GPU playback output to a real-time pixel budget.  Decode+RGB download from
# the GPU scales with OUTPUT pixels, so a large (near-4K) playback window would
# stall to a few fps.  ~720p sustains comfortably >30fps on this GTX 1650 Ti
# even under camera/detection load; larger windows just upscale this frame
# (smooth playback beats pixel-perfect for reviewing footage).
_kGpuMaxPlaybackPixels = 1280 * 720

# Cache for _file_has_audio(): {(path, size, mtime): bool}.  Cleared wholesale
# when it grows past the cap -- this is a hot-path cache, not a working set, so
# an occasional re-probe is cheaper than tracking recency.
_gAudioProbeCache = {}
_kAudioProbeCacheMax = 512

# Never let an ffmpeg child pop its own console window.  ffmpeg is a console
# program, and a console child inherits its parent's console -- but ALLOCATES A
# NEW, VISIBLE ONE when the parent has none.  The front end has none: the
# installed build launches it detached under pythonw (build\launcher.py), so
# every clip opened in the Search window flashed a black window on screen.  The
# same applies to any back end started without a console.  CREATE_NO_WINDOW is
# the same idiom used in backEnd\responses\CommandResponse.py.
_kNoWindow = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def _get_ffmpeg():
    """Return path to ffmpeg binary, preferring imageio-ffmpeg's bundled copy.

    See ClipUtils._get_ffmpeg: an installed build uses a Sighthound-named copy
    of the same binary.
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


def _file_has_audio(path):
    """Return True if the media file contains at least one audio stream.

    An MP4 says so in its header, so read it there: hasAudioTrack() looks for a
    `soun` handler in moov and costs ~3ms.  The ffmpeg fallback below is a full
    process spawn (measured 0.09-1.1s against the archive) for one boolean, and
    it was being paid on nearly every clip the Search window opened -- each clip
    is normally a different file, so the cache rarely helped.

    The answer is still cached process-wide because the callers ask repeatedly
    for the same file: ClipReader.open() clears the per-instance answer on every
    open, and DataManager._openMarkedFile builds a brand-new reader on every file
    switch -- so a single clip selection paid this one to three times over.
    Keyed on identity + size + mtime so a rewritten segment (the recorder
    re-stamps segments in place) re-probes.
    """
    try:
        st = os.stat(path)
        key = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime)
    except OSError:
        key = None

    if key is not None:
        cached = _gAudioProbeCache.get(key)
        if cached is not None:
            return cached

    # Straight from the header when we can read it; None means "not an MP4 I can
    # parse", and only then is the ffmpeg spawn worth it.
    if hasAudioTrack is not None:
        fromHeader = hasAudioTrack(path)
        if fromHeader is not None:
            if key is not None:
                if len(_gAudioProbeCache) >= _kAudioProbeCacheMax:
                    _gAudioProbeCache.clear()
                _gAudioProbeCache[key] = fromHeader
            return fromHeader

    try:
        r = subprocess.run([_get_ffmpeg(), '-i', path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=15, creationflags=_kNoWindow)
        hasAudio = b'Audio:' in r.stderr
    except Exception:
        # Don't cache a failure: a timeout under load would otherwise stick as
        # "no audio" for the rest of the session.
        return False

    if key is not None:
        if len(_gAudioProbeCache) >= _kAudioProbeCacheMax:
            _gAudioProbeCache.clear()
        _gAudioProbeCache[key] = hasAudio
    return hasAudio


def frameIndexAt(relativeMs, fps, totalFrames):
    """Which frame ClipReader.seek(relativeMs) lands on.

    Split out of seek() so that callers who need to know where a seek WOULD
    land -- DataManager snaps a clip's bounds onto real frame times, and used to
    decode a frame at each bound to find out -- cannot drift from what seek()
    actually does.

    The +1 absorbs the truncation in the ms values ClipReader itself reports: a
    frame's ms is floor(idx*1000/fps), which can sit up to 1ms BEFORE the frame
    really starts and so falls inside the PREVIOUS frame's interval.  Without
    it, seeking to a frame's own reported ms lands one frame earlier -- so
    reloadCurrentFrame() walked the video backwards a frame on every window
    resize, and _loadClip's seek to the play position it had just been handed
    missed the GPU frame cache and respawned ffmpeg.  A frame is ~60ms wide on
    these cameras, so this only changes which frame an arbitrary seek picks
    within the last millisecond of one.

    @param  relativeMs   Target ms from the clip's first frame (not epoch).
    @param  fps          Frames per second, as read at open.
    @param  totalFrames  Frame count, or 0 when the container doesn't say.
    @return idx          Zero-based frame index.
    """
    relativeMs = max(0, relativeMs)
    idx = max(0, int((relativeMs + 1) * fps / 1000.0))
    # Only clamp when totalFrames is known -- CAP_PROP_FRAME_COUNT returns 0 for
    # some mp4v containers, which would incorrectly force every seek to frame 0.
    if totalFrames > 0:
        idx = min(idx, totalFrames - 1)
    return idx


def frameMsAt(relativeMs, fps, totalFrames):
    """The ms ClipReader would report for the frame seek(relativeMs) lands on.

    Same value a decoded frame's .ms carries, so a caller can snap a time onto
    a real frame without opening a decoder.
    """
    return int(frameIndexAt(relativeMs, fps, totalFrames) * 1000.0 / fps)


class _AudioPlayer:
    """Streams a clip's audio to the speakers, synced loosely to video playback.

    The video player drives timing; this plays the clip's audio in real time
    from a requested offset.  It is started on unmute / play and stopped on
    mute / pause / seek, so it stays aligned with 1x playback without a shared
    clock (good enough for review; non-1x speeds are simply silent).
    """

    def __init__(self, clipPath, logFn=None):
        self._clipPath = clipPath
        self._logFn    = logFn
        self._proc     = None
        self._stream   = None
        self._thread   = None
        self._stop     = threading.Event()
        self._lock     = threading.Lock()

    def play_from(self, startSec):
        """(Re)start audio playback from startSec into the clip."""
        if not _sd_available:
            return
        self.stop()
        self._stop.clear()

        cmd = [_get_ffmpeg(), '-loglevel', 'quiet']
        if startSec and startSec > 0:
            cmd += ['-ss', '%.3f' % startSec]
        cmd += ['-i', self._clipPath, '-vn',
                '-f', 's16le', '-acodec', 'pcm_s16le',
                '-ac', str(_kAudioChannels), '-ar', str(_kAudioRate), 'pipe:1']
        t0 = time.monotonic()   # video clock reference for offset calibration
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=_kNoWindow)
        except Exception:
            return
        try:
            stream = _sd.RawOutputStream(samplerate=_kAudioRate,
                                         channels=_kAudioChannels, dtype='int16')
            stream.start()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return

        with self._lock:
            self._proc   = proc
            self._stream = stream
            self._thread = threading.Thread(target=self._feed,
                                            args=(proc, stream, t0), daemon=True)
            self._thread.start()

    def _feed(self, proc, stream, t0):
        frame_bytes = _kAudioChannels * 2   # int16 stereo => 4 bytes/frame
        try:
            buf = proc.stdout.read(_kAudioBlockBytes)
            if not buf:
                return

            # Remove the constant A/V offset: the video clock has been running
            # since t0 while the decoder spawned and the device buffered, so the
            # audio we'd play now is already "late" by that elapsed time plus the
            # output latency.  Drop exactly that many leading samples so the first
            # audible sample lines up with the current video frame.
            try:
                dev_latency = float(stream.latency)
            except Exception:
                dev_latency = 0.0
            skip_bytes = int((time.monotonic() - t0 + dev_latency)
                             * _kAudioRate) * frame_bytes
            while skip_bytes > 0 and buf and not self._stop.is_set():
                if len(buf) <= skip_bytes:
                    skip_bytes -= len(buf)
                    buf = proc.stdout.read(_kAudioBlockBytes)
                else:
                    buf = buf[skip_bytes:]
                    skip_bytes = 0

            # Play the remainder, self-paced by the blocking write().
            while not self._stop.is_set() and buf:
                try:
                    stream.write(buf)
                except Exception:
                    break
                buf = proc.stdout.read(_kAudioBlockBytes)
        finally:
            try:
                if self._stop.is_set():
                    # Asked to stop early (clip switch, leaving the view).
                    # abort() throws away whatever the device has already
                    # buffered; stop() plays it out first, which is how a clip
                    # kept talking for a moment after the user moved on -- and
                    # over a live camera or the next clip, that sounds like it
                    # is coming from what's on screen now.
                    stream.abort()
                else:
                    # Reached the end of the clip: let the last block finish.
                    stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            try:
                # Every clip switch spawns one of these; without this the read
                # pipe leaks a handle each time for the life of the front end.
                proc.stdout.close()
            except Exception:
                pass

    def stop(self):
        """Stop playback and release the device/process.

        The feeder thread is the *sole* owner of the sounddevice stream and
        closes it in its finally block — closing a PortAudio stream from two
        threads corrupts the heap.  So here we only signal the feeder and kill
        the ffmpeg process (which unblocks the feeder's read()), then join it.
        """
        self._stop.set()
        with self._lock:
            proc, self._proc   = self._proc, None
            self._stream       = None   # owned/closed by the feeder thread
            thread, self._thread = self._thread, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def close(self):
        self.stop()


class LiveAudioPlayer:
    """Plays a camera's live audio from the back-end's shared-memory ring buffer.

    The back end captures each camera's audio once (one RTSP session) and writes
    it to a per-camera ring buffer (see videoLib2.python.AudioRelay).  This reads
    that ring and plays it via sounddevice — so live audio needs NO camera
    connection of its own (important: many cameras cap at 2 RTSP sessions, which
    the backend video + recording-audio already use).  The feeder thread owns the
    sounddevice stream and is self-paced by the blocking write().
    """

    def __init__(self, logFn=None):
        self._logFn  = logFn
        self._path   = None      # ring buffer file currently playing
        self._thread = None
        self._stop   = threading.Event()
        self._lock   = threading.Lock()

    def play(self, audio_path):
        """Start playing the camera's ring buffer (no-op if already playing it)."""
        with self._lock:
            if (audio_path and self._path == audio_path
                    and self._thread is not None and self._thread.is_alive()):
                return
        self.stop()
        if not _sd_available or not audio_path:
            return
        self._stop.clear()
        with self._lock:
            self._path   = audio_path
            self._thread = threading.Thread(target=self._feed,
                                            args=(audio_path,), daemon=True)
            self._thread.start()

    def _log(self, msg):
        if self._logFn:
            try:
                self._logFn("live-audio: " + msg)
            except Exception:
                pass

    def _feed(self, path):
        from videoLib2.python.AudioRelay import AudioRingReader
        reader = AudioRingReader(path)

        # Wait briefly for the back end's ring buffer to appear / become valid.
        info = None
        for _ in range(50):           # up to ~5s
            if self._stop.is_set():
                reader.close()
                return
            info = reader.open()
            if info:
                break
            reader.close()
            time.sleep(0.1)
        if not info:
            self._log("ring buffer not available: %s" % path)
            return

        rate, channels = info
        stream = None
        try:
            stream = _sd.RawOutputStream(samplerate=rate, channels=channels,
                                         dtype='int16')
            stream.start()
            self._log("playing ring buffer (%dHz %dch): %s" % (rate, channels, path))
            while not self._stop.is_set():
                data = reader.read()
                if data:
                    try:
                        stream.write(data)
                    except Exception as e:
                        self._log("stream.write error: %r" % e)
                        break
                else:
                    time.sleep(0.01)
        except Exception as e:
            self._log("sounddevice error: %r" % e)
        finally:
            try:
                if stream is not None:
                    stream.stop(); stream.close()
            except Exception:
                pass
            reader.close()

    def stop(self):
        """Stop playback (the feeder thread owns and closes the stream)."""
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
            self._path = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def close(self):
        self.stop()


class _ClipFrame:
    """A single decoded frame returned by ClipReader.

    Attributes expected by the display pipeline:
      .ms      — ms offset from the start of the clip file (int)
      .width   — frame width in pixels
      .height  — frame height in pixels
      .buffer  — ctypes pointer (plain int) to contiguous RGB pixel data;
                 same convention as StreamReader._Frame so the GL canvas
                 can call glTexImage2D directly.

    Methods:
      .asPil()      — PIL.Image.Image (RGB)
      .asWxBuffer() — PIL.Image.Image (used by bitmap-mode updateBitmap)
    """

    def __init__(self, rgb_np, ms_relative):
        # Keep the numpy array alive so the ctypes pointer stays valid.
        self._data = np.ascontiguousarray(rgb_np)
        self.ms = ms_relative
        self.width = self._data.shape[1]
        self.height = self._data.shape[0]
        self.buffer = self._data.ctypes.data   # plain int — matches StreamReader

    def asPil(self):
        return Image.fromarray(self._data, 'RGB')

    def asWxBuffer(self):
        return self.asPil()

    def updateFromPil(self, pilImage):
        """Write a (marked-up) PIL image back into this frame's pixel buffer
        in place.

        Used by DataManager to draw display-only overlays (bounding boxes,
        region zones) onto a decoded frame.  The write MUST stay in place
        (self._data[:] = ...) so self.buffer keeps pointing at valid memory —
        do NOT reassign self._data.
        """
        arr = np.asarray(pilImage.convert('RGB'))
        if arr.shape == self._data.shape:
            self._data[:] = arr

    def asNumpy(self):
        """Return the live RGB ndarray backing this frame (H x W x 3, uint8).

        Draw on it IN PLACE (e.g. cv2.rectangle(arr, ...)) so self.buffer keeps
        pointing at valid memory — do NOT reassign or replace it.  Lets the
        display pipeline draw overlays directly, avoiding a PIL round-trip
        (asPil + updateFromPil = two full-frame conversions per frame).
        """
        return self._data

    def __bool__(self):
        return self._data is not None


class _GpuPipeDecoder:
    """Sequential clip decoder using ffmpeg + NVDEC (cuvid).

    Decodes on the GPU and — crucially — downscales to the playback size ON THE
    GPU (cuvid -resize) before the frame is copied back to system memory, so
    only the small display-size frame crosses the pipe.  For 4K HEVC this is
    ~10x faster than CPU decode (whose 33 ms/frame the playback budget can't
    afford); full-res GPU decode would be no faster because the 25 MB/frame
    download dominates.  Seeking is frame-accurate via -ss before -i (verified
    on ffmpeg 7.1).  Video only; audio stays on the separate _AudioPlayer.

    Spawning is LAZY: nothing starts until a frame is actually asked for, and
    then it starts at that frame.  Eagerly proving the decoder at frame 0 cost a
    whole spawn that the very next seek threw away.  Any spawn/decode failure is
    surfaced so ClipReader can fall back to cv2.
    """

    def __init__(self, path, decoder, outW, outH, fps, logFn=None):
        self._path       = path
        self._decoder    = decoder
        self._w          = int(outW)
        self._h          = int(outH)
        self._fps        = fps if (fps and fps > 0) else 25.0
        self._logFn      = logFn
        self._frameBytes = self._w * self._h * 3
        self._proc       = None
        self._frameIdx   = 0       # index of the NEXT frame readFrame() returns
        self._lastRaw    = None    # (rawBytes, idx) of the frame last returned

    def _log(self, msg):
        if self._logFn:
            try:
                self._logFn(msg)
            except Exception:
                pass

    def _spawn(self, startFrame):
        self._closeProc()
        cmd = [_get_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin']
        if startFrame > 0:
            # -ss before -i is fast AND frame-accurate on ffmpeg 7.1 (verified).
            cmd += ['-ss', '%.4f' % (startFrame / self._fps)]
        cmd += ['-c:v', self._decoder,
                '-resize', '%dx%d' % (self._w, self._h),
                '-i', self._path,
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=_kNoWindow)
        self._frameIdx = startFrame

    def _readRaw(self):
        if self._proc is None:
            return None
        need = self._frameBytes
        chunks = []
        got = 0
        rd = self._proc.stdout.read
        while got < need:
            chunk = rd(need - got)
            if not chunk:
                return None            # EOF or short read
            chunks.append(chunk)
            got += len(chunk)
        return b''.join(chunks)

    def _decode(self, raw):
        """Wrap one raw RGB frame as a writable ndarray, safe to draw on."""
        return np.frombuffer(raw, np.uint8).reshape(self._h, self._w, 3).copy()

    def readFrame(self):
        """@return (rgb_ndarray, frameIdx) for the current frame, or None at EOF.
        The ndarray is a writable copy, safe for in-place overlay drawing."""
        if self._proc is None:
            # Nothing spawned yet -- this reader is being played from the top.
            try:
                self._spawn(0)
            except Exception as e:
                self._log('gpu decode: spawn failed: %r' % e)
                return None
        raw = self._readRaw()
        if raw is None:
            return None
        idx = self._frameIdx
        self._frameIdx += 1
        # Kept as the raw bytes, not the ndarray: callers draw overlays in place
        # on what we hand back (see _ClipFrame.asNumpy), so caching the ndarray
        # would serve a marked-up frame the second time round.  bytes are
        # immutable, and re-wrapping them is what readFrame does anyway.
        self._lastRaw = (raw, idx)
        return self._decode(raw), idx

    def seek(self, frameIdx):
        """Frame-accurate seek; @return (ndarray, frameIdx) or None."""
        frameIdx = max(0, int(frameIdx))

        # Re-asking for the frame we just returned must not cost anything.  It
        # is a routine request -- _loadClip seeks to the play position it has
        # just been handed, and reloadCurrentFrame() re-fetches the paused frame
        # on every window resize -- and it lands on `ahead == -1`, which fails
        # the forward-hop test below and used to respawn ffmpeg outright.
        if self._lastRaw is not None and self._lastRaw[1] == frameIdx:
            return self._decode(self._lastRaw[0]), frameIdx

        # A respawn costs a process kill + CreateProcess + NVDEC init (measured
        # 0.45-2.4s against the archive), so for a short FORWARD hop it is much
        # cheaper to decode ahead and throw the frames away.  This is the common
        # case: frame-stepping and scrubbing move by a frame at a time.
        ahead = frameIdx - self._frameIdx
        if self._proc is not None and 0 <= ahead <= self._maxSeekAhead():
            if self._skipFrames(ahead):
                got = self.readFrame()
                if got is not None:
                    return got
            # Short read (EOF, or the pipe died): fall through and respawn,
            # which is exactly what this method used to do unconditionally.

        try:
            self._spawn(frameIdx)
        except Exception as e:
            self._log('gpu decode: seek spawn failed: %r' % e)
            return None
        return self.readFrame()

    def _maxSeekAhead(self):
        """How many frames it is worth decoding rather than respawning ffmpeg."""
        return max(1, int(2 * self._fps))

    def _skipFrames(self, count):
        """Discard `count` frames from the pipe, cheaply.

        Deliberately not readFrame(): the discarded frames never reach a caller,
        so there is no reason to pay for the numpy wrap and copy.
        @return  True if all `count` frames were consumed, False on short read.
        """
        for _ in range(count):
            if self._readRaw() is None:
                return False
            self._frameIdx += 1
        return True

    def _closeProc(self):
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

    def close(self):
        self._lastRaw = None
        self._closeProc()


class ClipReader:
    """OpenCV-backed video reader compatible with Sighthound Video's ClipReader API."""

    def __init__(self, logFn=None):
        self._cap = None
        self._outW = 0
        self._outH = 0
        self._fps = 25.0
        self._totalFrames = 0
        self._frameIdx = 0    # index of the NEXT frame read() will return
        self._logFn = logFn
        self._gpu = None      # _GpuPipeDecoder when NVDEC decode is active
        self._gpuProved = False  # True once the GPU path has produced a frame
        self._srcW = 0        # native source dimensions (cached at open)
        self._srcH = 0
        self._decodeInfo = ''  # decode-path description, logged by the caller

        # Audio playback state
        self._clipPath = ''
        self._enableAudio = False
        self._audioMuted = True
        self._hasAudio = None      # lazily probed, cached
        self._audio = None         # _AudioPlayer when audio is enabled

    # ------------------------------------------------------------------
    def decodeInfo(self):
        """Human-readable decode path chosen at open, for the caller to log.
        E.g. 'NVDEC hevc_cuvid 3840x2160->1280x720'.

        logFn is a plain one-argument Python logging callable (DataManager
        passes self._logger.info).  It used to be getCLogFn()'s ctypes
        callback, which takes (int level, bytes) -- so every self._logFn(msg)
        in this module raised, was swallowed, and logged nothing at all."""
        return self._decodeInfo

    # ------------------------------------------------------------------
    def open(self, fullPath, outW, outH, firstMs, extras=None):
        """Open a clip file for playback.

        @param  fullPath  Absolute path to the MP4 file.
        @param  outW      Output width (0 = native/aspect-ratio preserved).
        @param  outH      Output height (0 = native/aspect-ratio preserved).
        @param  firstMs   Absolute epoch-ms of first frame (caller-managed).
        @param  extras    Rendering hint dict (ignored in this implementation).
        @return           True on success, False on error.
        """
        if not _cv2_available:
            return False

        cap = cv2.VideoCapture(fullPath, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            return False

        self._cap = cap
        self._outW = max(0, int(outW))
        self._outH = max(0, int(outH))
        self._fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self._totalFrames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._srcW = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._srcH = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._gpuFourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        self._frameIdx = 0
        self._gpu = None
        self._gpuProved = False
        # A one-shot full-resolution still (zoomed pause frame) must NOT go
        # through the GPU path: that caps output to the real-time playback pixel
        # budget, which is exactly the detail we're trying to recover.  Decoding
        # a single frame on the CPU costs ~33ms at 4K -- fine for a still.
        self._nativeStill = bool(extras and extras.get('nativeStill'))
        # Set by callers that want exactly one frame out of this reader (thumbnails,
        # stills, a size probe) rather than to play the clip.  Keeps the NVDEC
        # spawn for playback only -- see _maybeStartGpu.
        self._singleFrame = bool(extras and extras.get('singleFrame'))

        # Set up audio playback if requested and the clip has an audio track.
        self._clipPath = fullPath
        self._hasAudio = None
        self._enableAudio = bool(extras and extras.get('enableAudio'))
        self._audioMuted = bool(extras and extras.get('audioMute', True))
        if self._audio is not None:
            self._audio.close()
            self._audio = None
        if self._enableAudio and self.hasAudio():
            self._audio = _AudioPlayer(fullPath, self._logFn)

        # Offload decode to the GPU (NVDEC) for clips whose CPU decode can't
        # keep up at playback rate (notably 4K HEVC).  Best-effort: any failure
        # leaves self._gpu None and the cv2 path below is used unchanged.  The
        # cv2 cap stays open for metadata and as the fallback decoder.
        self._maybeStartGpu(fullPath)
        return True

    # ------------------------------------------------------------------
    def _gpuOutSize(self):
        """GPU decode/download size: the display size, but never upscaled past
        the source and rounded to even dims (cuvid requirement)."""
        sw, sh = self._srcW, self._srcH
        ow, oh = self._outW, self._outH
        if ow <= 0 and oh <= 0:
            ow, oh = sw, sh
        elif ow <= 0:
            ow = int(round(sw * oh / float(sh))) if sh else sw
        elif oh <= 0:
            oh = int(round(sh * ow / float(sw))) if sw else sh
        if sw and sh and ow > sw:      # never upscale — no benefit, more transfer
            ow, oh = sw, sh
        # Cap to the real-time pixel budget (large windows just upscale this).
        if ow > 0 and oh > 0 and ow * oh > _kGpuMaxPlaybackPixels:
            s = math.sqrt(_kGpuMaxPlaybackPixels / float(ow * oh))
            ow = int(ow * s)
            oh = int(oh * s)
        ow -= ow % 2
        oh -= oh % 2
        return max(2, ow), max(2, oh)

    # ------------------------------------------------------------------
    def _maybeStartGpu(self, path):
        """Try to start NVDEC decode for this clip; leave self._gpu None to use
        cv2.  Gated to codecs/resolutions where CPU decode is the bottleneck.
        Logs its decision on EVERY path so the live decode route is visible."""
        try:
            cc = bytes([(self._gpuFourcc >> (8 * i)) & 0xFF
                        for i in range(4)]).decode('ascii', 'ignore').lower().strip()
        except Exception:
            cc = ''
        decoder = _kCuvidByCodec.get(cc)
        outW, outH = self._gpuOutSize()

        reason = None
        if self._nativeStill:
            reason = 'native still requested (full-res CPU decode)'
        elif decoder is None:
            reason = 'codec %r not NVDEC-supported' % cc
        elif not (decoder == 'hevc_cuvid' or self._srcW >= 2560):
            reason = 'small %s (CPU is fine)' % cc
        elif self._singleFrame:
            # NVDEC pays for itself by amortizing one process spawn over a whole
            # playback session.  A caller that opens the clip, grabs one frame
            # and throws the reader away can never amortize it: the spawn alone
            # measured 0.37-0.99s against the archive, against ~33ms to decode a
            # single 4K frame on the CPU.  The search results list opens a
            # reader per preview row, so this was a process per thumbnail.
            reason = 'single frame requested (spawn would not amortize)'
        elif self._srcW and outW * outH > 0.75 * self._srcW * self._srcH:
            # Downscale too small to matter: the per-frame download would
            # dominate and the GPU is no faster.  (Loosened 0.6 -> 0.75.)
            reason = 'output %dx%d ~= source %dx%d (transfer-bound)' % (
                outW, outH, self._srcW, self._srcH)
        if reason is not None:
            self._decodeInfo = 'cv2 %dx%d (codec=%s: %s)' % (
                self._srcW, self._srcH, cc, reason)
            return

        # Deliberately NOT spawned here.  This used to decode frame 0 purely to
        # prove NVDEC works, and the caller then seeked somewhere else -- so the
        # proof cost a whole spawn (0.45-2.4s against this archive) to produce a
        # frame nobody wanted.  The decoder now spawns at the first frame that is
        # actually asked for, and whether NVDEC really works here is settled by
        # that frame instead; see _gpuFallback.
        #
        # Best-effort and NOT globally sticky — a single transient failure must
        # not disable GPU decode for the rest of the session.
        self._gpu = _GpuPipeDecoder(path, decoder, outW, outH, self._fps,
                                    self._logFn)
        self._decodeInfo = 'NVDEC %s %dx%d->%dx%d' % (
            decoder, self._srcW, self._srcH, outW, outH)

    # ------------------------------------------------------------------
    def _gpuFallback(self, reason):
        """Give up on NVDEC for this reader and hand decoding back to cv2.

        Only ever reached before the GPU has produced a frame: once it has, a
        None is ordinary end-of-file rather than a decoder that cannot run.
        self._cap was never closed, so cv2 can take over mid-call.

        @param  reason  Short description for the log / decodeInfo.
        @return usable  True if cv2 can take over, False if there is nothing
                        left to decode with.
        """
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        self._decodeInfo = 'cv2 %dx%d (NVDEC failed: %s)' % (
            self._srcW, self._srcH, reason)
        if self._logFn:
            try:
                self._logFn('Playback decode fell back to ' + self._decodeInfo)
            except Exception:
                pass
        return self._cap is not None

    # ------------------------------------------------------------------
    def hasAudio(self):
        """Does the clip contain an audio track? (probed once, then cached)"""
        if self._hasAudio is None:
            self._hasAudio = bool(self._clipPath) and _file_has_audio(self._clipPath)
        return self._hasAudio

    # ------------------------------------------------------------------
    def setMute(self, muted):
        """Mute (stop) or note unmute intent for audio playback.

        Unmuting starts from the current position via playAudioFrom(); muting
        stops playback immediately.
        """
        self._audioMuted = bool(muted)
        if muted and self._audio is not None:
            self._audio.stop()

    # ------------------------------------------------------------------
    def playAudioFrom(self, relativeMs):
        """Start audio playback from relativeMs (ms from the clip's first frame)."""
        if self._audio is not None and self._enableAudio and not self._audioMuted:
            self._audio.play_from(max(0.0, relativeMs / 1000.0))

    # ------------------------------------------------------------------
    def pauseAudio(self):
        """Stop audio playback (e.g. on pause or seek)."""
        if self._audio is not None:
            self._audio.stop()

    # ------------------------------------------------------------------
    def setOutputSize(self, resolution):
        """Update output dimensions; called by DataManager.updateVideoSize()."""
        self._outW = max(0, int(resolution[0]))
        self._outH = max(0, int(resolution[1]))

    # ------------------------------------------------------------------
    def _gpuFrame(self, result):
        """Wrap a _GpuPipeDecoder (ndarray, idx) result as a _ClipFrame and sync
        self._frameIdx to the NEXT frame.  None (EOF/failure) -> None."""
        if result is None:
            return None
        arr, idx = result
        self._gpuProved = True
        self._frameIdx = idx + 1
        return _ClipFrame(arr, int(idx * 1000.0 / self._fps))

    def seek(self, relativeMs):
        """Seek to ms offset from the file start.

        @param  relativeMs  Target ms from the clip's first frame (not epoch).
        @return             A _ClipFrame, or None if seek/read failed.
        """
        target_frame = frameIndexAt(relativeMs, self._fps, self._totalFrames)

        if self._gpu is not None:
            frame = self._gpuFrame(self._gpu.seek(target_frame))
            if frame is not None or self._gpuProved:
                return frame
            # The GPU never produced its first frame: NVDEC isn't usable for
            # this clip after all, so retry the same request through cv2.
            if not self._gpuFallback('no frame at seek'):
                return None

        if self._cap is None:
            return None
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        # Read back the actual position — OpenCV may clamp to EOF for out-of-range seeks,
        # so _frameIdx must reflect where the cap actually landed, not what we asked for.
        self._frameIdx = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES))
        return self._readFrame()

    # ------------------------------------------------------------------
    def getNextFrame(self):
        """Return the next sequential frame, or None at end-of-file."""
        if self._gpu is not None:
            frame = self._gpuFrame(self._gpu.readFrame())
            if frame is not None or self._gpuProved:
                return frame
            if not self._gpuFallback('no frame at read'):
                return None
        if self._cap is None:
            return None
        return self._readFrame()

    # ------------------------------------------------------------------
    def getPrevFrame(self):
        """Step one frame back and return it, or None at start-of-file."""
        # frameIdx is the index of the NEXT unread frame; to go back one from
        # the last-returned frame, seek to frameIdx-2.
        target = max(0, self._frameIdx - 2)
        if self._gpu is not None:
            frame = self._gpuFrame(self._gpu.seek(target))
            if frame is not None or self._gpuProved:
                return frame
            if not self._gpuFallback('no frame at step back'):
                return None
        if self._cap is None:
            return None
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        self._frameIdx = target
        return self._readFrame()

    # ------------------------------------------------------------------
    def getNextFrameOffset(self):
        """Return ms offset of the next frame (relative to file start), or -1."""
        if self._gpu is None and self._cap is None:
            return -1
        # Don't gate on _totalFrames: CAP_PROP_FRAME_COUNT may be 0 for mp4v.
        # True EOF is signaled by a failed frame read in getNextFrame() instead.
        return int(self._frameIdx * 1000.0 / self._fps)

    # ------------------------------------------------------------------
    def getFrameTiming(self):
        """@return (fps, totalFrames) as read at open; feeds frameMsAt()."""
        return self._fps, self._totalFrames

    # ------------------------------------------------------------------
    def getInputSize(self):
        """Return native (width, height) of the video."""
        if self._srcW and self._srcH:
            return (self._srcW, self._srcH)
        if self._cap is None:
            return (0, 0)
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return (w, h)

    # ------------------------------------------------------------------
    def _readFrame(self):
        """Read one frame, apply output resize, return a _ClipFrame."""
        # Compute ms BEFORE reading: CAP_PROP_POS_MSEC after cap.read() returns
        # the position of the NEXT frame (or 0.0 for some mp4v containers), so
        # we derive the current frame's ms from _frameIdx instead.
        frame_ms = int(self._frameIdx * 1000.0 / self._fps)

        ret, bgr = self._cap.read()
        if not ret or bgr is None:
            return None

        self._frameIdx += 1   # advance to next unread frame

        bgr = self._resize(bgr)
        rgb = bgr[:, :, ::-1]
        return _ClipFrame(rgb, frame_ms)

    # ------------------------------------------------------------------
    def _resize(self, bgr):
        """Resize frame to output dimensions, preserving aspect ratio as needed."""
        if self._outW <= 0 and self._outH <= 0:
            return bgr
        h, w = bgr.shape[:2]
        if self._outW > 0 and self._outH > 0:
            return cv2.resize(bgr, (self._outW, self._outH))
        if self._outW <= 0:
            scale = self._outH / float(h)
            return cv2.resize(bgr, (max(1, int(w * scale)), self._outH))
        scale = self._outW / float(w)
        return cv2.resize(bgr, (self._outW, max(1, int(h * scale))))

    # ------------------------------------------------------------------
    def close(self):
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        if self._audio is not None:
            self._audio.close()
            self._audio = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# Module-level helpers (match the original Py2 ClipReader module API)
# ---------------------------------------------------------------------------

_gWarnedSyntheticFallback = False

def _logSyntheticFallbackOnce(clipPath):
    """Note, once per process, that we could not read a file's real frame times.

    One unreadable clip is unremarkable -- truncated mid-write, or a format the
    recorder didn't produce.  A fleet that falls back on EVERYTHING is a
    different thing, and would otherwise be invisible: the estimated ladder
    returns plausible-looking numbers either way.

    Deliberately NOT routed through the caller's `logFn`.  That is a ctypes
    CFUNCTYPE(c_int, c_char_p) built for native callers, so it can only be
    handed bytes, and the app's logger renders those as a repr -- b'...' with
    doubled backslashes.  Nothing in any of these logs looks like that today
    and this line is not worth being the first.  A module logger keeps the
    message readable for anyone who attaches a handler or runs the tools;
    DiskCleaner still warns on its own when the list comes back empty.
    """
    global _gWarnedSyntheticFallback
    if _gWarnedSyntheticFallback:
        return
    _gWarnedSyntheticFallback = True
    logging.getLogger(__name__).debug(
        "No MP4 sample table in %s; using estimated frame times "
        "(further occurrences not logged)", clipPath)


def getMsList(clipPath, logFn=None):
    """Return a list of frame timestamps in ms, relative to the file start.

    Callers bisect this list to turn an absolute time into an offset that
    really exists in the file, so the SPACING of these values is what matters,
    not just their count.

    Read from the MP4 sample tables where possible, which is both exact and far
    cheaper than opening a decoder.  The fallback is the old FPS-based ladder,
    `int(i*1000/fps)` -- correct only if every frame is evenly spaced, which a
    camera that stalls mid-segment makes false by up to a second or more.
    """
    if videoTrackIndex is not None:
        index = videoTrackIndex(clipPath)
        if index is not None:
            return index[0]
        _logSyntheticFallbackOnce(clipPath)

    if not _cv2_available:
        return []
    cap = cv2.VideoCapture(clipPath, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return [int(i * 1000.0 / fps) for i in range(total)] if total > 0 else []


def getDuration(clipPath, logFn=None):
    """Return the total duration of the clip in milliseconds."""
    if not _cv2_available:
        return 0
    cap = cv2.VideoCapture(clipPath, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return int(total * 1000.0 / fps) if total > 0 else 0
