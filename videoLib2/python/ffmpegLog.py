class FFmpegLog:
    """No-op stub — ffmpeg logging C extension not available."""
    def __init__(self, *a, **kw): pass
    def open(self, *a, **kw): return 0
    def flush(self, *a, **kw): return 0
    def close(self, *a, **kw): pass
