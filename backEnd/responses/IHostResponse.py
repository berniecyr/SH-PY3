"""
IHostResponse.py

Response class: send a command to an iHost / eWeLink CUBE device directly (the
native replacement for the external CubeScript webhook relay).

Configuration keys (from the rule editor, stored per-rule):
  ihostDevice     str   Device id (serial_number) to control
  ihostDeviceName str   Human name (for logs / display)
  ihostCommand    str   'on' | 'off' | 'toggle'  (default 'on')
  ihostTimeout    int   Auto-off after N seconds of no motion (0 = stay on)
  ihostNightOnly  bool  Only act between sunset and sunrise

The hub IP + token are GLOBAL (Options -> iHost, backEnd/IHostConfig.py) and are
NOT stored per rule.  One trigger per newly-appearing tracked object, using the
same _kObjTimeoutMs de-dup as WebhookResponse.  All stateful
behaviour (turn-on, auto-off, manual-preserve, night gate) lives in the
ResponseRunner's IHostController; this class just forwards the intent.
"""

import time

from backEnd.responses.BaseResponse import BaseResponse
from backEnd import MessageIds

_kObjTimeoutMs = 10000


class IHostResponse(BaseResponse):

    def __init__(self, ruleName, camLoc, responseRunnerQueue, configDict=None):
        super().__init__()
        if configDict is None:
            configDict = {}

        self._ruleName            = ruleName
        self._camLoc              = camLoc
        self._responseRunnerQueue = responseRunnerQueue
        self._deviceId            = configDict.get("ihostDevice", "")
        self._deviceName          = configDict.get("ihostDeviceName", "")
        self._command             = configDict.get("ihostCommand", "on")
        self._timeout             = configDict.get("ihostTimeout", 300)
        self._nightOnly           = bool(configDict.get("ihostNightOnly", False))

        self._objIdsProcessed = {}

    def _getNewActiveObjects(self, rangeDict):
        triggers = []
        for objId, objRanges in rangeDict.items():
            if objId not in self._objIdsProcessed:
                firstMs = objRanges[0][0][1]
                triggers.append((objId, firstMs))
            lastMs = objRanges[-1][1][1]
            self._objIdsProcessed[objId] = lastMs
        return triggers

    def _activeObjectsGC(self):
        now = int(time.time() * 1000)
        for objId in list(self._objIdsProcessed):
            if now - self._objIdsProcessed[objId] > _kObjTimeoutMs:
                del self._objIdsProcessed[objId]

    def addRanges(self, ms, rangeDict):
        newHits = self._getNewActiveObjects(rangeDict)
        self._activeObjectsGC()
        if newHits and self._deviceId:
            self._responseRunnerQueue.put([
                MessageIds.msgIdSendIHost,
                self._camLoc, self._ruleName,
                self._deviceId, self._deviceName,
                self._command, self._timeout, self._nightOnly,
            ])

    def startNewSession(self):
        pass
