"""
TtsManager.py — Kokoro-ONNX TTS for Sighthound event actions.

Uses kokoro-onnx (Python 3.14-compatible) to generate audio locally,
optionally casting to a Chromecast speaker via pychromecast.

Model files (~112 MB total) are downloaded from GitHub Releases on first use
and stored in the Sighthound data directory.  Subsequent calls use the
local cache.

Provides a process-level singleton via get_tts_manager().

--- Why kokoro-onnx (thewh1teagle) instead of kokoro (hexgrad) ---

The original Kokoro project lives at hexgrad/Kokoro-82M on HuggingFace and
its PyPI package is `kokoro`.  It requires Python < 3.13 and cannot be
installed in this venv (Python 3.14).

`kokoro-onnx` (github.com/thewh1teagle/kokoro-onnx) is a community port that
converts the same model weights to ONNX format and runs them via ONNX Runtime.
It has no Python version ceiling and is compatible with Python 3.14 and
numpy 2.x.  Voice quality is identical — only the inference engine differs
(ONNX Runtime vs PyTorch).

The ONNX model files are NOT on HuggingFace; they are hosted exclusively on
the kokoro-onnx GitHub Releases page, which is why downloads go there rather
than to HuggingFace.  The file URLs are stored in _kModelUrl / _kVoicesUrl
below.
"""

import logging
import os
import queue
import socket
import threading
import uuid

try:
    from kokoro_onnx import Kokoro as _Kokoro
    _kKokoroAvail = True
except ImportError:
    _kKokoroAvail = False

try:
    import soundfile as _sf
    _kSfAvail = True
except ImportError:
    _kSfAvail = False

try:
    import pychromecast as _pychromecast
    _kCcAvail = True
except ImportError:
    _kCcAvail = False

try:
    import winsound as _winsound
    _kWinsoundAvail = True
except ImportError:
    _kWinsoundAvail = False

try:
    import pyaudio as _pyaudio
    import wave as _wave
    _kPyaudioAvail = True
except ImportError:
    _kPyaudioAvail = False

# ── Constants ─────────────────────────────────────────────────────────────────

try:
    from appCommon.InstallPaths import getUserDataDir as _getUserDataDir
    from appCommon.InstallPaths import getBundledModelDir as _getBundledModelDir
except Exception:
    _getUserDataDir = lambda: os.path.join(
        os.path.expanduser('~'), 'AppData', 'Local', 'Sighthound Video Py3')
    _getBundledModelDir = lambda name: None

_kSighthoundDataDir = _getUserDataDir()
_kModelDir  = os.path.join(_kSighthoundDataDir, 'TtsModels')
_kCacheDir  = os.path.join(_kSighthoundDataDir, 'TtsCache')


def _modelFile(name):
    """Locate one of the Kokoro model files.

    An installed build ships them (they are 337 MB and would otherwise be
    downloaded from GitHub on first use, on a machine that may have no internet
    and under an account whose home directory is not the user's).  A source
    checkout keeps the old download-into-the-data-dir behaviour.

    @param  name  File name, e.g. "kokoro-v1.0.onnx".
    @return       Absolute path -- bundled if present, else in the data dir.
    """
    bundled = _getBundledModelDir('tts')
    if bundled:
        candidate = os.path.join(bundled, name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(_kModelDir, name)


_kModelPath  = _modelFile('kokoro-v1.0.onnx')
_kVoicesPath = _modelFile('voices-v1.0.bin')

# GitHub Releases for kokoro-onnx model files
_kModelUrl  = ('https://github.com/thewh1teagle/kokoro-onnx/releases/'
               'download/model-files-v1.0/kokoro-v1.0.onnx')
_kVoicesUrl = ('https://github.com/thewh1teagle/kokoro-onnx/releases/'
               'download/model-files-v1.0/voices-v1.0.bin')

# HTTP port for serving cached WAVs to Chromecast
_kHttpPort = 18765

# Timeouts (seconds) for connecting/waiting on a Chromecast.  Without these,
# pychromecast blocks forever on a wrong/offline IP and stalls the TTS queue.
_kCastConnectTimeout = 8.0
_kCastWaitTimeout    = 8.0

# How long the mDNS/zeroconf discovery browser listens before returning.
_kDiscoverTimeout = 5.0

# Voice labels shown in the UI → Kokoro voice codes
kTtsVoiceMap = {
    'Heart':  'af_heart',
    'Nicole': 'af_nicole',
    'Dora':   'ef_dora',
    'Emma':   'bf_emma',
    'Onyx':   'am_onyx',
}
kTtsVoiceNames = list(kTtsVoiceMap.keys())
_kVoiceCodeToName = {v: k for k, v in kTtsVoiceMap.items()}

# Map voice prefix → language code for kokoro-onnx
_kLangMap = {'a': 'en-us', 'b': 'en-gb', 'e': 'es', 'j': 'ja', 'z': 'zh'}


def _voice_lang(voice_code):
    return _kLangMap.get(voice_code[0], 'en-us')


# ── Singleton access ──────────────────────────────────────────────────────────

_manager = None
_manager_lock = threading.Lock()


def get_tts_manager(logger=None):
    """Return the process-level TTSManager, creating it on first call."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = _TTSManager(logger=logger)
    return _manager


# ── Model download ────────────────────────────────────────────────────────────

def _download_file(url, dest_path, logger):
    """Download url to dest_path with a .tmp swap for atomicity."""
    import requests
    logger.info("[TTS] Downloading %s ..." % os.path.basename(dest_path))
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + '.tmp'
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    with open(tmp, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    os.replace(tmp, dest_path)
    logger.info("[TTS] Saved %s" % os.path.basename(dest_path))


def ensure_models(logger=None):
    """Download model files if not already cached.  Safe to call repeatedly."""
    if logger is None:
        logger = logging.getLogger(__name__)
    for url, path in [(_kModelUrl, _kModelPath), (_kVoicesUrl, _kVoicesPath)]:
        if not os.path.exists(path):
            _download_file(url, path, logger)


# ── Speaker discovery ──────────────────────────────────────────────────────────

def discover_speakers(timeout=_kDiscoverTimeout, logger=None):
    """Discover Google/Chromecast speakers on the LAN via mDNS/zeroconf.

    @param timeout  Seconds to listen for advertisements.
    @param logger   Optional logger.
    @return         A list of {'name', 'ip', 'model'} dicts (possibly empty),
                    de-duplicated by IP.  Never raises.

    Safe to call from any process (front end or back end); it creates a short-
    lived zeroconf browser and tears it down before returning.
    """
    import time as _time

    if logger is None:
        logger = logging.getLogger(__name__)
    if not _kCcAvail:
        logger.warning("[TTS] discover_speakers: pychromecast not available")
        return []

    try:
        import zeroconf as _zeroconf
        from pychromecast.discovery import CastBrowser, SimpleCastListener
    except Exception as e:
        logger.warning("[TTS] discover_speakers: import failed: %s" % e)
        return []

    zc = None
    browser = None
    try:
        zc = _zeroconf.Zeroconf()
        browser = CastBrowser(SimpleCastListener(), zc)
        browser.start_discovery()
        _time.sleep(max(0.5, timeout))

        seen = set()
        results = []
        for info in list(browser.devices.values()):
            host = getattr(info, 'host', '') or ''
            if not host or host in seen:
                continue
            seen.add(host)
            results.append({
                'name':  info.friendly_name or host,
                'ip':    host,
                'model': info.model_name or '',
            })
        logger.info("[TTS] discover_speakers found %d device(s)" % len(results))
        return results
    except Exception as e:
        logger.warning("[TTS] discover_speakers error: %s" % e)
        return []
    finally:
        # stop_discovery() closes the zeroconf instance internally; the extra
        # guarded close() covers the case where start/stop never ran.
        try:
            if browser is not None:
                browser.stop_discovery()
        except Exception:
            pass
        try:
            if zc is not None:
                zc.close()
        except Exception:
            pass


# ── Manager implementation ────────────────────────────────────────────────────

class _TTSManager(object):
    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(__name__)
        self._kokoro = None
        self._kokoro_lock = threading.Lock()
        self._queue = queue.Queue()

        # Learned name -> IP map, updated when a send-failure fallback finds a
        # speaker that has moved to a new address.
        self._ipCache = {}

        os.makedirs(_kCacheDir, exist_ok=True)

        if _kCcAvail:
            self._local_ip = self._get_local_ip()
            self._httpd = self._start_server()
        else:
            self._local_ip = '127.0.0.1'
            self._httpd = None

        worker = threading.Thread(target=self._run, daemon=True,
                                  name="TtsManager-worker")
        worker.start()

    # ── Setup helpers ────────────────────────────────────────────────────────

    def _get_local_ip(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return '127.0.0.1'

    def _start_server(self):
        import http.server, socketserver, urllib.parse

        cache_dir = _kCacheDir

        class _Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                name = os.path.basename(urllib.parse.unquote(path))
                return os.path.join(cache_dir, name)

            def log_message(self, fmt, *args):
                pass

        try:
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("", _kHttpPort), _Handler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True,
                                 name="TtsManager-http")
            t.start()
            self._logger.info("[TTS] HTTP server on port %d" % _kHttpPort)
            return httpd
        except Exception as e:
            self._logger.warning("[TTS] HTTP server failed: %s" % e)
            return None

    def _get_kokoro(self):
        """Return the loaded Kokoro instance (lazy, downloads models if needed)."""
        with self._kokoro_lock:
            if self._kokoro is None:
                if not _kKokoroAvail:
                    raise RuntimeError(
                        "kokoro-onnx is not installed.  "
                        "Run: pip install kokoro-onnx")
                ensure_models(self._logger)
                self._logger.info("[TTS] Loading Kokoro ONNX model...")
                self._kokoro = _Kokoro(_kModelPath, _kVoicesPath)
                self._logger.info("[TTS] Kokoro ONNX model ready")
        return self._kokoro

    # ── Audio generation ─────────────────────────────────────────────────────

    def _generate(self, text, voice, speed):
        """Generate (or retrieve cached) WAV.  Returns filepath or None."""
        if not _kSfAvail:
            raise RuntimeError("soundfile is not installed")

        safe = "".join(c for c in text if c.isalnum() or c == ' ').strip()
        safe = safe.replace(' ', '_').lower()[:60]
        filename = "%s-%.1f-%s.wav" % (voice, speed, safe)
        filepath = os.path.join(_kCacheDir, filename)

        if os.path.exists(filepath):
            return filepath

        kokoro = self._get_kokoro()
        lang = _voice_lang(voice)
        samples, sample_rate = kokoro.create(text, voice=voice,
                                              speed=speed, lang=lang)
        _sf.write(filepath, samples, sample_rate)
        self._logger.info("[TTS] Generated %s" % filename)
        return filepath

    # ── Playback ─────────────────────────────────────────────────────────────

    def _play_local(self, filepath):
        if _kWinsoundAvail:
            _winsound.PlaySound(filepath, _winsound.SND_FILENAME)
        elif _kPyaudioAvail:
            inst = _pyaudio.PyAudio()
            wf = _wave.open(filepath, 'rb')
            ch, w, fr, _, _, _ = wf.getparams()
            stream = inst.open(rate=fr, channels=ch,
                               format=inst.get_format_from_width(w),
                               output=True)
            data = wf.readframes(4096)
            while data:
                stream.write(data)
                data = wf.readframes(4096)
            stream.close()
            inst.terminate()
        else:
            raise RuntimeError("No audio playback available (winsound/pyaudio)")

    def _cast_to_ip(self, ip, audio_url, duration):
        """Connect to the Chromecast at ip and play audio_url.

        Bounded by connect/wait timeouts so a wrong or offline address raises
        promptly instead of blocking the worker thread forever.  Always
        disconnects the socket before returning.  Raises on any failure.
        """
        import time
        host = (ip, 8009, uuid.uuid4(), None, None)
        cast = _pychromecast.get_chromecast_from_host(
            host, tries=1, retry_wait=0.5, timeout=_kCastConnectTimeout)
        try:
            cast.wait(timeout=_kCastWaitTimeout)
            orig_vol = (cast.status.volume_level
                        if cast.status and cast.status.volume_level is not None
                        else 0.5)
            cast.set_volume(1.0)
            mc = cast.media_controller
            mc.play_media(audio_url, 'audio/wav')
            mc.block_until_active(timeout=_kCastWaitTimeout)
            time.sleep(duration + 1.0)
            cast.set_volume(orig_vol)
        finally:
            try:
                cast.disconnect(timeout=5.0)
            except Exception:
                pass

    def _cast(self, filepath, cc_ip, cc_name=''):
        """Cast a synthesized WAV to a speaker, with a rediscovery fallback.

        Normally the saved (static) IP works on the first try.  If it doesn't —
        e.g. the speaker got a new DHCP address — we try any IP we cached from a
        prior fallback, then re-discover the speaker on the network by its saved
        friendly name and retry there.  A manually entered IP with no saved name
        still gets a best-effort fallback when exactly one speaker is present.
        """
        if not _kCcAvail:
            raise RuntimeError("pychromecast not installed")
        if self._httpd is None:
            raise RuntimeError("HTTP server not running")

        duration = _sf.info(filepath).duration
        filename = os.path.basename(filepath)
        audio_url = "http://%s:%d/%s" % (self._local_ip, _kHttpPort, filename)

        # Build the ordered candidate list: saved static IP, then a cached IP we
        # previously learned for this speaker name.
        candidates = []
        if cc_ip and cc_ip.strip():
            candidates.append(cc_ip.strip())
        cached = self._ipCache.get(cc_name) if cc_name else None
        if cached and cached not in candidates:
            candidates.append(cached)

        tried = []
        lastErr = None
        for ip in candidates:
            try:
                self._cast_to_ip(ip, audio_url, duration)
                self._logger.info("[TTS] Cast complete to %s (%s)"
                                  % (ip, cc_name or '?'))
                return
            except Exception as e:
                lastErr = e
                tried.append(ip)
                self._logger.warning("[TTS] Cast to %s failed: %s" % (ip, e))

        # Fallback: the speaker may have moved.  Re-discover and match by name.
        self._logger.info(
            "[TTS] Rediscovering speaker (name=%r) after send failure to %s"
            % (cc_name, tried or [cc_ip]))
        speakers = discover_speakers(timeout=_kDiscoverTimeout,
                                     logger=self._logger)
        target = None
        if cc_name:
            for s in speakers:
                if s['name'] == cc_name:
                    target = s
                    break
        elif len(speakers) == 1:
            # No saved name, but a single speaker on the LAN — use it.
            target = speakers[0]

        if target is None:
            raise RuntimeError(
                "Could not reach speaker %r (tried %s); rediscovery found no "
                "match among %d device(s)"
                % (cc_name or cc_ip, ", ".join(tried) or cc_ip, len(speakers)))

        newIp = target['ip']
        if newIp in tried:
            # Already tried this exact address; don't loop.
            raise (lastErr or RuntimeError("Speaker at %s unreachable" % newIp))

        self._cast_to_ip(newIp, audio_url, duration)
        if cc_name:
            self._ipCache[cc_name] = newIp
        self._logger.info("[TTS] Cast complete to %s (%s) via rediscovery"
                          % (newIp, cc_name or '?'))

    def _do_play(self, text, voice, speed, output, cc_ip, cc_name=''):
        """Generate + play one TTS request.  Raises on error."""
        filepath = self._generate(text, voice, speed)
        if output == 'chromecast' and (cc_ip or cc_name):
            self._cast(filepath, cc_ip, cc_name)
        else:
            self._play_local(filepath)

    def _do_cast_file(self, filepath, cc_ip, cc_name=''):
        """Cast an existing WAV file (e.g. a rule's "Play sound") to a speaker.

        The built-in HTTP server only serves from _kCacheDir, so make the file
        reachable there (copied once, keyed by a hash of its full path so two
        different sources with the same basename don't collide), then reuse the
        same cast+fallback path as TTS.
        """
        import hashlib
        import shutil

        if not filepath or not os.path.exists(filepath):
            raise RuntimeError("sound file not found: %r" % filepath)

        digest = hashlib.md5(os.path.abspath(filepath).encode('utf-8')).hexdigest()[:8]
        served = os.path.join(_kCacheDir, "snd_%s_%s" % (digest,
                                                         os.path.basename(filepath)))
        if not os.path.exists(served):
            shutil.copy2(filepath, served)
        self._cast(served, cc_ip, cc_name)

    # ── Queue worker ─────────────────────────────────────────────────────────

    def _run(self):
        import time
        while True:
            kind, payload, done_cb = self._queue.get()
            try:
                if kind == 'castfile':
                    self._do_cast_file(*payload)
                else:  # 'tts'
                    self._do_play(*payload)
                if done_cb:
                    done_cb(None)
            except Exception as e:
                self._logger.error("[TTS] Playback error: %s" % e)
                if done_cb:
                    done_cb(str(e))
            finally:
                self._queue.task_done()
            time.sleep(0.5)

    # ── Public API ────────────────────────────────────────────────────────────

    def queue_tts(self, text, voice='af_heart', speed=1.0,
                  output='local', cc_ip='', cc_name='', done_callback=None):
        """Enqueue a TTS request.

        @param text          Text to speak.
        @param voice         Kokoro voice code (e.g. 'af_heart').
        @param speed         Speech rate multiplier (0.5–2.0).
        @param output        'local' or 'chromecast'.
        @param cc_ip         Chromecast IP (when output=='chromecast').
        @param cc_name       Chromecast friendly name; used to re-discover the
                             speaker if its saved IP stops responding.
        @param done_callback Optional callable(error_str_or_None) called when
                             playback finishes.  Called from the worker thread
                             — use wx.CallAfter to update UI.
        """
        self._queue.put(('tts',
                         (text, voice, speed, output, cc_ip, cc_name),
                         done_callback))
        self._logger.info("[TTS] Queued: %r (voice=%s)" % (text, voice))

    def queue_cast_file(self, filepath, cc_ip='', cc_name='',
                        done_callback=None):
        """Enqueue an existing WAV file to be cast to a network speaker.

        Used by the "Play sound" response when its output is a speaker rather
        than the local machine.  Reuses the TTS worker thread, HTTP server, and
        moved-speaker rediscovery fallback.
        """
        self._queue.put(('castfile', (filepath, cc_ip, cc_name),
                         done_callback))
        self._logger.info("[TTS] Queued sound cast: %r -> %s (%s)"
                          % (os.path.basename(filepath or ''), cc_ip,
                             cc_name or '?'))

    def models_ready(self):
        """Return True if model files are already downloaded."""
        return (os.path.exists(_kModelPath) and
                os.path.exists(_kVoicesPath))
