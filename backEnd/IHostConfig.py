"""
IHostConfig.py

Single source of truth for the global iHost / eWeLink CUBE settings
(ihost_config.json).  Both the frontend Options dialog (which writes it) and the
backend IHostController (which reads it at trigger time) import from here so the
hub IP/token, the geographic location used for the night-only gate, and the
cached device list can never drift apart.

Pure stdlib only -- safe to import from either side without dragging in wx /
requests / ML packages.  Mirrors the shape of ImageCheckConfig.py.
"""

import copy
import json
import os


# Data dir / config path -- MUST match the convention the rest of the app uses
# (see ImageCheckConfig.py) so the Options dialog writes where the backend
# reads, even in the service-launched case.
def _appDataDir():
    """@return  The data directory (see appCommon.InstallPaths.getUserDataDir)."""
    try:
        from appCommon.InstallPaths import getUserDataDir
        return getUserDataDir()
    except Exception:
        return os.path.join(os.path.expanduser('~'), 'AppData', 'Local',
                            'Sighthound Video Py3')


_kAppDataDir = _appDataDir()
_kConfigFileName = 'ihost_config.json'


# Canonical defaults.  latitude/longitude seed the night-only gate; they default
# to the location the user's CubeScript_v01.py used (Santo Domingo) so night-only
# works out of the box and can be edited in Options -> iHost.  `devices` is the
# cached [{"name","id"}] list populated by the "Refresh devices" button.
DEFAULTS = {
    "ip":                 "",
    "token":              "",
    "latitude":           18.4861,
    "longitude":          -69.9312,
    "nightOffsetMinutes": 30,      # night starts this many minutes before sunset
                                   # and ends this many before sunrise (matches
                                   # CubeScript_v01.py is_night()'s -30min window)
    "devices":            [],
}


def getConfigPath():
    """Return the absolute path to ihost_config.json (env-var overridable)."""
    return os.environ.get("IHOST_CONFIG", "") or \
        os.path.join(_kAppDataDir, _kConfigFileName)


def mergeDefaults(userCfg):
    """Return DEFAULTS shallow-merged with userCfg (userCfg wins per key).

    @param  userCfg  Dict read from disk (may be partial), or None.
    @return cfg      Full config dict safe to read every key from.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if isinstance(userCfg, dict):
        for k, v in userCfg.items():
            cfg[k] = v
    return cfg


def loadConfig():
    """Load the config merged over DEFAULTS.  Never raises."""
    path = getConfigPath()
    try:
        with open(path) as f:
            return mergeDefaults(json.load(f))
    except Exception:
        return copy.deepcopy(DEFAULTS)


def saveConfig(cfg):
    """Write cfg as pretty JSON.  Returns True on success (never raises)."""
    path = getConfigPath()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def ensureDefaults():
    """Write the default config if none exists yet.  No-op otherwise.

    Called when a brand-new database is created so a fresh install starts with
    the config file present (UI and backend agree from the first run).

    @return wrote  True if defaults were written, False otherwise.
    """
    path = getConfigPath()
    if os.path.isfile(path):
        return False
    return saveConfig(copy.deepcopy(DEFAULTS))
