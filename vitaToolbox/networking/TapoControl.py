#!/usr/bin/env python

#*****************************************************************************
#
# TapoControl.py
#   Fire the siren and switch the white spotlight on TP-Link Tapo cameras via
#   their local HTTPS control API on port 443, using the pytapo library.
#
#   Everything runs on ONE daemon worker thread.  pytapo bridges its async
#   transport through a persistent event loop guarded by an RLock
#   (pytapo/asyncHandler.py), so keeping every call and every cached session on
#   a single thread sidesteps cross-thread loop reuse entirely -- and it
#   serialises impatient double-clicking for free.  Callers hand in a callback
#   and get the answer back ON THAT WORKER THREAD; a wx caller is responsible
#   for marshalling to the UI thread itself (wx.CallAfter).
#
#   NOTE ON CREDENTIALS: the camera account that works for RTSP is NOT
#   accepted here.  Cameras running the encrypted login (encrypt_type 3)
#   validate the client against the TP-LINK ACCOUNT -- the email address and
#   password used to sign in to the Tapo app.  A camera account fails the
#   device_confirm check and pytapo reports "Invalid authentication data".
#   Verified on a C320WS (firmware 1.9.1): camera account refused, TP-Link
#   account accepted.  parseTapoTarget() therefore takes a user/password
#   override, which the Options dialog's Tapo tab fills in; use isAuthError()
#   on a failure message to tell "wrong credentials" from "unreachable".
#
#   Repeated rejected logins earn a "Temporary Suspension: Try again in 1800
#   seconds" lockout from the camera, so do not retry a failed login in a loop.
#
#   No wx in here -- this package is UI-agnostic (see Onvif.py, Upnp.py).
#
#*****************************************************************************

import socket
import threading
import time
import urllib.parse

from collections import deque

from vitaToolbox.loggingUtils.LoggingUtils import EmptyLogger


# Operations, as passed to TapoController.submit().
kOpSirenOn    = 'sirenOn'
kOpSirenOff   = 'sirenOff'
kOpLightOn    = 'lightOn'
kOpLightOff   = 'lightOff'
kOpLightState = 'lightState'
kOpProbe      = 'probe'

# The camera's local control API.  Also pytapo's default; passed explicitly
# because it is a requirement here, not an incidental default.
kControlPort = 443

# Told to the user when pytapo isn't installed.  It is imported lazily inside
# the worker so a missing package can never stop the app launching.
kNoPytapoMsg = ("The pytapo library is not installed, so camera siren and "
                "light control is unavailable.\n\n"
                "Install it with:  pip install pytapo")

# pytapo raises bare Exceptions with this text for every credential rejection.
_kAuthErrorText = "invalid authentication data"

# How long to wait for the control port to accept a connection before giving
# up.  The worker is serialised, so one unreachable camera must not be allowed
# to sit on the queue while the user is clicking at a different one.
_kConnectTimeoutSecs = 2.0

# How long a refused connection suppresses further SPECULATIVE work against the
# same host.  Selecting a camera fires a background spotlight-state query, and a
# camera that isn't there costs the whole timeout above every time -- which a
# placeholder entry pointing at an address with no camera behind it does on
# every single selection.  Anything the user actually asked for ignores this and
# always tries, so a camera that has just come back is never refused.
_kUnreachableTtlSecs = 60.0

_kLoopbackHosts = ('localhost', '127.0.0.1', '::1')


##############################################################################
def parseTapoTarget(camUri, controlUser=None, controlPassword=None,
                    requireUriAuth=False):
    """Work out what to talk to for a camera, given its stream URI.

    The host always comes from the URI.  The credentials come from the
    override when one is supplied, and otherwise from the URI's auth part --
    i.e. the same camera account the RTSP stream uses.

    requireUriAuth additionally demands that the URI carry an account of its
    own, whether or not the override is used.  A URI with no auth part at all
    was never a configured camera -- it is a placeholder someone typed an
    address into -- and the override cannot conjure a camera that was never
    set up: it supplies the CONTROL account, not the camera.  It is off by
    default because the two callers that build a URI from a bare host
    (ResponseRunner's rule action, and tapoHostFromUri below) come here only
    for the scheme/loopback/hostname checks and would fail it every time.
    Turn it on for work that merely SELECTING a camera triggers, where an
    entry pointing at nothing costs a connect timeout nobody asked to pay.

    @param  camUri           The camera's stream URI, as stored in the camera db.
    @param  controlUser      Username to use instead of the URI's, or None.
    @param  controlPassword  Password to use instead of the URI's, or None.
    @param  requireUriAuth   True to refuse a URI that carries no account.
    @return target           (host, user, password), or None if this URI can't
                             name a controllable camera.
    """
    try:
        splitResult = urllib.parse.urlsplit(camUri)
    except ValueError:
        return None

    if splitResult.scheme not in ('rtsp', 'rtsps'):
        # Webcams, UPnP and ONVIF URIs all fall out here.
        return None

    try:
        host = splitResult.hostname
    except ValueError:
        return None

    if not host or host.lower() in _kLoopbackHosts:
        # A local test stream is not a camera we can shout through.
        return None

    # urlsplit leaves these percent-encoded; the camera wants them raw.  Same
    # treatment CameraSetupWizard gives the auth part it reads back.
    user = splitResult.username
    password = splitResult.password

    if requireUriAuth and (not user or not password):
        # Read BEFORE the override is consulted, on purpose: the override says
        # which account controls the camera, not whether there is a camera.
        return None

    if controlUser and controlPassword:
        return (host, controlUser, controlPassword)

    if not user or not password:
        return None

    return (host,
            urllib.parse.unquote(user),
            urllib.parse.unquote(password))


##############################################################################
def tapoHostFromUri(camUri):
    """The address to control, for a camera whose stream URI we have.

    For callers that supply their own credentials and only need to know the
    URI names a real camera on the network -- the Options dialog's test button
    and the rule action, neither of which uses the URI's own account.

    @param  camUri  The camera's stream URI, as stored in the camera db.
    @return host    The hostname, or None if this URI can't name one.
    """
    target = parseTapoTarget(camUri, "x", "x")
    return target[0] if target is not None else None


##############################################################################
def isAuthError(errorMsg):
    """Whether a failure message means "the camera rejected these credentials".

    Worth distinguishing: an auth failure is fixed by supplying different
    credentials, everything else by fixing the camera or the network.

    @param  errorMsg  The message handed to a submit() callback.
    @return isAuth    True if the camera rejected the credentials.
    """
    return _kAuthErrorText in (errorMsg or "").lower()


##############################################################################
class _TapoJob(object):
    """One queued operation."""

    ###########################################################
    def __init__(self, target, op, callback, coalesceKey, speculative):
        self.target = target
        self.op = op
        self.callback = callback
        self.coalesceKey = coalesceKey
        self.speculative = speculative


##############################################################################
class TapoController(object):
    """Serialised access to Tapo cameras' local control API.

    Use TapoController.instance(); the worker thread and the session cache are
    process-wide, and there is no value in more than one of either.
    """

    _instance = None
    _instanceLock = threading.Lock()

    ###########################################################
    def __init__(self, logger=None):
        """TapoController constructor -- use instance() instead.

        @param  logger  A logger, or None for no logging at all.
        """
        self._logger = logger if logger is not None else EmptyLogger()

        self._cond = threading.Condition()
        self._jobs = deque()
        self._thread = None

        # host/user/password -> pytapo.Tapo.  ONLY ever touched by the worker
        # thread, which is why it needs no lock.
        self._sessions = {}

        # host -> when its control port last refused a connection.  Same
        # worker-thread-only rule as _sessions.
        self._unreachableSince = {}

    ###########################################################
    @classmethod
    def instance(cls, logger=None):
        """Get the process-wide controller, creating it if needed.

        @param  logger      A logger to give the controller if this call is the
                            one that creates it; ignored afterwards.
        @return controller  The TapoController.
        """
        with cls._instanceLock:
            if cls._instance is None:
                cls._instance = cls(logger)
            return cls._instance

    ###########################################################
    def submit(self, target, op, callback, coalesceKey=None,
               speculative=False):
        """Queue an operation against a camera.

        @param  target       (host, user, password), from parseTapoTarget().
        @param  op           One of the kOp* constants.
        @param  callback     Called on the WORKER THREAD as
                             callback(ok, value): value is the operation's
                             result when ok, else a message fit to show a user.
        @param  coalesceKey  If given, any still-queued job with the same key
                             is dropped first -- for state queries, where only
                             the newest one is worth making.
        @param  speculative  True for background work nobody asked for, which
                             is skipped outright while the host is known
                             unreachable rather than paying the connect
                             timeout again.  Leave False for anything the user
                             actually pressed: those always try.
        """
        with self._cond:
            if coalesceKey is not None:
                self._jobs = deque(job for job in self._jobs
                                   if job.coalesceKey != coalesceKey)
            self._jobs.append(_TapoJob(target, op, callback, coalesceKey,
                                       speculative))

            if self._thread is None:
                self._thread = threading.Thread(target=self._workerLoop,
                                                name="TapoControl",
                                                daemon=True)
                self._thread.start()

            self._cond.notify()

    ###########################################################
    def _workerLoop(self):
        """Run queued operations, one at a time, forever."""
        while True:
            with self._cond:
                while not self._jobs:
                    self._cond.wait()
                job = self._jobs.popleft()

            ok, value = self._runJob(job)

            try:
                job.callback(ok, value)
            except Exception:
                # A caller whose callback throws must not take the worker down
                # with it -- every later press would silently do nothing.
                self._logger.error("Tapo callback failed", exc_info=True)

    ###########################################################
    def _runJob(self, job):
        """Run one operation.

        @param  job    The _TapoJob to run.
        @return ok     True if it worked.
        @return value  The operation's result, or a user-facing message.
        """
        host, user, _ = job.target

        if job.speculative and self._isUnreachable(host):
            # Not worth another connect timeout, and not worth a warning: the
            # one that set this said it all.
            msg = "%s was unreachable moments ago; not retried" % host
            self._logger.debug("Tapo %s skipped: %s" % (job.op, msg))
            return False, msg

        # A WARNING should mean something the user asked for did not happen.
        # Speculative work is background chatter by definition, and a camera
        # entry that points at nothing would otherwise file one every time the
        # Monitor screen is opened -- so it reports at DEBUG, matching the
        # skipped-job path above.  Anything the user pressed keeps its voice.
        # SV_LOG_LEVEL=debug brings the quiet ones back when diagnosing.
        logFailure = self._logger.debug if job.speculative \
                     else self._logger.warning
        logSuccess = self._logger.debug if job.speculative \
                     else self._logger.info

        try:
            tapo = self._getSession(job.target)
            value = _kOps[job.op](tapo)
        except Exception as e:
            # Drop the session so the next press starts a fresh login rather
            # than replaying a stale or half-built one.
            self._sessions.pop(job.target, None)
            msg = str(e) or type(e).__name__
            logFailure("Tapo %s failed on %s (user %s): %s"
                       % (job.op, host, user, msg))
            return False, msg

        logSuccess("Tapo %s on %s -> %s" % (job.op, host, value))
        return True, value

    ###########################################################
    def _isUnreachable(self, host):
        """Whether this host refused a connection recently enough to skip.

        Worker thread only.

        @param  host    The camera address.
        @return recent  True if it failed to connect within the last TTL.
        """
        since = self._unreachableSince.get(host)
        if since is None:
            return False
        if (time.time() - since) < _kUnreachableTtlSecs:
            return True

        # Expired -- forget it, so the next attempt is a real one.
        self._unreachableSince.pop(host, None)
        return False


    ###########################################################
    def _getSession(self, target):
        """Get a logged-in pytapo session for a camera, creating it if needed.

        Keyed on the full credential triple, not just the host: keying on host
        alone would keep using a stale session after the password is changed in
        the camera wizard.

        Worker thread only.

        @param  target  (host, user, password).
        @return tapo    A pytapo.Tapo.
        """
        tapo = self._sessions.get(target)
        if tapo is not None:
            return tapo

        try:
            from pytapo import Tapo
        except ImportError:
            raise Exception(kNoPytapoMsg)

        host, user, password = target

        # Fail fast on a camera that isn't there.  Without this, an offline
        # camera can sit in pytapo's retry/backoff for many seconds while
        # everything queued behind it waits.
        try:
            with socket.create_connection((host, kControlPort),
                                          _kConnectTimeoutSecs):
                pass
        except OSError as e:
            # Note it so speculative work skips this host for a while.  Only
            # the CONNECT is recorded -- a camera that answers and then refuses
            # our credentials is very much reachable, and must keep being tried
            # so a corrected account takes effect immediately.
            self._unreachableSince[host] = time.time()
            raise Exception("Cannot reach %s on port %d: %s"
                            % (host, kControlPort, e))

        self._unreachableSince.pop(host, None)

        self._logger.info("Opening Tapo control session to %s as %s"
                          % (host, user))
        tapo = Tapo(host, user, password, controlPort=kControlPort)

        # Force the login now, so a credential problem is reported by the
        # operation the user actually asked for rather than the next one.
        tapo.getBasicInfo()

        self._sessions[target] = tapo
        return tapo


##############################################################################
def _setSiren(tapo, on):
    """Start or stop the siren, over whichever API this camera implements.

    No single call covers the range, and a camera that lacks the one you pick
    answers -40106 (UNSUPPORTED_METHOD) or -40210 (METHOD_DO_NOT_EXIST), which
    pytapo raises.  Three paths, in the order the Home Assistant integration
    uses them:

      1. startManualAlarm / stopManualAlarm -- the older "do" action.
      2. setSirenStatus -- the newer one.  1 and 2 are both issued, as that
         integration does, so a model that ignores one still hears the other.
      3. testUsrDefAudio -- plays the configured alarm tone directly.  Only
         tried when the first two were refused, because it is a fallback
         rather than a parallel path.

    Measured here on a C320WS (fw 1.9.1) and a C560WS (fw 1.1.10): paths 1 and
    2 are BOTH refused (-40106 UNSUPPORTED_METHOD and METHOD_DO_NOT_EXIST),
    which is what JurajNyiri/HomeAssistant-Tapo-Control issues #678 (C320WS)
    and #713 (C325WB) describe.  Path 3 is what actually sounds these
    cameras, and it does so whether or not the camera's own alarm feature is
    enabled -- so a refusal here is about the model, not about that setting.
    getSirenTypeList is refused too, so the tone comes from getAlarm's
    alarm_type.

    @param  tapo  A logged-in pytapo.Tapo.
    @param  on    True to sound the siren, False to silence it.
    @return value The result of whichever call the camera accepted.
    """
    results = []
    errors = []

    def _try(label, call):
        try:
            results.append(call())
        except Exception as e:
            errors.append("%s: %s" % (label, str(e) or type(e).__name__))

    if on:
        _try("startManualAlarm", tapo.startManualAlarm)
    else:
        _try("stopManualAlarm", tapo.stopManualAlarm)
    _try("setSirenStatus", lambda: tapo.setSirenStatus(on))

    if not results:
        toneId = _alarmToneId(tapo)
        _try("testUsrDefAudio", lambda: tapo.testUsrDefAudio(toneId, on))

    if results:
        return results[0]

    # Nothing took.  Report every distinct reason, so "this model has no
    # manual siren" reads differently from a network fault.
    raise Exception("; ".join(dict.fromkeys(errors)))


##############################################################################
def _alarmToneId(tapo):
    """The tone to play for a manual siren, for the testUsrDefAudio path.

    @param  tapo    A logged-in pytapo.Tapo.
    @return toneId  The camera's configured alarm type, or 0 if it won't say.
    """
    try:
        return int((tapo.getAlarm() or {}).get('alarm_type', 0) or 0)
    except Exception:
        return 0


##############################################################################
def _lightState(tapo):
    """Whether the camera's white spotlight is currently on.

    @param  tapo  A logged-in pytapo.Tapo.
    @return isOn  True if the lamp is lit.
    """
    try:
        status = (tapo.getWhitelampStatus() or {}).get('status', 0)
        return int(status) == 1
    except Exception:
        # Older models expose the lamp as an image switch instead.
        return bool(tapo.getForceWhitelampState())


##############################################################################
def _setLight(tapo, on):
    """Switch the camera's white spotlight on or off.

    The C320WS (and the rest of this generation) refuses force_wtl_state --
    "Switch force_wtl_state is not supported by this camera" -- and exposes
    the lamp as a TOGGLE instead, so the state has to be read first and
    reversed only when it doesn't already match.  Older models that have the
    switch keep working through the fallback.

    NOTE: the camera runs its own timer on the lamp (rest_time, 300s when
    measured here) and switches it off by itself.  Our idea of "on" therefore
    goes stale, which is why the light state is re-read whenever a camera is
    selected rather than trusted indefinitely.

    @param  tapo  A logged-in pytapo.Tapo.
    @param  on    True to light the lamp.
    @return isOn  The state we left it in.
    """
    try:
        current = _lightState(tapo)
    except Exception:
        current = None

    if current is None:
        # Couldn't read it, so we can't toggle safely -- set it outright.
        tapo.setForceWhitelampState(on)
        return on

    if current != on:
        try:
            tapo.reverseWhitelampStatus()
        except Exception:
            tapo.setForceWhitelampState(on)
    return on


##############################################################################
def _describeCamera(tapo):
    """Name the camera we just logged in to, for a credentials test.

    Getting this far is the point -- _getSession has already had to complete a
    login -- so the return value is only there to show the user WHICH camera
    answered.

    @param  tapo  A logged-in pytapo.Tapo.
    @return desc  Model and firmware, as best the camera reports them.
    """
    info = tapo.getBasicInfo() or {}
    basic = info.get('device_info', {}).get('basic_info', info)
    parts = [basic.get('device_model'), basic.get('sw_version')]
    return " ".join(p for p in parts if p) or "camera"


##############################################################################
# What each kOp* actually does on the camera.  Kept out of the class so the
# operation set reads as one table.
_kOps = {
    kOpSirenOn:    lambda tapo: _setSiren(tapo, True),
    kOpSirenOff:   lambda tapo: _setSiren(tapo, False),
    kOpLightOn:    lambda tapo: _setLight(tapo, True),
    kOpLightOff:   lambda tapo: _setLight(tapo, False),
    kOpLightState: _lightState,
    kOpProbe:      _describeCamera,
}
