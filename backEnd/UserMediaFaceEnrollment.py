"""Preview faces from Image-view detections; persistence uses FaceEnrollment.

Read usermedia.db only. User files must never be registered in clipdb, where
DiskCleaner would treat them as recordings it owns.
"""

import math
import os
from pathlib import Path
import sqlite3


def resolveDetection(dbPath, path, detectionId, analyzedMs):
    """Resolve a displayed detection, rejecting replaced results/files."""
    conn = sqlite3.connect(Path(dbPath).resolve().as_uri() + '?mode=ro',
                           uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        hasLocations = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_locations'").fetchone()
        locationJoin = ('JOIN file_locations l ON l.fileUid=f.uid ' if hasLocations else '')
        prefix = 'l' if hasLocations else 'f'
        row = conn.execute(
            ('SELECT d.*, {p}.path, f.kind, {p}.size, {p}.mtime, f.analyzedMs '
             'FROM detections d JOIN files f ON f.uid = d.fileUid '
             '{join}WHERE d.uid = ? AND {p}.path = ?').format(p=prefix, join=locationJoin),
            (int(detectionId), path)).fetchone()
    finally:
        conn.close()
    if row is None or str(row['analyzedMs']) != str(analyzedMs):
        raise ValueError('This detection has changed. Select the file again.')
    if row['type'] != 'person' or row['faceDetConf'] is None:
        raise ValueError('Select a detection with a face.')
    if row['kind'] not in ('image', 'video'):
        raise ValueError('This file type cannot be enrolled.')
    stat = os.stat(path)
    if stat.st_size != row['size'] or int(stat.st_mtime) != row['mtime']:
        raise ValueError('This file has changed. Analyze it again first.')
    return dict(row)


def candidatesFromFrame(frameRgb, box, client, minDet, minFacePx):
    """Extract quality-gated faces inside the selected normalized person box.

Offer each usable face for explicit preview selection if boxes overlap.
Never choose the largest person elsewhere in a group photo.
"""
    import io
    import numpy as np
    from PIL import Image

    height, width = frameRgb.shape[:2]
    coords = [float(v) for v in box]
    if (not all(math.isfinite(v) and 0 <= v <= 1 for v in coords)
            or coords[0] >= coords[2] or coords[1] >= coords[3]):
        return {'error': 'This detection has an invalid person region.'}
    x1, y1, x2, y2 = [int(round(v * s)) for v, s in
                       zip(coords, (width, height, width, height))]
    crop = np.ascontiguousarray(frameRgb[y1:y2, x1:x2])
    if crop.size == 0:
        return {'error': 'This detection has an empty person region.'}
    candidates = []
    for face in client.face(crop) or []:
        score = float(getattr(face, 'det_score', 0) or 0)
        bbox = getattr(face, 'bbox', None)
        embedding = getattr(face, 'embedding', None)
        if not math.isfinite(score) or score < minDet or bbox is None or embedding is None:
            continue
        if len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox):
            continue
        fx1, fy1, fx2, fy2 = [int(v) for v in bbox]
        ch, cw = crop.shape[:2]
        fx1, fx2 = max(0, min(cw, fx1)), max(0, min(cw, fx2))
        fy1, fy2 = max(0, min(ch, fy1)), max(0, min(ch, fy2))
        fw, fh = fx2 - fx1, fy2 - fy1
        if min(fw, fh) < max(1, minFacePx):
            continue
        emb = np.asarray(embedding, dtype=np.float32).flatten()
        norm = float(np.linalg.norm(emb))
        if not emb.size or not np.isfinite(emb).all() or not math.isfinite(norm) or norm <= 0:
            continue
        emb = emb / norm
        # Match the existing enrollment preview's context margin.
        faceCrop = crop[max(0, int(fy1 - .6 * fh)):int(fy2 + .6 * fh),
                        max(0, int(fx1 - .6 * fw)):int(fx2 + .6 * fw)]
        buf = io.BytesIO()
        Image.fromarray(faceCrop).save(buf, format='JPEG', quality=95)
        candidates.append({'det': score, 'w': fw, 'h': fh,
                           'jpeg': buf.getvalue(), 'embedding': emb})
    if not candidates:
        return {'error': 'No face met the enrollment quality floors '
                '(detection >= %.0f%%, size >= %d px). Try a clearer image '
                'or another video detection.' % (minDet * 100, minFacePx)}
    candidates.sort(key=lambda c: -c['det'])
    # Keep the shared preview manageable. Other sampled detections remain selectable.
    return {'ok': True, 'candidates': candidates[:3]}


def harvestUserMediaFace(dbPath, path, detectionId, analyzedMs, logger):
    """Read the selected still/frame and prepare crops without saving anything."""
    from backEnd.FaceEnrollment import _enrollFloors
    from backEnd.DetectionServiceClient import DetectionServiceClient
    from backEnd.UserMediaAnalysis import _readImageRgb

    row = resolveDetection(dbPath, path, detectionId, analyzedMs)
    if row['kind'] == 'image':
        frame = _readImageRgb(path)
    else:
        import cv2
        cap = cv2.VideoCapture(path)
        try:
            atMs = int(row['atMs'] or 0)
            if atMs < 0 or not cap.isOpened():
                return {'error': 'Could not open the selected video frame.'}
            # Same seek/decode convention as UserMediaAnalysis.analyzeVideo.
            cap.set(cv2.CAP_PROP_POS_MSEC, atMs)
            ok, bgr = cap.read()
            if not ok or bgr is None:
                return {'error': 'The selected video frame is no longer readable.'}
            frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        finally:
            cap.release()
    client = DetectionServiceClient(logger)
    try:
        if not client.ping().get('face'):
            return {'error': 'The detection service has no face model loaded.'}
        return candidatesFromFrame(frame, [row[k] for k in ('x1', 'y1', 'x2', 'y2')],
                                   client, *_enrollFloors())
    finally:
        client.close()
