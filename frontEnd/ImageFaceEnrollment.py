"""Image-view enrollment using Search's preview and backend commit workflow."""

import threading
import wx

from appCommon.InstallPaths import getUserDataDir
from frontEnd.BackEndClient import BackEndClient
from frontEnd.EnrollFacePreviewDialog import EnrollFacePreviewDialog


def _request(parent, message, operation, logger):
    """Keep the UI responsive; use a worker-owned XML-RPC connection.

The app-modal progress window prevents a second enrollment or selection change.
Commit cannot be cancelled after dispatch: its outcome must be reported.
"""
    dataDir = getUserDataDir()
    result = {}

    def work():
        try:
            client = BackEndClient()
            if not client.connect(dataDir, timeout=90):
                raise RuntimeError('Could not connect to the back end.')
            result['value'] = operation(client)
        except Exception as exc:
            logger.error('Image face enrollment failed', exc_info=True)
            result['value'] = {'error': str(exc)}

    dialog = wx.ProgressDialog('Add face to baseline', message, parent=parent,
                               style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
    worker = threading.Thread(target=work, name='imageFaceEnrollment', daemon=True)
    try:
        worker.start()
        while worker.is_alive():
            dialog.Pulse()
            wx.MilliSleep(50)
            wx.SafeYield(dialog, True)
        worker.join()
    finally:
        dialog.Destroy()
    return result.get('value') or {'error': 'No response from the back end.'}


def enrollFace(parent, path, detection, analyzedMs, logger):
    """Preview selected detection, then commit only the confirmed face crops."""
    def harvest(client):
        result = client.harvestFaceFromUserMedia(path, detection['uid'], analyzedMs)
        if result and result.get('ok'):
            result['people'] = client.getBaselinePeople() or []
        return result

    result = _request(parent, 'Preparing face crops...', harvest, logger)
    if not result.get('ok') or not result.get('candidates'):
        wx.MessageBox(result.get('error', 'No usable face found.'), 'No face found',
                      wx.OK | wx.ICON_INFORMATION, parent)
        return False
    names = [p['name'] for p in result['people'] if p.get('name')]
    dialog = EnrollFacePreviewDialog(parent, result['candidates'], names,
                                    prefillName=detection.get('faceName') or '',
                                    prefillGender=detection.get('gender') or '')
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return False
        name, gender = dialog.getName(), dialog.getGender()
        selected = dialog.getSelectedIndices()
    finally:
        dialog.Destroy()
    if not name or not selected:
        return False
    committed = _request(parent, 'Adding selected face crops...',
        lambda client: client.commitFaceHarvest(result['token'], selected, name, gender),
        logger)
    if not committed.get('ok'):
        wx.MessageBox(committed.get('error', 'Enrollment failed.'), 'Face not added',
                      wx.OK | wx.ICON_ERROR, parent)
        return False
    wx.MessageBox('Added %d face image(s) for %s to the baseline.\n\n'
                  'Cameras reload the baseline automatically. Analyze this file '
                  'again to update its saved recognition results.' %
                  (committed.get('added', 0), committed.get('name', name)),
                  'Face added', wx.OK | wx.ICON_INFORMATION, parent)
    return True
