"""
TapoResponse.py

Response class: sound the siren and/or switch on the white spotlight of the
rule's own TP-Link Tapo camera, over the camera's local control API on port 443.

Configuration keys (from the rule editor, stored per-rule):
  tapoSiren  bool  Sound the siren
  tapoLight  bool  Switch the white spotlight on

Injected by BackEndApp._loadResponses, not stored per rule:
  tapoHost   str   The camera's address, derived from its stream URI

The ACCOUNT is global (Options -> Tapo, backEnd/TapoConfig.py) and is NOT stored
per rule -- and deliberately never travels on the queue.  The ResponseRunner
reads it at fire time.  One trigger per newly-appearing tracked object, using the
same _kObjTimeoutMs de-dup as IHostResponse.

Fire and forget: the siren stops itself after the camera's configured alarm
duration and the camera runs its own timer on the lamp (300s when measured), so
this response never schedules an "off".
"""

import time

from backEnd.responses.BaseResponse import BaseResponse
from backEnd import MessageIds

_kObjTimeoutMs = 10000


class TapoResponse(BaseResponse):

    def __init__(self, ruleName, camLoc, responseRunnerQueue, configDict=None):
        super().__init__()
        if configDict is None:
            configDict = {}

        self._ruleName            = ruleName
        self._camLoc              = camLoc
        self._responseRunnerQueue = responseRunnerQueue
        self._host                = configDict.get("tapoHost", "")
        self._siren               = bool(configDict.get("tapoSiren", False))
        self._light               = bool(configDict.get("tapoLight", False))

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
        if newHits and self._host and (self._siren or self._light):
            self._responseRunnerQueue.put([
                MessageIds.msgIdTapoAction,
                self._camLoc, self._ruleName,
                self._host, self._siren, self._light,
            ])

    def startNewSession(self):
        pass
