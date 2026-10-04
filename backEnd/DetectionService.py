"""
DetectionService.py

The shared ML inference service: ONE process that owns YOLO (torch/CUDA) and,
when enabled in imagecheck_config.json, InsightFace and NudeNet — serving
every camera process over a localhost socket (see DetectionServiceClient).

Why: per-camera model loading cost ~1.3-1.9 GB RAM and one CUDA context per
camera.  At 15 cameras that saturated 32 GB of system RAM and 4 GB of VRAM
(GPU sat idle while inference silently fell back to CPU).  One service = one
copy of the models, one CUDA context, GPU actually used.

Design notes:
  * This module's top-level imports are LIGHT.  BackEndApp imports it to
    spawn the child, and must not pay for torch.  All ML imports happen
    inside _Models.load() in the service process.
  * Inference is serialized with a single lock — a nano YOLO on GPU runs in
    ~5-10 ms, and detection traffic is motion-gated and bursty, so a simple
    lock outperforms anything clever at this scale.
  * Which YOLO and NudeNet weights to run are chosen in the Options dialog
    (YOLO_MODEL / NUDE_MODEL).  Both are resolved to absolute paths under
    <install root>\\models; a name that resolves to nothing falls back to the
    shipped default rather than letting ultralytics download one silently.
  * The config file is re-checked (mtime) every few seconds; newly enabled
    models (RUN_FACE/RUN_NUDITY flipped on in the Options dialog) are loaded
    without a restart.  Models are never unloaded, and CHANGING a model takes
    effect only on the next service start — the load runs under the inference
    lock, so swapping one live would stall every camera.
  * The listening port is dynamic; it's published to
    <config dir>/detection_service.port for clients to read.
  * The names of the models actually loaded are published to
    <config dir>/detection_models.json after every load, for the System tab.
    Config-enabled is not the same as running: a load can fail, and an
    uninstalled name falls back, so only this process knows the truth.
"""

import contextlib
import json
import logging
import logging.handlers
import os
import socket
import threading
import time

import numpy as np

from backEnd.ImageCheckConfig import (
    DEFAULTS as _DEFAULTS, getConfigPath, loadConfig, migrateConfig,
    nudeModelResolution, onnxRuntimeTargets, resolveNudeModelPath,
    resolveYoloModelPath,
)
from backEnd.DetectionServiceClient import (
    modelsFilePath, portFilePath, sendMsg, recvMsg,
)

_kConfigPollSecs = 5.0
_kLogName = 'DetectionService.log'
_kLogSize = 5 * 1024 * 1024


# Requests at or above this get a line in the log.  Normal CPU inference here
# is tens to hundreds of ms; anything past a second means either a genuinely
# slow model or a queue behind the inference lock, and the log line separates
# those two so the 640m-vs-320n tradeoff can be read off real numbers.
_kSlowRequestMs = 1000.0

# A request in flight this long is dropped rather than run: the client gives up
# at DetectionServiceClient._kRequestTimeout (30s) and closes the socket, so the
# inference slot would be spent producing a frame nobody can receive.  Kept
# under that timeout so work is shed just BEFORE the client stops waiting for
# it, never after.
#
# This is the backpressure the design note above lacks.  That note argues a
# plain lock beats anything cleverer because "a nano YOLO on GPU runs in
# ~5-10ms" -- true on a GPU, false when CUDA is unavailable and the premise
# silently inverts.  Measured on a CPU-only host 2026-08-29: 500s per request,
# 460s of it queued on this lock, 1066 service threads, 12.1GB of commit.
# Without a deadline the queue has no bound and the collapse is self-sustaining,
# because every client timeout adds a reconnect and another queued thread.
_kRequestDeadlineSecs = 25.0

# Log every Nth drop.  Once the service is shedding it sheds a lot, and a line
# per dropped frame would bury the reason it started.
_kExpiryLogEvery = 50

# Close a connection idle this long.  The client reconnects transparently per
# request, so this is invisible in normal use; it exists so a half-open socket
# cannot park a service thread forever, which is how the thread count above was
# reached in the first place.
_kIdleTimeoutSecs = 300.0

# Heartbeat cadence, and the thread count under which a fully idle service stays
# silent.
_kHeartbeatSecs = 60.0
_kQuietThreads = 16

# accept() can fail without the listener being dead.  A peer that resets
# between connect and accept gives ConnectionAbortedError, and a connection
# storm gives EMFILE -- both routine, both survivable, and both used to end
# the accept loop permanently and leave a live process answering nothing.
# Retry with a short pause; give up only if it never recovers.
_kAcceptMaxFails = 20
_kAcceptRetrySecs = 0.5

# Per-thread scratch for handing the lock wait back to _handleClient.  One
# connection is one thread, so this stays private to the request in flight.
_tls = threading.local()


class _Expired(Exception):
    """A queued request whose client has already given up waiting for it."""


def _requireImage(img, op):
    """Reject anything but an image array, BEFORE it can reach the lock.

    Every legitimate caller sends np.ascontiguousarray(...) -- see the three
    call sites in DetectionServiceClient -- so real traffic pays one
    isinstance check.  What this keeps out is a bare string, which neither
    model library rejects:

      * nudenet._read_image does `if isinstance(image_path, str):
        cv2.imread(image_path)`, so a string is read as a local file path on
        this host and classified for the caller.
      * ultralytics check_source() treats a string as a file/URL to
        download, or -- for anything whose extension it does not recognise
        -- as a WEBCAM or stream, opened with cv2.VideoCapture.  Measured
        against reserved address space (RFC 5737): an unreachable rtsp://
        blocks for 30.1 s.  That block would happen while HOLDING the
        inference lock, stalling every camera, and it cannot be shed: the
        deadline covers the wait FOR the lock, never the work done under it.

    The service listens on 127.0.0.1 with no authentication, so any local
    process can send this even though the real client never would.

    @param  img  The payload from the request.
    @param  op   Operation name, for the error message.
    """
    if not isinstance(img, np.ndarray):
        raise ValueError("%s: img must be a numpy array, got %s"
                         % (op, type(img).__name__))


class _Stats:
    """Service-wide counters, shared across every connection thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._served = 0
        self._expired = 0

    def noteServed(self):
        with self._lock:
            self._served += 1

    def noteExpired(self):
        with self._lock:
            self._expired += 1
            return self._expired

    def snapshot(self):
        with self._lock:
            return self._served, self._expired


class _Models:
    """Owns the ML models; loads lazily/eagerly per config."""

    def __init__(self, logger):
        self._logger = logger
        self._lock = threading.Lock()      # serializes ALL inference
        self._yolo = None
        self._face = None
        self._nude = None
        # Names of what is ACTUALLY loaded, for the System tab.  Set only on a
        # successful load, and taken from the RESOLVED file rather than the
        # config, so a fallback (_resolveModel) is reported honestly.
        self._yoloName = None
        self._faceName = None
        self._nudeName = None

    # -- loading ---------------------------------------------------------

    def _resolveModel(self, name, default, resolver, label):
        """Resolve a configured model name to an absolute path.

        Falls back to the shipped default when the configured file is not
        installed, so a config naming a model this build does not carry
        degrades to a working detector plus a log line, rather than to no
        detection at all.

        @param  name      Name from the config, e.g. "yolo11s.pt".
        @param  default   The name to fall back to (from DEFAULTS).
        @param  resolver  name -> absolute path or None.
        @param  label     Model family, for log messages.
        @return           Absolute path, or None when nothing could be found.
        """
        path = resolver(name)
        if path is None and name != default:
            self._logger.warning(
                "%s: model %r is not installed; falling back to %s",
                label, name, default)
            name = default
            path = resolver(name)
        if path is None:
            self._logger.error(
                "%s: no model file found for %r -- %s detection is disabled",
                label, name, label)
        return path

    def load(self, cfg):
        """Load whatever the config asks for that isn't loaded yet."""
        with self._lock:
            # Wrapped like the Face and NudeNet blocks below.  Without this a
            # broken YOLO -- a corrupt .pt, a torch/CUDA mismatch, an OOM
            # during construction -- propagated straight out of load() and
            # the two blocks after it never ran, so face and nudity
            # detection stayed unattempted on every retry too.  The log said
            # only that YOLO had failed, never that it had taken the others
            # with it.
            try:
                if self._yolo is None:
                    from ultralytics import YOLO
                    name = cfg.get("YOLO_MODEL", _DEFAULTS["YOLO_MODEL"])
                    weights = self._resolveModel(
                        name, _DEFAULTS["YOLO_MODEL"], resolveYoloModelPath,
                        "YOLO")
                    # Hand ultralytics an ABSOLUTE path, never a bare name:
                    # given a name it cannot find, it downloads from GitHub
                    # into the process's working directory -- which for the
                    # back end is wherever the service account started.
                    # _resolveModel returns None instead, and we decline to
                    # load rather than fetch.
                    if weights is not None:
                        self._logger.info("loading YOLO weights: %s", weights)
                        t0 = time.time()
                        self._yolo = YOLO(weights)
                        self._yoloName = os.path.basename(weights)
                        try:
                            import torch
                            self._logger.info(
                                "YOLO loaded in %.1fs (cuda available: %s)",
                                time.time() - t0, torch.cuda.is_available())
                        except Exception:
                            self._logger.info("YOLO loaded in %.1fs",
                                              time.time() - t0)
            except Exception:
                self._logger.error("YOLO load failed", exc_info=True)

            if self._face is None and cfg.get("RUN_FACE",
                                              _DEFAULTS["RUN_FACE"]):
                try:
                    # onnxruntime-gpu needs CUDA/cuDNN DLLs resolvable at
                    # session creation; torch (already imported for YOLO)
                    # bundles them, and preload_dlls() wires them up.  Must
                    # run before any InferenceSession or every session
                    # silently falls back to CPU.
                    try:
                        import onnxruntime as _ort
                        _ort.preload_dlls()
                    except Exception:
                        pass
                    from insightface.app import FaceAnalysis
                    self._logger.info("loading InsightFace/ArcFace ...")
                    t0 = time.time()
                    # An installed build ships buffalo_l inside the program, so
                    # point InsightFace at it.  Left to itself it downloads
                    # ~280 MB into the HOME of whoever runs the back end --
                    # which under the service is not the user's profile at all,
                    # and needs internet on a machine that may have none.
                    faceKwargs = {}
                    try:
                        from appCommon.InstallPaths import getInsightFaceRoot
                        bundledRoot = getInsightFaceRoot()
                        if bundledRoot:
                            faceKwargs["root"] = bundledRoot
                            self._logger.info("using bundled face models: %s",
                                              bundledRoot)
                    except Exception:
                        pass
                    # Ask for what onnxruntime actually has.  Requesting the
                    # CUDA provider on a machine with no NVIDIA GPU crashes the
                    # process outright rather than falling back -- a native
                    # access violation, so the except below never sees it and
                    # the service just vanishes.  See onnxRuntimeTargets().
                    faceProviders, faceCtxId = onnxRuntimeTargets()
                    self._logger.info("InsightFace providers: %s (ctx_id %d)",
                                      ",".join(faceProviders), faceCtxId)
                    face = FaceAnalysis(
                        name="buffalo_l",
                        providers=faceProviders,
                        **faceKwargs)
                    # det_thresh 0.2 (not the 0.5 default) so LOW-score faces
                    # are returned too — parity with the batch enroller
                    # (EnrollFaces).  Production callers apply their own
                    # floors client-side (FACE_DET_CONF, ENROLL_MIN_DET), but
                    # the baseline REBUILD must see marginal faces or
                    # previously-enrolled crops silently drop out of
                    # known_faces.dat on every rebuild.
                    face.prepare(ctx_id=faceCtxId, det_size=(640, 640),
                                 det_thresh=0.2)
                    self._face = face
                    self._faceName = "buffalo_l"
                    self._logger.info("InsightFace loaded in %.1fs",
                                      time.time() - t0)
                except Exception:
                    self._logger.error("InsightFace load failed",
                                       exc_info=True)

            if self._nude is None and cfg.get("RUN_NUDITY",
                                              _DEFAULTS["RUN_NUDITY"]):
                try:
                    # Same preload as the face branch: ORT needs torch's CUDA/
                    # cuDNN DLLs wired BEFORE the InferenceSession is created,
                    # or nudity inference silently lands on the CPU.  Do it
                    # here too so it doesn't depend on RUN_FACE being on.
                    try:
                        import onnxruntime as _ort
                        _ort.preload_dlls()
                    except Exception:
                        pass
                    from nudenet import NudeDetector
                    name = cfg.get("NUDE_MODEL", _DEFAULTS["NUDE_MODEL"])
                    path = self._resolveModel(
                        name, _DEFAULTS["NUDE_MODEL"], resolveNudeModelPath,
                        "NudeNet")
                    if path is None:
                        raise RuntimeError("no NudeNet model file")
                    # The name may have fallen back, so take the resolution
                    # from the file we ACTUALLY resolved.  320n and 640m are
                    # the same graph trained at different imgsz: preprocessing
                    # at the wrong one throws nothing, it just returns boxes
                    # scaled by 2x that still look like real detections.
                    resolution = nudeModelResolution(
                        os.path.basename(path))
                    self._logger.info("loading NudeNet %s (%d px) from %s",
                                      os.path.basename(path), resolution, path)
                    t0 = time.time()
                    self._nude = NudeDetector(model_path=path,
                                              inference_resolution=resolution)
                    self._nudeName = os.path.basename(path)
                    # The vendored nudenet builds its InferenceSession with no
                    # providers= argument (nudenet.py:152 is commented out), so
                    # it binds CPU-only even with onnxruntime-gpu + CUDA present.
                    # Rebuild the session on CUDA explicitly.  It MUST be the
                    # same file the detector was constructed from, or the
                    # preprocessing size and the graph disagree -- see above.
                    #
                    # Only worth doing when CUDA is really there: the default
                    # session is already CPU, so on a GPU-less machine this
                    # would buy nothing and risk everything (asking for the
                    # CUDA provider without a GPU can kill the process outright,
                    # natively, past any except -- see onnxRuntimeTargets()).
                    nudeProviders, _nudeCtxId = onnxRuntimeTargets()
                    if 'CUDAExecutionProvider' in nudeProviders:
                        try:
                            import onnxruntime as _ort
                            self._nude.onnx_session = _ort.InferenceSession(
                                path, providers=nudeProviders)
                            # The rebuilt session is authoritative for the feed
                            # name.
                            self._nude.input_name = \
                                self._nude.onnx_session.get_inputs()[0].name
                        except Exception:
                            self._logger.warning("NudeNet: CUDA session rebuild "
                                                 "failed; keeping default session",
                                                 exc_info=True)
                    else:
                        self._logger.info("NudeNet: no CUDA provider available; "
                                          "keeping the default CPU session")
                    try:
                        provs = self._nude.onnx_session.get_providers()
                        self._logger.info(
                            "NudeNet loaded in %.1fs (%d px, providers: %s)",
                            time.time() - t0, resolution, provs)
                    except Exception:
                        self._logger.info("NudeNet loaded in %.1fs",
                                          time.time() - t0)
                except Exception:
                    self._logger.error("NudeNet load failed", exc_info=True)

        # Outside the lock: republish the loaded set.  Done on EVERY load, not
        # just the first, so a model enabled at runtime by the config watcher
        # shows up without a service restart.
        _writeModelsFile(self, self._logger)

    # -- inference -------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, deadline=None):
        """Take the inference lock, recording how long the wait was.

        Every op serializes here, so time spent waiting is time one camera
        spent queued behind another -- distinguishing that from model time is
        the whole point of the measurement.

        It is also the only place that sees the whole queue, so it is where a
        request that waited past its deadline is abandoned: raising _Expired
        here costs nothing and frees the slot for a frame someone is still
        waiting on.  Checked AFTER the lock is taken, because the wait itself is
        what the deadline is about.
        """
        t0 = time.monotonic()
        with self._lock:
            _tls.lockWaitMs = (time.monotonic() - t0) * 1000.0
            if deadline is not None and time.monotonic() > deadline:
                raise _Expired()
            yield

    def caps(self):
        return {'yolo': self._yolo is not None,
                'face': self._face is not None,
                'nudity': self._nude is not None,
                'names': {'yolo': self._yoloName,
                          'face': self._faceName,
                          'nudity': self._nudeName}}

    def loadedNames(self):
        """Names of the loaded models, in display order, skipping what's off.

        Order matches the System tab line: YOLO, NudeNet, then ArcFace.
        """
        return [n for n in (self._yoloName, self._nudeName, self._faceName)
                if n]

    def yolo(self, img, conf, deadline=None):
        _requireImage(img, "yolo")
        rows = []
        names = {}
        with self._locked(deadline):
            if self._yolo is None:
                raise RuntimeError("YOLO not loaded")
            names = self._yolo.names
            results = self._yolo(img, verbose=False, conf=conf)
            # One device->host transfer per tensor per frame, and nothing
            # else under the lock.  box.cls[0] / box.conf[0] / box.xyxy[0]
            # each force their own CUDA sync when the model ran on the GPU,
            # so the old per-box loop spent 3.6 ms of LOCK time on a 20-box
            # frame (9.1 ms at 50) against 0.13 ms for this, flat -- measured
            # on this machine, against a model call the design note above
            # budgets at 5-10 ms.  face() and nudity() already kept their
            # post-processing outside the lock; yolo() is the hot path and
            # was the one that did not.
            for r in results:
                boxes = r.boxes
                if len(boxes) == 0:
                    continue
                rows.append((boxes.cls.reshape(-1).tolist(),
                             boxes.conf.reshape(-1).tolist(),
                             boxes.xyxy.reshape(-1, 4).tolist()))

        dets = []
        for clsList, confList, xyxyList in rows:
            for i in range(len(clsList)):
                x1, y1, x2, y2 = xyxyList[i]
                dets.append((names[int(clsList[i])], float(confList[i]),
                             int(x1), int(y1), int(x2), int(y2)))
        return dets

    def face(self, img, deadline=None):
        _requireImage(img, "face")
        with self._locked(deadline):
            if self._face is None:
                return []
            faces = self._face.get(img)
        out = []
        for f in faces:
            det_score = None
            try:
                if getattr(f, 'det_score', None) is not None:
                    det_score = float(f.det_score)
            except Exception:
                pass
            sex = None
            try:
                if getattr(f, 'sex', None):
                    sex = f.sex
                elif getattr(f, 'gender', None) is not None:
                    sex = 'M' if int(f.gender) == 1 else 'F'
            except Exception:
                pass
            age = None
            try:
                if getattr(f, 'age', None) is not None:
                    age = int(f.age)
            except Exception:
                pass
            # Guarded like det_score/sex/age above, which each leave None on
            # failure.  These two were the asymmetric pair: an np.asarray
            # that raised for ONE face abandoned the whole out list, losing
            # the fields already computed for every other face in the frame
            # and turning the request into an error instead of a partial
            # result.
            bbox = None
            try:
                raw = getattr(f, 'bbox', None)
                if raw is not None:
                    bbox = np.asarray(raw, dtype=np.float32)
            except Exception:
                pass
            emb = None
            try:
                raw = getattr(f, 'embedding', None)
                if raw is not None:
                    emb = np.asarray(raw, dtype=np.float32)
            except Exception:
                pass
            out.append({
                'det_score': det_score,
                'sex': sex,
                'age': age,
                'bbox': bbox,
                'embedding': emb,
            })
        return out

    def nudity(self, img_bgr, deadline=None):
        _requireImage(img_bgr, "nudity")
        with self._locked(deadline):
            if self._nude is None:
                return []
            dets = self._nude.detect(img_bgr)
        # Ensure plain picklable types only.
        out = []
        for d in dets or []:
            try:
                out.append({'class': str(d.get('class', '')),
                            'score': float(d.get('score', 0.0))})
            except Exception:
                continue
        return out


def _writeModelsFile(models, logger):
    """Publish the loaded model names for the System tab.

    Written atomically (tmp + replace) like the port file, so a reader never
    sees a half-written file.  The pid lets the reader ignore a file left
    behind by a service that has since died.  Failure is logged and swallowed
    -- this is a status file, and nothing about detection depends on it.
    """
    path = modelsFilePath()
    try:
        payload = {'pid': os.getpid(),
                   'writtenMs': time.time() * 1000.0,
                   'models': models.loadedNames()}
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        logger.error("could not write models file %s", path, exc_info=True)


def _handleClient(conn, addr, models, stats, logger):
    """Serve one camera process's persistent connection."""
    try:
        # NOT settimeout(None): a peer that vanished without a FIN would park
        # this thread in recv forever, and one stuck thread per abandoned
        # connection is precisely how this service used to drown.
        conn.settimeout(_kIdleTimeoutSecs)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        while True:
            try:
                req = recvMsg(conn)
            except (ConnectionError, OSError):
                # socket.timeout lands here too (it is an OSError): an idle or
                # half-open peer.  The client reconnects on its next request.
                break
            op = req.get('op') if isinstance(req, dict) else None
            _tls.lockWaitMs = 0.0
            t0 = time.monotonic()
            deadline = t0 + _kRequestDeadlineSecs
            try:
                if op == 'ping':
                    # Cheap, and callers use it to decide whether the service
                    # exists at all -- never drop it on the deadline.
                    resp = {'ok': True, 'caps': models.caps()}
                elif op == 'yolo':
                    resp = {'ok': True,
                            'dets': models.yolo(req['img'],
                                                float(req.get('conf', 0.25)),
                                                deadline)}
                elif op == 'face':
                    resp = {'ok': True,
                            'faces': models.face(req['img'], deadline)}
                elif op == 'nudity':
                    resp = {'ok': True,
                            'dets': models.nudity(req['img'], deadline)}
                else:
                    resp = {'ok': False, 'err': 'unknown op %r' % (op,)}
                # Only count work we actually did.  An unknown op used to be
                # booked as served, which inflated the heartbeat's rate
                # against a request nothing ran for.
                if resp.get('ok'):
                    stats.noteServed()
            except _Expired:
                # Answer "nothing detected" rather than an error: callers
                # already degrade to no-detections, and a clean empty result
                # keeps a shedding service out of their error path -- which is
                # what keeps the caller's outstanding-request accounting
                # balanced (see QueuedDataManagerCloud._processCloudResults).
                total = stats.noteExpired()
                if total == 1 or total % _kExpiryLogEvery == 0:
                    logger.warning(
                        "dropped %s request after %.0f s queued past its"
                        " deadline (%d dropped so far, %d threads):"
                        " inference is not keeping up with the frame rate",
                        op, getattr(_tls, 'lockWaitMs', 0.0) / 1000.0,
                        total, threading.active_count())
                resp = {'ok': True, 'dets': [], 'faces': [], 'expired': True}
            except Exception as e:
                logger.error("request failed: %r", e, exc_info=True)
                resp = {'ok': False, 'err': repr(e)}

            elapsedMs = (time.monotonic() - t0) * 1000.0
            if elapsedMs >= _kSlowRequestMs:
                waitMs = getattr(_tls, 'lockWaitMs', 0.0)
                # "model/post-processing", not "in the model": face() and
                # nudity() build their results after releasing the lock, so
                # that time lands in elapsedMs but not in waitMs.
                logger.info(
                    "slow %s request: %.0f ms total = %.0f ms queued on the"
                    " inference lock + %.0f ms model/post-processing"
                    " (%d threads)",
                    op, elapsedMs, waitMs, elapsedMs - waitMs,
                    threading.active_count())

            try:
                sendMsg(conn, resp)
            except (ConnectionError, OSError):
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _heartbeat(stats, logger, stop):
    """Periodic one-liner: thread count, requests served, requests dropped.

    Thread count is what identifies a connection storm at a glance -- it is the
    figure separating "the model is slow" from "the service is drowning in
    abandoned connections", and until now nothing logged it.  Silent while the
    service is idle AND healthy, so it costs nothing on a quiet system but is
    already running when one goes bad.
    """
    served = expired = 0
    while not stop.wait(_kHeartbeatSecs):
        try:
            nowServed, nowExpired = stats.snapshot()
            threads = threading.active_count()
            if (nowServed == served and nowExpired == expired
                    and threads <= _kQuietThreads):
                continue
            logger.info(
                "heartbeat: %d threads, %d served (+%d), %d dropped (+%d)",
                threads, nowServed, nowServed - served,
                nowExpired, nowExpired - expired)
            served, expired = nowServed, nowExpired
        except Exception:
            logger.error("heartbeat error", exc_info=True)


def _configWatcher(models, logger, stop):
    """Reload config on change so RUN_FACE/RUN_NUDITY toggles take effect
    without a service restart.

    Note this thread does NOT insulate cameras from a model load: _Models.load
    takes the inference lock for its whole body, so a load started here blocks
    every camera's request until it finishes.  Tolerable because a load only
    happens when a model is enabled for the first time.  It is also why
    CHANGING a model requires a restart rather than swapping in place.
    """
    path = getConfigPath()
    last = None
    while not stop.wait(_kConfigPollSecs):
        try:
            mtime = os.path.getmtime(path) if os.path.isfile(path) else None
            if mtime != last:
                last = mtime
                models.load(loadConfig())
        except Exception:
            logger.error("config watcher error", exc_info=True)


def runDetectionService(userDir, logDir):
    """Child-process entry point (spawned by BackEndProcessJumper)."""
    logger = logging.getLogger('DetectionService')
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(logDir, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(
            os.path.join(logDir, _kLogName),
            maxBytes=_kLogSize, backupCount=1)
        h.setFormatter(logging.Formatter(
            '%(asctime)s - %(process)d - %(levelname)s - %(message)s'))
        logger.addHandler(h)
    except Exception:
        logging.basicConfig(level=logging.INFO)

    logger.info("=== DetectionService starting (pid %d) ===", os.getpid())

    # Bring an existing config forward before reading it, so a default that has
    # since changed applies even on a machine where Options is never opened.
    try:
        if migrateConfig():
            logger.info("imagecheck_config.json migrated to current defaults")
    except Exception:
        logger.error("config migration failed", exc_info=True)

    models = _Models(logger)
    try:
        models.load(loadConfig())
    except Exception:
        # Keep serving; the config watcher retries the load and clients get
        # clean errors (degraded to no-detections) meanwhile.
        logger.error("initial model load failed", exc_info=True)

    stop = threading.Event()
    threading.Thread(target=_configWatcher, args=(models, logger, stop),
                     daemon=True).start()

    stats = _Stats()
    threading.Thread(target=_heartbeat, args=(stats, logger, stop),
                     daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(32)
    port = srv.getsockname()[1]

    # Publish the port atomically so clients never read a partial file.
    pf = portFilePath()
    try:
        tmp = pf + '.tmp'
        with open(tmp, 'w') as f:
            f.write('%d %d\n' % (port, os.getpid()))
        os.replace(tmp, pf)
    except Exception:
        logger.error("could not write port file %s", pf, exc_info=True)
        # Fatal -- no client can find us without the port file -- but leave
        # nothing behind: this return used to skip the cleanup below, so the
        # listening socket stayed open and the watcher and heartbeat threads
        # kept running.
        stop.set()
        try:
            srv.close()
        except Exception:
            pass
        return
    logger.info("listening on 127.0.0.1:%d", port)

    consecutiveFails = 0
    try:
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                # A failed accept() is not a dead listener.  A peer that
                # resets between connect and accept raises
                # ConnectionAbortedError, and running out of descriptors
                # under a connection storm raises EMFILE -- the 2026-08-29
                # shape exactly.  Both used to end this loop for good,
                # leaving a process that was alive, held the models and the
                # port file, and accepted nothing ever again.
                consecutiveFails += 1
                if consecutiveFails > _kAcceptMaxFails:
                    logger.error("accept() failed %d times running; stopping",
                                 consecutiveFails, exc_info=True)
                    raise
                logger.warning("accept() failed (%d in a row); retrying",
                               consecutiveFails, exc_info=True)
                stop.wait(_kAcceptRetrySecs)
                continue
            consecutiveFails = 0
            threading.Thread(target=_handleClient,
                             args=(conn, addr, models, stats, logger),
                             daemon=True).start()
    except Exception:
        logger.error("server loop exited", exc_info=True)
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:
            pass
