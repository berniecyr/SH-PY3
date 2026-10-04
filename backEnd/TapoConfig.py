"""
TapoConfig.py

Single source of truth for the global TP-Link Tapo control credentials
(tapo_config.json).  Both the frontend Options dialog (which writes it) and the
backend ResponseRunner (which reads it when a rule fires the siren or the
spotlight) import from here, so the account can never drift between the manual
buttons on the monitor screen and the rule actions.

These are the TP-LINK ACCOUNT credentials -- the email address and password used
to sign in to the Tapo app -- NOT the camera account in a stream URI.  That
camera account streams RTSP but is refused by the cameras' control API on port
443.  See vitaToolbox/networking/TapoControl.py.

Why a file and not a preference: rule actions execute in the ResponseRunner, a
separate process with no access to the front end's 'Gui Prefs.pkl'.

Pure stdlib only -- safe to import from either side without dragging in wx /
requests / ML packages.  Mirrors the shape of IHostConfig.py.
"""

import copy
import json
import os


# Data dir / config path -- MUST match the convention the rest of the app uses
# (see IHostConfig.py) so the Options dialog writes where the backend reads,
# even in the service-launched case.
def _appDataDir():
    """@return  The data directory (see appCommon.InstallPaths.getUserDataDir)."""
    try:
        from appCommon.InstallPaths import getUserDataDir
        return getUserDataDir()
    except Exception:
        return os.path.join(os.path.expanduser('~'), 'AppData', 'Local',
                            'Sighthound Video Py3')


_kAppDataDir = _appDataDir()
_kConfigFileName = 'tapo_config.json'


# Canonical defaults.  No useful default for either: the account is the user's
# own TP-Link sign-in email.
DEFAULTS = {
    "user":     "",
    "password": "",
}


def getConfigPath():
    """Return the absolute path to tapo_config.json (env-var overridable)."""
    return os.environ.get("TAPO_CONFIG", "") or \
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


def getCredentials():
    """The account to control cameras with.

    @return creds  (user, password); either may be "" if not configured yet.
    """
    cfg = loadConfig()
    return cfg.get("user", ""), cfg.get("password", "")


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
