# Stub svsentry.Sentry — AI analytics engine not available.
# ObjectCollector is a base class; VideoPipeline calls its methods when objects
# are detected. With no real pipeline running, none of these callbacks fire.

kDefaultPipelineConfigFile = None


def loadSentry(loadLibraryFunc=None, configFile=None):
    pass


class ObjectCollector(object):
    def __init__(self):
        pass

    def __del__(self):
        pass


class VideoPipeline(object):
    def __init__(self, objectCollector, pipelineConfigFile=None):
        self.objectCollector = objectCollector

    def updateVideoPath(self, videoPath):
        pass

    def processClipFrame(self, frame, ms):
        pass

    def flush(self):
        pass
