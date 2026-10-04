"""
IHostClient.py

Low-level client for the eWeLink CUBE / iHost Open API v2.

Talks directly to the hub's LAN REST API (no cloud, no external relay).  Shared
by the front-end (Options "Refresh devices" button, rule-editor device picker)
and the back-end (IHostController), so it stays dependency-light: stdlib + the
already-bundled `requests`.

API shape (base http://{ip}/open-api/v2/rest, header Authorization: Bearer {token}):
  * GET  /devices          -> data.device_list[] of {serial_number, name,
                              capabilities[], state.power.powerState}
  * GET  /devices/{id}     -> data.state.power.powerState ("on"/"off")
  * PUT  /devices/{id}     body {"state":{"power":{"powerState":"on"|"off"}}}

The GET/PUT control shape mirrors the user's proven CubeScript_v01.py
(cube_get / cube_set) -- the authoritative dialect for this hardware.  (The
public docs also mention POST /devices/{id}/action; PUT is what works on the
real hub, so that is what we send.)
"""

import socket

import requests


_kDefaultTimeout = 5

# Per-host timeout while sweeping the LAN for a hub.  Short on purpose: a hub
# on the same subnet answers in milliseconds, and everything else is either
# silent or refuses the connection immediately.
_kProbeTimeout = 0.6

# The documented default hostname (http://ihost.local).  Windows resolves
# .local names over mDNS itself, so this needs no extra dependency and usually
# finds the hub before the sweep starts.
_kHostHints = ("ihost.local", "ihost")

# Only sweep networks at least this large a mask -- a /24 is 254 probes, which
# is quick; anything broader is a different kind of operation and we skip it
# rather than hammer a corporate range.
_kMinSweepPrefix = 23


def _base(ip):
    return "http://%s/open-api/v2/rest" % ip


def _headers(token, withJson=False):
    h = {"Authorization": "Bearer %s" % token}
    if withJson:
        h["Content-Type"] = "application/json"
    return h


def getBridgeInfo(ip, timeout=_kDefaultTimeout):
    """Identify the hub at `ip`.  No token needed -- this is how we recognise
    one during discovery, and how "is this address right?" gets answered.

    @return info  {'ip', 'mac', 'domain', 'fw_version', 'name'}
    @raise  Exception if the address isn't a hub or can't be reached.
    """
    r = requests.get("%s/bridge" % _base(ip), timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error", 0) not in (0, None):
        raise RuntimeError("iHost API error %s: %s" %
                           (payload.get("error"), payload.get("message", "")))
    data = payload.get("data") or {}
    if not data.get("mac") and not data.get("name"):
        raise RuntimeError("not an iHost bridge")
    return data


def requestAccessToken(ip, timeout=_kDefaultTimeout):
    """Ask the hub for an API token.

    The hub will not hand one out until somebody physically confirms it: the
    first call comes back `401 link button not pressed`, the hub's web console
    then shows a pop-up, and only after the user presses Done does a call
    return the token.  The confirmation window is about five minutes, so the
    caller is expected to poll.

    @return (token, message)  token is None until the user has confirmed;
                              message is the hub's own wording, worth showing.
    @raise  Exception only if the hub can't be reached at all.
    """
    r = requests.get("%s/bridge/access_token" % _base(ip), timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    err = payload.get("error", 0)
    token = (payload.get("data") or {}).get("token")
    if err in (0, None) and token:
        return token, payload.get("message", "success")
    return None, payload.get("message", "waiting for confirmation")


def _sweepTargets():
    """IPv4 addresses worth probing on this machine's own subnets."""
    try:
        import ipaddress

        import netifaces
    except Exception:
        return []

    targets = []
    seen = set()
    for iface in netifaces.interfaces():
        try:
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET) or []
        except Exception:
            continue
        for a in addrs:
            addr, mask = a.get("addr"), a.get("netmask")
            if not addr or not mask or addr.startswith("127."):
                continue
            try:
                net = ipaddress.IPv4Network("%s/%s" % (addr, mask),
                                            strict=False)
            except Exception:
                continue
            if net.prefixlen < _kMinSweepPrefix or net.num_addresses <= 2:
                continue
            for host in net.hosts():
                s = str(host)
                if s != addr and s not in seen:
                    seen.add(s)
                    targets.append(s)
    return targets


def discoverHubs(progressFn=None, cancelFn=None, timeout=_kProbeTimeout):
    """Find eWeLink CUBE / iHost hubs on the LAN.

    Tries the documented default hostname first, then sweeps this machine's own
    subnets, asking each address to identify itself via /bridge.

    @param  progressFn  Optional callable(done, total) for a UI.
    @param  cancelFn    Optional callable returning True to stop early.
    @return hubs        List of {'ip', 'name', 'mac', 'fw_version'}, best
                        (hostname-resolved) first.  Never raises.
    """
    from concurrent.futures import ThreadPoolExecutor

    hubs = []
    seenIps = set()

    def _consider(host):
        try:
            info = getBridgeInfo(host, timeout=max(timeout, 2.0))
        except Exception:
            return None
        ip = info.get("ip") or host
        if ip in seenIps:
            return None
        seenIps.add(ip)
        hubs.append({"ip": ip,
                     "name": info.get("name", "iHost"),
                     "mac": info.get("mac", ""),
                     "fw_version": info.get("fw_version", "")})
        return ip

    for hint in _kHostHints:
        if cancelFn and cancelFn():
            return hubs
        try:
            # Addresses may carry a port (host:8081); resolve the name only.
            socket.gethostbyname(hint.rsplit(":", 1)[0]
                                 if hint.count(":") == 1 else hint)
        except Exception:
            continue
        _consider(hint)
    if hubs:
        return hubs

    targets = _sweepTargets()
    total = len(targets) or 1
    done = 0
    if progressFn:
        progressFn(0, total)
    with ThreadPoolExecutor(max_workers=64) as pool:
        for _ in pool.map(_probeOne(timeout, hubs, seenIps), targets):
            done += 1
            if progressFn and (done % 8 == 0 or done == total):
                progressFn(done, total)
            if cancelFn and cancelFn():
                break
    return hubs


def _probeOne(timeout, hubs, seenIps):
    """Build the per-address probe used by the sweep."""
    def probe(host):
        try:
            info = getBridgeInfo(host, timeout=timeout)
        except Exception:
            return None
        ip = info.get("ip") or host
        if ip not in seenIps:
            seenIps.add(ip)
            hubs.append({"ip": ip,
                         "name": info.get("name", "iHost"),
                         "mac": info.get("mac", ""),
                         "fw_version": info.get("fw_version", "")})
        return ip
    return probe


def listDevices(ip, token, timeout=_kDefaultTimeout):
    """Return the hub's devices as a list of simple dicts.

    @param  ip       Hub IP / host.
    @param  token    Bearer token.
    @return devices  List of {'name', 'id', 'powerState', 'capabilities'};
                     empty list if the hub reports no devices.
    @raise  Exception on connection / HTTP / API-level failure so the caller
            (the Options refresh button) can surface a meaningful message.
    """
    url = "%s/devices" % _base(ip)
    r = requests.get(url, headers=_headers(token), timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error", 0) not in (0, None):
        raise RuntimeError("iHost API error %s: %s" %
                           (payload.get("error"), payload.get("message", "")))

    deviceList = (payload.get("data") or {}).get("device_list") or []
    out = []
    for d in deviceList:
        power = (d.get("state") or {}).get("power") or {}
        out.append({
            "name":         d.get("name", ""),
            "id":           d.get("serial_number", ""),
            "powerState":   power.get("powerState"),
            "capabilities": [c.get("capability")
                             for c in (d.get("capabilities") or [])],
        })
    return out


def getPowerState(ip, token, deviceId, timeout=_kDefaultTimeout):
    """Return a device's power state ('on'/'off'), or None if unavailable.

    Mirrors CubeScript_v01.py cube_get.  Never raises -- returns None on any
    failure so callers can degrade gracefully.
    """
    url = "%s/devices/%s" % (_base(ip), deviceId)
    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data") or {}
        return ((data.get("state") or {}).get("power") or {}).get("powerState")
    except Exception:
        return None


def setPower(ip, token, deviceId, state, timeout=_kDefaultTimeout):
    """Set a device's power to 'on' or 'off'.

    Mirrors CubeScript_v01.py cube_set: PUT /devices/{id} with body
    {"state":{"power":{"powerState":state}}}.

    @return ok  True on success, False on any failure (never raises).
    """
    url = "%s/devices/%s" % (_base(ip), deviceId)
    body = {"state": {"power": {"powerState": state}}}
    try:
        r = requests.put(url, headers=_headers(token, withJson=True),
                         json=body, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json().get("error", 0) in (0, None)
        except Exception:
            return True   # 2xx with a non-JSON body: assume success
    except Exception:
        return False
