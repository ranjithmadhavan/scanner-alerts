import os
import tempfile

os.environ.setdefault("STORAGE", "memory")
os.environ.setdefault("SCANNER_ENABLED", "0")
os.environ.setdefault("BIAS_CAPTURE", "0")
os.environ.setdefault("OI_CAPTURE", "0")  # tests that want scheduled OI snapshots switch it on
os.environ.setdefault("SESSION_SECRET", "test")
os.environ.setdefault("INSTRUMENTS_CACHE_DIR", tempfile.mkdtemp(prefix="instruments-"))  # never the real cache
