"""
DetectionServiceClient.py

Thin RPC client for the shared DetectionService process.

Camera processes used to load the whole ML stack (torch/YOLO + InsightFace +
NudeNet) EACH — ~1.3-1.9 GB of RAM and one CUDA context per camera, which
saturated both system RAM and GPU VRAM at 10+ cameras.  Instead, one
DetectionService process owns the models and every camera talks to it over a
localhost socket.  This module is deliberately light: importing it must not
pull in torch/ultralytics/insightface/nudenet (that's the whole point).

Protocol: length-prefixed (4-byte big-endian) pickles over TCP on 127.0.0.1.
The service writes its port to <config dir>/detection_service.port at startup.
Every public method raises on failure (service down, timeout, error reply);
callers already wrap detection calls in try/except and degrade gracefully to
"no detections", which matches the behaviour when models were loading locally.
"""

import os
import pickle
import socket
import struct
import threading
import time
from types import SimpleNamespace

import numpy as np

from backEnd.ImageCheckConfig import getConfigPath

_kPortFileName = 'detection_service.port'
_kModelsFileName = 'detection_models.json'
_kMaxMessage   = 64 * 1024 * 1024   # sanity cap on a single message
_kConnectTimeout = 5.0
# Generous request timeout: a cold first inference after model load can take
# a few seconds on GPU init; normal requests are tens of ms.
_kRequestTimeout = 30.0

# Cameras routinely start before the service has bound its port -- a cold
# start that also loads NudeNet takes ~20 s.  Retrying inside _connect turns
# what used to be a burst of separate ConnectionRefused failures (one per
# camera every couple of seconds, logged as WinError 10061) into a single
# quiet wait.  Deliberately shorter than a worst-case cold start: the caller
# drops the frame and the next one reconnects, which beats blocking a
# camera's detector thread for half a minute.
_kConnectRetryStart = 0.25
_kConnectRetryMax   = 4.0
_kConnectRetryTotal = 8.0

# After a request TIMES OUT, stop asking for this long.  A timeout means the
# service is queued deeper than _kRequestTimeout, and the reflex of dropping the
# socket and reconnecting on the very next frame is what turns a slow service
# into a thread storm: every reconnect is another service thread holding another
# queued frame, which makes the queue deeper still.  Backing off hands the
# service room to drain.  Short enough that recovery is a second or two, not a
# stall.
_kTimeoutCooldownSecs = 5.0


def portFilePath():
    """Path of the file the service publishes its listening port in."""
    return os.path.join(os.path.dirname(getConfigPath()), _kPortFileName)


def modelsFilePath():
    """Path of the file the service publishes its LOADED model names in.

    A file rather than an RPC: the health view polls every couple of seconds
    and the message server must never block on a socket to a service that may
    be busy or wedged.  Written by the process that did the loading, so it
    reports what is actually in memory -- not what the config asked for.
    """
    return os.path.join(os.path.dirname(getConfigPath()), _kModelsFileName)


def sendMsg(sock, obj):
    data = pickle.dumps(obj, protocol=4)
    if len(data) > _kMaxMessage:
        raise ValueError("message too large: %d" % len(data))
    sock.sendall(struct.pack('!I', len(data)) + data)


def recvMsg(sock):
    hdr = _recvExact(sock, 4)
    (n,) = struct.unpack('!I', hdr)
    if n > _kMaxMessage:
        raise ValueError("message too large: %d" % n)
    return pickle.loads(_recvExact(sock, n))


def _recvExact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


class DetectionBusy(RuntimeError):
    """Raised while backing off after a timeout; callers treat it as a miss.

    A distinct type so a caller can tell "the service is saturated" from "the
    service is broken", though both degrade to no-detections today.
    """


class DetectionServiceClient:
    """Persistent-connection client; reconnects transparently per request."""

    def __init__(self, logger=None):
        self._logger = logger
        self._sock = None
        self._lock = threading.Lock()
        # monotonic deadline; 0 means "not backing off"
        self._coolUntil = 0.0

    # -- plumbing --------------------------------------------------------

    def _connect(self):
        """Connect to the service, tolerating a brief startup window.

        The port file is re-read on every attempt on purpose: a service that
        restarted publishes a NEW port, so a cached one would keep being
        refused for as long as the client held it.
        """
        deadline = time.monotonic() + _kConnectRetryTotal
        delay = _kConnectRetryStart
        while True:
            try:
                with open(portFilePath(), 'r') as f:
                    port = int(f.read().split()[0])
                sock = socket.create_connection(('127.0.0.1', port),
                                                timeout=_kConnectTimeout)
                sock.settimeout(_kRequestTimeout)
                return sock
            except (OSError, ValueError, IndexError):
                # No port file yet, a partial one, or nothing listening.
                # Re-raise the real error once the budget is spent so the
                # caller still sees why it failed.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, _kConnectRetryMax)

    def _request(self, payload):
        with self._lock:
            now = time.monotonic()
            if now < self._coolUntil:
                raise DetectionBusy(
                    "detection service saturated; backing off for %.1f s"
                    % (self._coolUntil - now))
            try:
                if self._sock is None:
                    self._sock = self._connect()
                sendMsg(self._sock, payload)
                resp = recvMsg(self._sock)
            except Exception as e:
                # Drop the connection; next request reconnects (the service
                # may have restarted on a new port).
                try:
                    if self._sock is not None:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
                # A TIMEOUT is the saturation signal specifically.  A refused or
                # reset connection means the service RESTARTED, and those must
                # reconnect immediately -- backing off there would make a cold
                # start worse, not better (see _connect's retry window).
                if isinstance(e, socket.timeout):
                    self._coolUntil = (time.monotonic()
                                       + _kTimeoutCooldownSecs)
                    if self._logger is not None:
                        self._logger.warning(
                            "detection request timed out after %.0f s;"
                            " pausing detection for %.0f s",
                            _kRequestTimeout, _kTimeoutCooldownSecs)
                raise
            self._coolUntil = 0.0
        if not isinstance(resp, dict) or not resp.get('ok'):
            err = resp.get('err') if isinstance(resp, dict) else repr(resp)
            raise RuntimeError("detection service error: %s" % err)
        return resp

    def close(self):
        with self._lock:
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None

    # -- API -------------------------------------------------------------

    def ping(self):
        """Return the service's capability dict, e.g. {'face': True, ...}."""
        return self._request({'op': 'ping'}).get('caps', {})

    def yolo(self, img_rgb, conf):
        """Run YOLO on an RGB ndarray.

        @return list of (label, score, x1, y1, x2, y2) for ALL classes; the
                caller filters to the classes it cares about."""
        resp = self._request({'op': 'yolo',
                              'img': np.ascontiguousarray(img_rgb),
                              'conf': float(conf)})
        return resp.get('dets', [])

    def face(self, img_rgb):
        """Run InsightFace on an RGB ndarray.

        @return list of SimpleNamespace(det_score, sex, age, embedding) —
                attribute-compatible with insightface's Face objects as used
                by ObjectDetectorClientImageCheck.  Empty list when the
                service has no face model loaded (RUN_FACE off)."""
        resp = self._request({'op': 'face',
                              'img': np.ascontiguousarray(img_rgb)})
        out = []
        for f in resp.get('faces', []):
            emb = f.get('embedding')
            bbox = f.get('bbox')
            out.append(SimpleNamespace(
                det_score=f.get('det_score') or 0.0,
                sex=f.get('sex'),
                gender=None,
                age=f.get('age'),
                bbox=None if bbox is None else np.asarray(bbox),
                embedding=None if emb is None else np.asarray(emb)))
        return out

    def nudity(self, img_bgr):
        """Run NudeNet on a BGR ndarray.

        @return list of {'class': str, 'score': float, ...} dicts (NudeNet's
                own result shape).  Empty when RUN_NUDITY is off."""
        resp = self._request({'op': 'nudity',
                              'img': np.ascontiguousarray(img_bgr)})
        return resp.get('dets', [])
