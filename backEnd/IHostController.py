"""
IHostController.py

Stateful controller that turns iHost / eWeLink CUBE devices on in response to
rule triggers and auto-offs them after a period of no motion -- the native port
of the external CubeScript_v01.py Flask relay.

A single instance lives on the long-lived ResponseRunner process (per-rule
response objects are rebuilt on every rule reload, so they cannot hold this
state).  Global hub IP / token / location come from IHostConfig, read fresh on
each trigger so Options changes take effect without a restart.

Behaviour parity with CubeScript_v01.py:
  * 'on'    : if not already automating this device, remember whether it was
              already on (manual); if off, turn it on.  Bump last_trigger and arm
              the auto-off timer.  Repeated motion keeps re-arming it.
  * auto-off: once `timeout` seconds pass with no new trigger, turn the device
              off -- UNLESS it was already on when automation began (respect a
              manually-on device).  timeout == 0 => latch on, never auto-off.
  * 'off'   : turn off now (one-shot; clears automation state).
  * 'toggle': read current state, set the opposite (one-shot).
  * nightOnly: skip the whole action in daylight (the sunset-offset .. sunrise-
              offset window), using the configured latitude/longitude.

State is in-memory, keyed by device id, so two rules pointing at the same plug
share one automation state.  A process restart forgets armed auto-offs (same as
a CubeScript restart).
"""

import datetime
import math
import threading
import time

import appCommon.IHostClient as IHostClient
import backEnd.IHostConfig as IHostConfig


_kSweepIntervalSecs = 2


class IHostController(object):

    ###########################################################
    def __init__(self, logger):
        self._logger = logger
        self._devices = {}                 # deviceId -> state dict
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitorLoop,
                                        name="iHostMonitor", daemon=True)
        self._thread.start()

    ###########################################################
    def trigger(self, deviceId, deviceName, command, timeout, nightOnly):
        """Handle one rule trigger for a device.  Returns True if it acted."""
        label = deviceName or deviceId
        if not deviceId:
            self._logger.warning("iHost: trigger with no device configured")
            return False

        cfg = IHostConfig.loadConfig()
        ip = cfg.get("ip", "")
        token = cfg.get("token", "")
        if not ip or not token:
            self._logger.warning(
                "iHost: hub IP/token not set (Options -> iHost); ignoring "
                "'%s' for %s" % (command, label))
            return False

        if nightOnly and not self._isNight(cfg):
            self._logger.info("iHost: ignored (daylight) %s -> %s"
                              % (label, command))
            return False

        command = (command or "on").lower()

        if command == "off":
            ok = IHostClient.setPower(ip, token, deviceId, "off")
            with self._lock:
                self._devices.pop(deviceId, None)
            self._logger.info("iHost: OFF %s (%s)"
                              % (label, "ok" if ok else "FAILED"))
            return ok

        if command == "toggle":
            current = IHostClient.getPowerState(ip, token, deviceId)
            newState = "off" if current == "on" else "on"
            ok = IHostClient.setPower(ip, token, deviceId, newState)
            self._logger.info("iHost: TOGGLE %s -> %s (%s)"
                              % (label, newState, "ok" if ok else "FAILED"))
            return ok

        # Default: command == "on" -- the auto-off automation.
        return self._triggerOn(ip, token, deviceId, label, timeout)

    ###########################################################
    def shutdown(self):
        """Stop the sweeper thread (called on ResponseRunner exit)."""
        self._stop.set()

    ###########################################################
    def _triggerOn(self, ip, token, deviceId, label, timeout):
        now = time.time()
        timeoutSecs = int(timeout) if timeout else 0

        # Decide if this starts a new automation cycle (network read is done
        # OUTSIDE the lock so a slow hub can't stall the sweeper).
        with self._lock:
            dev = self._devices.get(deviceId)
            newCycle = dev is None or not dev.get("automation_on")

        wasOn = False
        if newCycle:
            wasOn = (IHostClient.getPowerState(ip, token, deviceId) == "on")

        with self._lock:
            dev = self._devices.get(deviceId)
            if dev is None or not dev.get("automation_on"):
                dev = {"name": label, "was_initially_on": wasOn}
                self._devices[deviceId] = dev
                startedCycle = True
            else:
                startedCycle = False
            dev["last_trigger"] = now
            dev["automation_on"] = True
            dev["timeout"] = timeoutSecs
            dev["ip"] = ip
            dev["token"] = token
            wasInitiallyOn = dev["was_initially_on"]

        if startedCycle and wasInitiallyOn:
            self._logger.info(
                "iHost: %s already on (manual) -- will not auto-off" % label)
        elif startedCycle:
            ok = IHostClient.setPower(ip, token, deviceId, "on")
            self._logger.info("iHost: ON %s (auto-off %ss) (%s)"
                              % (label, timeoutSecs, "ok" if ok else "FAILED"))
        return True

    ###########################################################
    def _monitorLoop(self):
        while not self._stop.is_set():
            now = time.time()
            toOff = []
            with self._lock:
                for deviceId, dev in self._devices.items():
                    if not dev.get("automation_on"):
                        continue
                    timeoutSecs = dev.get("timeout", 0)
                    if timeoutSecs and now - dev["last_trigger"] > timeoutSecs:
                        dev["automation_on"] = False
                        if dev.get("was_initially_on"):
                            self._logger.info(
                                "iHost: skip auto-off (manual) %s"
                                % dev.get("name"))
                        else:
                            toOff.append((deviceId, dict(dev)))
            # Network calls outside the lock.
            for deviceId, dev in toOff:
                ok = IHostClient.setPower(dev["ip"], dev["token"],
                                          deviceId, "off")
                self._logger.info("iHost: AUTO-OFF %s (%s)"
                                  % (dev.get("name"), "ok" if ok else "FAILED"))
            self._stop.wait(_kSweepIntervalSecs)

    ###########################################################
    def _isNight(self, cfg):
        lat = cfg.get("latitude")
        lon = cfg.get("longitude")
        if lat is None or lon is None:
            return True    # no location configured -> don't block the action
        sunrise, sunset = self._sunriseSunsetMinutes(lat, lon)
        if sunrise is None or sunset is None:
            return True    # polar day/night -> don't block
        offset = cfg.get("nightOffsetMinutes", 30)
        lt = time.localtime()
        nowMin = lt.tm_hour * 60 + lt.tm_min
        # Night = before (sunrise - offset) or after (sunset - offset), matching
        # CubeScript_v01.py is_night().
        return nowMin < (sunrise - offset) or nowMin > (sunset - offset)

    ###########################################################
    @staticmethod
    def _sunriseSunsetMinutes(lat, lon):
        """(sunrise, sunset) minutes from local midnight; (None, None) at poles.

        Simplified NOAA solar algorithm, ported from
        backEnd.RealTimeRule._calcSunriseSunsetMinutes (copied so this module
        stays importable in the response process without pulling in the heavy
        RealTimeRule dependency chain).
        """
        N = datetime.date.today().timetuple().tm_yday
        Lsun = (280.460 + 0.9856474 * (N - 1)) % 360
        g = math.radians((357.528 + 0.9856003 * (N - 1)) % 360)
        lam = math.radians(
            (Lsun + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
        sin_dec = math.sin(math.radians(23.439)) * math.sin(lam)
        dec = math.asin(sin_dec)
        f = math.radians((279.575 + 0.9856474 * (N - 1)) % 360)
        EqT = (-104.0 * math.sin(f) + 596.0 * math.cos(f)
               - 4.0 * math.sin(2 * f) + 0.5 * math.cos(2 * f)
               + 0.2 * math.sin(3 * f) + 0.8 * math.cos(3 * f)) / 60.0
        cos_ha = (math.sin(math.radians(-0.833))
                  - math.sin(math.radians(lat)) * sin_dec) / \
                 (math.cos(math.radians(lat)) * math.cos(dec))
        if cos_ha < -1 or cos_ha > 1:
            return None, None
        ha_deg = math.degrees(math.acos(cos_ha))
        if time.daylight and time.localtime().tm_isdst:
            tz_hours = -time.altzone / 3600.0
        else:
            tz_hours = -time.timezone / 3600.0
        noon_min = 720.0 - 4.0 * lon - EqT + tz_hours * 60.0
        return int(noon_min - ha_deg * 4.0), int(noon_min + ha_deg * 4.0)
