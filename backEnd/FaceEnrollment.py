"""
FaceEnrollment.py

Shared face-enrollment-from-footage core, used by BOTH the LAN record viewer
(WebServer.py) and the desktop Search window (via NetworkMessageServer RPC).

Given a recorded clip and a moment in it, it samples frames around that moment,
re-finds the person with the shared DetectionService, extracts the best face
crops, saves them as JPEGs into the same Baseline/<Person>/ folder the photo
enrollment uses, and appends their ArcFace embeddings to known_faces.dat
(atomic write, one-time .bak backup).

The flow is split into HARVEST (find + quality-gate candidate crops, no
persistence) and COMMIT (save the crops the user confirmed) so the desktop UI
can show a preview with quality info before anything touches the baseline.
Quality floors (ENROLL_MIN_DET / ENROLL_MIN_FACE_PX in imagecheck_config.json)
reject junk crops server-side.

This module also owns the baseline MANAGEMENT primitives (delete / move /
rename / rebuild).  known_faces.dat has no image->embedding mapping, so every
mutation finishes with rebuildKnownFaces(), which re-derives the whole file
from the Baseline folders via the shared DetectionService (the model is
already loaded there — no InsightFace import in this process).

Why a shared module: camera-view enrollments dramatically outperform portrait
photos for recognition (similarity ~0.6 vs ~0.3-0.5 against camera footage),
so both entry points must build identical data.  Camera processes hot-reload
known_faces.dat, so changes start matching within seconds without a restart.
"""

import os
import pickle
import re
import shutil
import threading
import time


# The Baseline photo folder + recognition cache the rest of the app uses.
# Resolved through InstallPaths so a service-launched back end uses the user's
# data directory rather than the service account's profile.
try:
    from appCommon.InstallPaths import getUserDataDir as _getUserDataDir
    _kDataDir = _getUserDataDir()
except Exception:
    _kDataDir = os.path.join(os.path.expanduser('~'), 'AppData', 'Local',
                             'Sighthound Video Py3')
kBaselineDir     = os.path.join(_kDataDir, 'Baseline')
kKnownFacesDat   = os.path.join(_kDataDir, 'known_faces.dat')

_kMaxCrops   = 3        # face crops offered per enroll action
_kSampleOffsetsSec = (-1.5, -0.75, 0.0, 0.75, 1.5)   # around the target moment

_kImageExts = ('.jpg', '.jpeg', '.png', '.bmp')

# One enroll at a time: known_faces.dat is a read-modify-write.
_enrollLock = threading.Lock()


def displayNameFromFolder(folderName):
    """ 'Bernie-M' -> 'Bernie', 'Alice' -> 'Alice' (matches EnrollFaces). """
    return folderName.split('-')[0]


def listBaselinePeople():
    """ Existing enrollment folders as [{'folder', 'name'}]. """
    out = []
    try:
        for d in sorted(os.listdir(kBaselineDir)):
            full = os.path.join(kBaselineDir, d)
            if os.path.isdir(full) and d.lower() != 'ignore':
                out.append({"folder": d, "name": displayNameFromFolder(d)})
    except OSError:
        pass
    return out


def safePersonName(name):
    """ Sanitize a submitted name into a display name / folder stem:
    letters, digits, spaces and underscores only; no path separators. """
    cleaned = "".join(c for c in (name or "").strip()
                      if c.isalnum() or c in (" ", "_")).strip()
    return cleaned[:40]


def _enrollFloors():
    """ (minDet, minFacePx) quality floors from imagecheck_config.json. """
    try:
        from backEnd.ImageCheckConfig import loadConfig
        cfg = loadConfig()
        return (float(cfg.get("ENROLL_MIN_DET", 0.65)),
                int(cfg.get("ENROLL_MIN_FACE_PX", 64)))
    except Exception:
        return 0.65, 64


def _resolveUnderBaseline(relPath):
    """ Resolve a client-supplied relative path against the Baseline dir.

    @return absPath inside kBaselineDir, or None if the path escapes it.
    """
    if not relPath or os.path.isabs(relPath):
        return None
    base = os.path.normpath(kBaselineDir)
    full = os.path.normpath(os.path.join(base, relPath))
    if full == base or not full.startswith(base + os.sep):
        return None
    return full


def _safeFolderName(folder):
    """ Validate a client-supplied baseline FOLDER name (no separators). """
    if not folder or folder != os.path.basename(folder) or folder in ('.', '..'):
        return None
    if folder.lower() == 'ignore':
        return None
    return folder


###############################################################################
# Harvest / commit (the enroll-with-preview flow)
###############################################################################

def harvestFaceCandidates(absPath, firstMs, centerMs, camLoc, logger):
    """ Find quality-gated face-crop candidates around a moment in a clip.

    NO persistence — the caller previews these and commits a selection via
    commitFaceEnrollment().

    @param  absPath   Absolute path to the recorded clip on disk.
    @param  firstMs   Absolute ms of the clip's first frame.
    @param  centerMs  Absolute ms of the moment to harvest around.
    @param  camLoc    Camera location (for logging only here).
    @param  logger    Logger.
    @return dict      {'ok': True, 'candidates': [ {det, w, h, jpeg (bytes),
                      embedding (np.float32)} ... best first ]} or
                      {'error': ...}.  On success with zero candidates the
                      error explains the best rejected quality vs the floors.
    """
    import cv2                       # heavy-ish: import on use, not on import
    import numpy as np
    from backEnd.DetectionServiceClient import DetectionServiceClient

    if not absPath or not os.path.isfile(absPath):
        return {"error": "the recorded clip for this moment is not on disk"}

    try:
        cli = DetectionServiceClient(logger)
        caps = cli.ping()            # -> capability dict, e.g. {'face': True}
        if not caps.get('face'):
            return {"error": "detection service has no face model loaded "
                             "(is face recognition enabled?)"}
    except Exception:
        return {"error": "detection service unavailable"}

    minDet, minFacePx = _enrollFloors()

    cap = cv2.VideoCapture(absPath)
    candidates = []                   # (det, faceW, faceH, jpegBytes, emb)
    bestRejected = None               # (det, facePx) of the best floor-reject
    sawAnyFace = False
    try:
        for offSec in _kSampleOffsetsSec:
            posMs = (centerMs - firstMs) + offSec * 1000.0
            if posMs < 0:
                continue
            cap.set(cv2.CAP_PROP_POS_MSEC, posMs)
            ok, frameBgr = cap.read()
            if not ok:
                continue
            rgb = cv2.cvtColor(frameBgr, cv2.COLOR_BGR2RGB)
            try:
                persons = [d for d in cli.yolo(rgb, 0.4) if d[0] == 'person']
            except Exception:
                return {"error": "detection service unavailable"}
            if not persons:
                continue
            # Single-subject assumption: enroll the largest person in frame.
            persons.sort(key=lambda d: -(d[4] - d[2]) * (d[5] - d[3]))
            _, _s, x1, y1, x2, y2 = persons[0]
            w, h = x2 - x1, y2 - y1
            crop = rgb[max(0, int(y1 - 0.4 * h)):int(y2 + 0.4 * h),
                       max(0, int(x1 - 0.4 * w)):int(x2 + 0.4 * w)]
            if crop.size == 0:
                continue
            try:
                faces = cli.face(crop)
            except Exception:
                return {"error": "detection service unavailable"}
            if not faces:
                continue
            sawAnyFace = True
            face = max(faces, key=lambda f: (f.det_score or 0))
            det = float(face.det_score or 0)
            if face.embedding is None:
                continue

            # Face pixel size from the service's bbox (junk filter: a tiny
            # face carries too little detail to help recognition).
            bbox = getattr(face, 'bbox', None)
            faceW = faceH = 0
            if bbox is not None and len(bbox) == 4:
                fx1, fy1, fx2, fy2 = (int(v) for v in bbox)
                faceW, faceH = max(0, fx2 - fx1), max(0, fy2 - fy1)
            facePx = min(faceW, faceH) if faceW and faceH else 0

            # Quality floors (config: ENROLL_MIN_DET / ENROLL_MIN_FACE_PX).
            # A missing bbox leaves facePx unknown — gate on det alone then.
            if det < minDet or (facePx and facePx < minFacePx):
                if bestRejected is None or det > bestRejected[0]:
                    bestRejected = (det, facePx)
                continue

            emb = np.asarray(face.embedding, np.float32).flatten()
            emb /= (np.linalg.norm(emb) + 1e-9)
            # Tight face crop (bbox + context) when the service returns a bbox;
            # else the whole person crop.
            if faceW and faceH:
                fw, fh, m = faceW, faceH, 0.6
                fc = crop[max(0, int(fy1 - m * fh)):int(fy2 + m * fh),
                          max(0, int(fx1 - m * fw)):int(fx2 + m * fw)]
                if fc.size == 0:
                    fc = crop
            else:
                fc = crop
            okEnc, buf = cv2.imencode(
                '.jpg', cv2.cvtColor(fc, cv2.COLOR_RGB2BGR),
                [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if not okEnc:
                continue
            candidates.append((det, faceW, faceH, buf.tobytes(), emb))
    finally:
        cap.release()

    if not candidates:
        if bestRejected is not None:
            det, px = bestRejected
            sizeStr = (", %d px" % px) if px else ""
            return {"error": "no face met the enrollment quality floors "
                             "(best candidate: det %.0f%%%s; floors: det >= "
                             "%.0f%%, >= %d px). Try a moment where the face "
                             "is closer and clearer."
                             % (det * 100, sizeStr, minDet * 100, minFacePx)}
        if sawAnyFace:
            return {"error": "a face was seen but no usable embedding could "
                             "be extracted around this moment"}
        return {"error": "no face detected in the footage around this moment"}

    # Best first; drop near-duplicate embeddings (same decoded frame twice).
    candidates.sort(key=lambda c: -c[0])
    chosen = []
    import numpy as np2  # local alias for clarity in the comprehension below
    for det, faceW, faceH, jpeg, emb in candidates:
        if any(float(np2.dot(emb, c["embedding"])) > 0.995 for c in chosen):
            continue
        chosen.append({"det": det, "w": faceW, "h": faceH,
                       "jpeg": jpeg, "embedding": emb})
        if len(chosen) >= _kMaxCrops:
            break

    return {"ok": True, "candidates": chosen}


def commitFaceEnrollment(name, gender, candidates, camLoc, centerMs, logger):
    """ Persist previously harvested candidates under `name`.

    Saves each candidate's JPEG into Baseline/<folder>/ and appends its
    embedding to known_faces.dat (atomic, under the enroll lock).

    @param  name        Person's display name (existing or new).
    @param  gender      'M' / 'F' / '' (used only when creating a new folder).
    @param  candidates  List of harvest dicts ({det, jpeg, embedding, ...}).
    @param  camLoc      Camera location (used only in saved filenames).
    @param  centerMs    Absolute ms of the enrolled moment (for filenames).
    @param  logger      Logger.
    @return dict        {'ok': True, 'name', 'folder', 'added', 'images',
                        'totalEncodings'} or {'error': ...}.
    """
    name = safePersonName(name)
    if not name:
        return {"error": "a person name is required"}
    if not candidates:
        return {"error": "no face crops were selected"}

    gender = (gender or "").upper()
    if gender not in ("M", "F"):
        gender = ""

    # -- pick the enrollment folder (existing match by display name, else new)
    folder = None
    for p in listBaselinePeople():
        if p["name"].lower() == name.lower():
            folder = p["folder"]
            name = p["name"]          # keep the established capitalization
            break
    if folder is None:
        folder = name + ("-%s" % gender if gender else "")
    folderPath = os.path.join(kBaselineDir, folder)

    # -- persist: JPEGs into Baseline/<folder>/ + embeddings into the dat ---
    with _enrollLock:
        try:
            os.makedirs(folderPath, exist_ok=True)
        except OSError:
            return {"error": "cannot create baseline folder %r" % folder}
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(centerMs / 1000.0))
        camTag = re.sub(r'[^A-Za-z0-9_-]', '_', camLoc or "cam")
        saved = []
        for i, cand in enumerate(candidates):
            fn = "cam_%s_%s_%d.jpg" % (camTag, stamp, i)
            fp = os.path.join(folderPath, fn)
            try:
                with open(fp, "wb") as f:
                    f.write(cand["jpeg"])
                saved.append(fn)
            except Exception:
                pass
        if not saved:
            return {"error": "could not write face images to the baseline"}

        encodings, names = [], []
        if os.path.isfile(kKnownFacesDat):
            try:
                with open(kKnownFacesDat, "rb") as f:
                    data = pickle.load(f)
                encodings = list(data.get("encodings", []))
                names = list(data.get("names", []))
            except Exception:
                return {"error": "known_faces.dat is unreadable; not appending"}
            bak = kKnownFacesDat + ".bak"
            if not os.path.exists(bak):
                try:
                    shutil.copy2(kKnownFacesDat, bak)
                except Exception:
                    pass
        for cand in candidates:
            encodings.append(cand["embedding"])
            names.append(name)
        if not _writeKnownFacesDat(encodings, names):
            return {"error": "could not update known_faces.dat"}

    if logger is not None:
        try:
            logger.info("enrolled %d face crop(s) for %r (folder %r) "
                        "from %s @ %d" % (len(candidates), name, folder,
                                          camLoc, centerMs))
        except Exception:
            pass
    return {"ok": True, "name": name, "folder": folder,
            "added": len(candidates), "images": saved,
            "totalEncodings": len(names)}


def enrollFaceFromClipFile(absPath, firstMs, centerMs, camLoc, name, gender,
                           logger):
    """ One-shot harvest + commit-all (compat wrapper; LAN viewer path).

    @return dict as commitFaceEnrollment / {'error': ...}.
    """
    name = safePersonName(name)
    if not name:
        return {"error": "a person name is required"}
    result = harvestFaceCandidates(absPath, firstMs, centerMs, camLoc, logger)
    if not result.get("ok"):
        return result
    return commitFaceEnrollment(name, gender, result["candidates"],
                                camLoc, centerMs, logger)


###############################################################################
# Baseline management (delete / move / rename / rebuild)
###############################################################################

def _writeKnownFacesDat(encodings, names):
    """ Atomically write known_faces.dat.  Caller holds _enrollLock. """
    tmp = kKnownFacesDat + ".tmp"
    try:
        with open(tmp, "wb") as f:
            pickle.dump({"encodings": encodings, "names": names}, f)
        os.replace(tmp, kKnownFacesDat)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def rebuildKnownFaces(logger):
    """ Re-derive known_faces.dat from the Baseline folders.

    Routes every image through the shared DetectionService (its face model is
    already loaded — no InsightFace import here), reusing EnrollFaces'
    portrait preprocessing.  THE consistency primitive: every baseline
    mutation ends here, and the Options "Re-enroll faces now" button uses it
    too.  Writing proceeds even when the result is empty (deleting the last
    person must clear the file).

    @return {'ok': True, 'people', 'totalEncodings', 'skipped'} or
            {'error': ...}.
    """
    import cv2
    import numpy as np
    from backEnd.DetectionServiceClient import DetectionServiceClient
    from backEnd.EnrollFaces import _prepareImage   # cv2-only preprocessing

    try:
        cli = DetectionServiceClient(logger)
        caps = cli.ping()
        if not caps.get('face'):
            return {"error": "detection service has no face model loaded "
                             "(is face recognition enabled?)"}
    except Exception:
        return {"error": "detection service unavailable"}

    encodings, names = [], []
    skipped = 0
    for person in listBaselinePeople():
        folderPath = os.path.join(kBaselineDir, person["folder"])
        try:
            files = sorted(os.listdir(folderPath))
        except OSError:
            continue
        for fn in files:
            if not fn.lower().endswith(_kImageExts):
                continue
            img = cv2.imread(os.path.join(folderPath, fn))
            if img is None:
                skipped += 1
                continue
            prepped = _prepareImage(img)
            try:
                faces = cli.face(cv2.cvtColor(prepped, cv2.COLOR_BGR2RGB))
            except Exception:
                return {"error": "detection service unavailable"}
            faces = [f for f in (faces or []) if f.embedding is not None]
            if not faces:
                skipped += 1
                continue

            # Largest face in the image (same policy as batch EnrollFaces).
            def _area(f):
                bbox = getattr(f, 'bbox', None)
                if bbox is None or len(bbox) != 4:
                    return 0
                return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
            face = max(faces, key=_area)
            emb = np.asarray(face.embedding, np.float32).flatten()
            emb /= (np.linalg.norm(emb) + 1e-9)
            encodings.append(emb)
            names.append(person["name"])

    with _enrollLock:
        if not _writeKnownFacesDat(encodings, names):
            return {"error": "could not write known_faces.dat"}

    if logger is not None:
        try:
            logger.info("rebuilt known_faces.dat: %d encodings, %d people, "
                        "%d skipped" % (len(names), len(set(names)), skipped))
        except Exception:
            pass
    return {"ok": True, "people": len(set(names)),
            "totalEncodings": len(names), "skipped": skipped}


def _rebuildAfter(logger, extra=None):
    """ Rebuild the dat and fold `extra` keys into the result. """
    result = rebuildKnownFaces(logger)
    if extra and isinstance(result, dict):
        result.update(extra)
    return result


def deleteBaselineImages(relPaths, logger):
    """ Delete individual baseline images (paths relative to the Baseline
    dir, e.g. 'Bernie-M/cam_....jpg'), then rebuild the dat. """
    deleted = 0
    for rel in (relPaths or []):
        full = _resolveUnderBaseline(rel)
        if full is None:
            return {"error": "invalid image path %r" % rel}
        try:
            os.remove(full)
            deleted += 1
        except OSError:
            pass
    return _rebuildAfter(logger, {"deleted": deleted})


def moveBaselineImages(relPaths, targetFolder, logger):
    """ Move baseline images into another person's folder (correction).
    targetFolder may be a new 'Name-G' folder; created if missing. """
    targetFolder = _safeFolderName(targetFolder)
    if targetFolder is None:
        return {"error": "invalid target folder"}
    targetPath = os.path.join(kBaselineDir, targetFolder)
    try:
        os.makedirs(targetPath, exist_ok=True)
    except OSError:
        return {"error": "cannot create folder %r" % targetFolder}

    moved = 0
    for rel in (relPaths or []):
        full = _resolveUnderBaseline(rel)
        if full is None:
            return {"error": "invalid image path %r" % rel}
        dest = os.path.join(targetPath, os.path.basename(full))
        # Never clobber: suffix on name collision.
        if os.path.exists(dest):
            stem, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists("%s_%d%s" % (stem, n, ext)):
                n += 1
            dest = "%s_%d%s" % (stem, n, ext)
        try:
            shutil.move(full, dest)
            moved += 1
        except OSError:
            pass
    return _rebuildAfter(logger, {"moved": moved})


def renameBaselinePerson(folder, newName, logger):
    """ Rename a person (folder rename; the '-G' gender suffix is kept),
    then rebuild so the dat names follow. """
    folder = _safeFolderName(folder)
    if folder is None:
        return {"error": "invalid folder"}
    src = os.path.join(kBaselineDir, folder)
    if not os.path.isdir(src):
        return {"error": "no such person folder"}
    newName = safePersonName(newName)
    if not newName:
        return {"error": "a new name is required"}
    suffix = folder.split('-', 1)[1] if '-' in folder else ""
    newFolder = newName + ("-%s" % suffix if suffix else "")
    if newFolder == folder:
        return _rebuildAfter(logger)
    dst = os.path.join(kBaselineDir, newFolder)
    if os.path.exists(dst):
        return {"error": "a person folder named %r already exists" % newFolder}
    try:
        os.rename(src, dst)
    except OSError:
        return {"error": "could not rename the folder"}
    return _rebuildAfter(logger, {"folder": newFolder})


def deleteBaselinePerson(folder, logger):
    """ Delete a person and all their baseline images, then rebuild. """
    folder = _safeFolderName(folder)
    if folder is None:
        return {"error": "invalid folder"}
    full = os.path.join(kBaselineDir, folder)
    if not os.path.isdir(full):
        return {"error": "no such person folder"}
    try:
        shutil.rmtree(full)
    except OSError:
        return {"error": "could not delete the folder"}
    return _rebuildAfter(logger)
