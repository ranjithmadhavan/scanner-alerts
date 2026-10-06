import os

os.environ.setdefault("STORAGE", "memory")
os.environ.setdefault("SCANNER_ENABLED", "0")
os.environ.setdefault("BIAS_CAPTURE", "0")
os.environ.setdefault("OI_CAPTURE", "0")  # tests that want scheduled OI snapshots switch it on
os.environ.setdefault("SESSION_SECRET", "test")
