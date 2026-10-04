"""
ImageCheckConfig.py

Single source of truth for the ImageCheck AI-detection configuration
(imagecheck_config.json).  Both the backend detector
(ObjectDetectorClientImageCheck) and the frontend Options dialog import from
here so the defaults, the on-disk path, and the load/save semantics can never
drift apart.

Pure stdlib only — safe to import from either the backend or the frontend
without dragging in numpy / wx / ML packages.

The config controls the per-crop detection pipeline:
  * YOLO_CONF_THRESHOLD      base YOLO detection floor (person/vehicle/animal)
  * PERSON_CONF_FOR_ATTRS    min person confidence before the expensive
                             face + nudity stage runs at all (precision/perf gate)
  * FACE_DET_CONF            min face *detection* score to trust a region as a
                             face ("is it a face?")
  * FACEMATCH_CONF           min recognition similarity for a positive ID
                             ("is it this person?")
  * NUDE_THRESHOLDS          per-class NudeNet score floors
  * NUDE_ENABLED             which NudeNet classes are detected at all
"""

import copy
import json
import os


# ---------------------------------------------------------------------------
# Data dir / config path.
#
# NOTE: this MUST match the path the detector reads from.  Several backend
# modules (ObjectDetectorClientImageCheck, EnrollFaces, TtsManager) resolve it
# the same way, through appCommon.InstallPaths — that guarantees the Options
# dialog writes where the detector reads, even in the service-launched case,
# where "~" is the service account's profile rather than the user's.
# ---------------------------------------------------------------------------
def _appDataDir():
    """@return  The data directory (see appCommon.InstallPaths.getUserDataDir)."""
    try:
        from appCommon.InstallPaths import getUserDataDir
        return getUserDataDir()
    except Exception:
        return os.path.join(os.path.expanduser('~'), 'AppData', 'Local',
                            'Sighthound Video Py3')


_kAppDataDir = _appDataDir()
_kConfigFileName = 'imagecheck_config.json'


# ---------------------------------------------------------------------------
# Canonical defaults — the "global baseline thresholds".
# ---------------------------------------------------------------------------
DEFAULTS = {
    # Bumped when a default changes in a way that existing configs must follow
    # rather than keep overriding.  See migrateConfig().
    "CONFIG_VERSION":        1,
    # A bare FILE NAME from YOLO_MODELS, never a path: this config outlives
    # moves between a source checkout and an installed build, which keep the
    # weights in different places, and the front end that writes it may be a
    # different account from the back end that reads it.  Resolved to an
    # absolute path at load time (resolveYoloModelPath).
    "YOLO_MODEL":            "yolo26s.pt",
    "YOLO_CONF_THRESHOLD":   0.25,   # base detection floor
    "PERSON_CONF_FOR_ATTRS": 0.50,   # gate face/nudity behind a confident person
    "RUN_FACE":              True,
    "FACE_DET_CONF":         0.60,   # min face-detection score to trust a face
    "FACEMATCH_CONF":        0.32,   # min recognition similarity for a positive ID
                                     # (measured on production footage: true
                                     # matches 0.33-0.44, strangers ~0.1-0.25)
    "KNOWN_FACES_DAT":       os.path.join(_kAppDataDir, 'known_faces.dat'),
    "MIN_FACE_SIZE":         20,
    "RUN_NUDITY":            False,
    # Which NudeNet weights to run — a bare file name from NUDE_MODELS, for the
    # same reasons as YOLO_MODEL above.
    "NUDE_MODEL":            "320n.onnx",
    # Analyse face/nudity on the camera's FULL-RESOLUTION snapshot (written
    # continuously by the recorder from main-stream keyframes) instead of the
    # small analysis-stream crop.  Restores the recognition range lost when
    # analysis moved to the substream; automatically falls back to the
    # analysis-stream crop whenever the snapshot is missing or stale.
    "FULLRES_ATTRS":         True,
    # Keep sampling person objects with the detector AFTER their type vote
    # completes (throttled, ~1/s per object) so a face recognized late in the
    # track (person walking closer) still updates the object's attributes —
    # required for reliable "Faces" rules.  False restores legacy behavior
    # (detection stops once the object is reported).
    "FACE_TRACK_UPDATES":    True,
    # How long (ms) a track must be missing from the tracker's output before
    # the type vote treats it as lost and force-decides.  0 = legacy (the first
    # frame without the object).  See _kTrackLostGraceMs in
    # QueuedDataManagerCloud.
    "TRACK_LOST_GRACE_MS":   2000,
    # Morphological close applied to the motion mask before blobs are cut, in
    # px at VideoPipeline._kThresholdRefSize (1280x720).  Joins the fragments a
    # person breaks into against a busy background.  0 = off.  A camera's
    # extra['motionClosePx'] overrides this.
    "MOTION_CLOSE_PX":       0,
    # Quality floors for enrolling faces from footage ("add face to baseline").
    # Crops below these are rejected server-side and never offered/saved:
    # ENROLL_MIN_DET     = min face-detection score (junk crops score low),
    # ENROLL_MIN_FACE_PX = min face box size in pixels (smaller = too little
    #                      detail to help recognition; a 1.4 KB thumbnail once
    #                      polluted the baseline this way).
    "ENROLL_MIN_DET":        0.65,
    "ENROLL_MIN_FACE_PX":    64,
    "NUDE_THRESHOLDS": {
        "FEMALE_BREAST_EXPOSED":    0.35,
        "MALE_BREAST_EXPOSED":      0.35,
        "FEMALE_GENITALIA_EXPOSED": 0.70,
        "MALE_GENITALIA_EXPOSED":   0.20,
        "BUTTOCKS_EXPOSED":         0.40,
    },
    # Which NudeNet classes are detected at all.  None of these fire unless the
    # class is listed here AND its score clears NUDE_THRESHOLDS.  Default: all on.
    "NUDE_ENABLED": [
        "FEMALE_BREAST_EXPOSED",
        "MALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "BUTTOCKS_EXPOSED",
    ],
}


# Ordered (class_key, friendly_label) pairs for UI rendering.  Keeping the
# order here means the Options dialog and any future UI render consistently.
NUDE_CATEGORIES = [
    ("FEMALE_BREAST_EXPOSED",    "Female breast exposed"),
    ("MALE_BREAST_EXPOSED",      "Male chest exposed"),
    ("FEMALE_GENITALIA_EXPOSED", "Female genitalia exposed"),
    ("MALE_GENITALIA_EXPOSED",   "Male genitalia exposed"),
    ("BUTTOCKS_EXPOSED",         "Buttocks exposed"),
]


# ---------------------------------------------------------------------------
# Selectable models.
#
# Entry [0] of each list is the default AND the fallback for an unrecognised
# config value, so DEFAULTS must always name it.  Reordering either list
# changes what a fresh install runs — that is what the checks in the tests
# guard against.
# ---------------------------------------------------------------------------

# Ordered (file_name, friendly_label) pairs for the object-detection picker.
# All four run at the same 640 px inference size; they differ in capacity.
# yolo26* are NMS-free (end2end) and cost the same FLOPs as the yolo11* of the
# same scale, so the 11s are here for comparison rather than for economy.
YOLO_MODELS = [
    ("yolo26s.pt", "Balanced — YOLO26 small (recommended)"),
    ("yolo26n.pt", "Fastest — YOLO26 nano"),
    ("yolo11s.pt", "YOLO11 small"),
    ("yolo11n.pt", "YOLO11 nano"),
]

# Ordered (file_name, friendly_label, inference_resolution) for the nudity
# picker.  Both nets are the SAME graph — input 'images' [b,3,h,w], output
# 'output0' [b,22,n], the same 18 classes in the same order — and differ only
# in the imgsz they were trained at.  So switching is a straight file swap plus
# the matching preprocessing size, and NUDE_THRESHOLDS stay meaningful across
# both (though 640 fires on smaller subjects, so they may want re-tuning).
NUDE_MODELS = [
    ("320n.onnx", "Fast (320 px) — default", 320),
    ("640m.onnx", "Accurate (640 px) — slower, more GPU memory", 640),
]


def yoloModelEntry(name):
    """Look a model up in YOLO_MODELS.

    @param  name  File name from the config, e.g. "yolo26s.pt".
    @return       The (name, label) tuple, or entry [0] when name is unknown
                  (an old or hand-edited config).
    """
    for entry in YOLO_MODELS:
        if entry[0].lower() == str(name or "").lower():
            return entry
    return YOLO_MODELS[0]


def nudeModelEntry(name):
    """Look a model up in NUDE_MODELS.

    @param  name  File name from the config, e.g. "640m.onnx".
    @return       The (name, label, resolution) tuple, or entry [0] when name
                  is unknown.
    """
    for entry in NUDE_MODELS:
        if entry[0].lower() == str(name or "").lower():
            return entry
    return NUDE_MODELS[0]


def nudeModelResolution(name):
    """@return  The square inference resolution for a NUDE_MODEL name.

    A table rather than parsing digits out of the file name: the resolution is
    a property of how the net was TRAINED, and a table makes adding a model a
    reviewed one-line change.
    """
    return nudeModelEntry(name)[2]


def resolveYoloModelPath(name):
    """Absolute path of the YOLO weights for a name, or None if not installed.

    Guarded like _appDataDir(): this module promises stdlib-only imports, and
    the front end calls it just to mark a model it does not have.
    """
    try:
        from appCommon.InstallPaths import getYoloModelPath
        return getYoloModelPath(name)
    except Exception:
        return None


def resolveNudeModelPath(name):
    """Absolute path of the NudeNet weights for a name, or None if absent."""
    try:
        from appCommon.InstallPaths import getNudeNetModelPath
        return getNudeNetModelPath(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def getConfigPath():
    """Return the absolute path to imagecheck_config.json (env-var overridable)."""
    return os.environ.get("IMAGECHECK_CONFIG", "") or \
        os.path.join(_kAppDataDir, _kConfigFileName)


# ---------------------------------------------------------------------------
# Load / merge / save
# ---------------------------------------------------------------------------
def mergeDefaults(userCfg):
    """Return DEFAULTS deep-merged with userCfg (userCfg wins).

    NUDE_THRESHOLDS is merged per-class so a partial user dict still gets
    defaults for any class it omits.

    @param  userCfg  Dict read from disk (may be partial), or None.
    @return cfg      Full config dict safe to read every key from.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(userCfg, dict):
        return cfg

    for k, v in userCfg.items():
        if k == "NUDE_THRESHOLDS" and isinstance(v, dict):
            merged = dict(cfg["NUDE_THRESHOLDS"])
            for ck, cv in v.items():
                merged[ck.upper()] = cv
            cfg["NUDE_THRESHOLDS"] = merged
        else:
            cfg[k] = v
    return cfg


def loadConfig():
    """Load the config from disk merged over DEFAULTS.

    Never raises — returns a full copy of DEFAULTS if the file is missing or
    unreadable.

    @return cfg  Full config dict.
    """
    path = getConfigPath()
    try:
        with open(path) as f:
            return mergeDefaults(json.load(f))
    except Exception:
        return copy.deepcopy(DEFAULTS)


def saveConfig(cfg):
    """Write cfg to the config path as pretty JSON.

    @param  cfg  Dict to persist.
    @return ok   True on success, False on failure (never raises).
    """
    path = getConfigPath()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def ensureDefaults():
    """Write the full default config if no config file exists yet.

    Called when a brand-new database is created so a fresh install starts with
    the global baseline thresholds persisted (UI and detector agree from the
    first run).  No-op if a config already exists.

    @return wrote  True if defaults were written, False otherwise.
    """
    path = getConfigPath()
    if os.path.isfile(path):
        return False
    return saveConfig(copy.deepcopy(DEFAULTS))


def migrateConfig():
    """Bring an existing config forward when a default changes meaningfully.

    Deliberately NOT called from loadConfig(): that runs on every read, in
    several processes, and must stay free of side effects.  Call it where
    ensureDefaults() is called, plus once at DetectionService startup so it
    applies even if the Options dialog is never opened.

    Migrations are gated on CONFIG_VERSION so each one runs at most once.  That
    matters here: a stored "yolo26n.pt" is indistinguishable from a deliberate
    choice of the nano model, so without the gate this would keep overriding a
    user who genuinely wants it.  After the migration the user's choice wins
    for good.

    @return migrated  True if the config was changed and rewritten.
    """
    path = getConfigPath()
    if not os.path.isfile(path):
        return False            # ensureDefaults() will write current defaults
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False

    version = cfg.get("CONFIG_VERSION", 0)
    if version >= DEFAULTS["CONFIG_VERSION"]:
        return False

    # v1: the shipped YOLO default moved from the nano model to the small one.
    # Only rewrite configs still sitting on the OLD default -- anything else is
    # an explicit choice from the (new) picker and is left alone.
    if version < 1 and cfg.get("YOLO_MODEL") == "yolo26n.pt":
        cfg["YOLO_MODEL"] = DEFAULTS["YOLO_MODEL"]

    cfg["CONFIG_VERSION"] = DEFAULTS["CONFIG_VERSION"]
    return saveConfig(cfg)


def onnxRuntimeTargets():
    """Which execution providers and InsightFace context to ask onnxruntime for.

    Decided from what onnxruntime ACTUALLY offers, never assumed.  Asking for the
    CUDA provider on a machine that has no NVIDIA GPU does not fall back politely
    -- onnxruntime-gpu can take the whole process down with an access violation,
    which is not a Python exception and so cannot be caught by the try/except
    around the load.  That is exactly what happened on a GPU-less Hyper-V test
    machine on 2026-08-14: the DetectionService died inside the InsightFace load
    nine times in 95 seconds, logging neither success nor failure, and the back
    end restarted it forever.

    Two checks, and BOTH are needed:

      1. onnxruntime must offer the CUDA provider at all.  This alone is not
         enough -- get_available_providers() reports what the BUILD was compiled
         with, not what this machine can run.  Measured on the dev box: it lists
         TensorrtExecutionProvider on a machine with no TensorRT installed.
      2. torch must actually see a usable CUDA device.  This is the hardware
         oracle, and a soft query -- on the GPU-less test machine it returned
         False cleanly while the ORT list still advertised CUDA.  torch is
         already imported by the DetectionService (YOLO loads first), so this
         costs nothing there.

    If torch cannot be imported at all we fall back to trusting the ORT list,
    which is the best information left.

    Imports are inside the function so this module stays importable by the
    light-weight callers that only want config paths.

    @return  (providers, ctxId).  ctxId is InsightFace's device selector: 0 means
             GPU 0, -1 means CPU.
    """
    cpuOnly = (["CPUExecutionProvider"], -1)
    cuda = (["CUDAExecutionProvider", "CPUExecutionProvider"], 0)

    try:
        import onnxruntime
        if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
            return cpuOnly
    except Exception:
        return cpuOnly

    try:
        import torch
    except Exception:
        return cuda             # no second opinion available; trust the build
    try:
        return cuda if torch.cuda.is_available() else cpuOnly
    except Exception:
        return cpuOnly


def enabledNudeThresholds(cfg):
    """Return the effective {CLASS: threshold} dict for nudity detection.

    Only classes that are both listed in NUDE_ENABLED and present in
    NUDE_THRESHOLDS are returned.  If NUDE_ENABLED is absent (older config),
    every class in NUDE_THRESHOLDS is treated as enabled (back-compat).

    @param  cfg  Full config dict (post-merge).
    @return out  {CLASS_UPPER: float_threshold}
    """
    thresholds = {
        k.upper(): v
        for k, v in cfg.get("NUDE_THRESHOLDS", DEFAULTS["NUDE_THRESHOLDS"]).items()
    }
    enabled = cfg.get("NUDE_ENABLED", None)
    if enabled is None:
        return thresholds
    enabledSet = {e.upper() for e in enabled}
    return {k: v for k, v in thresholds.items() if k in enabledSet}
