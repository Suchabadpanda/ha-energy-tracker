import os
import tempfile
from pathlib import Path

# The app reads its settings once, when first used: point it at a throwaway database and
# make sure it does not try to reach a real Home Assistant.
os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
for name in ("SUPERVISOR_TOKEN", "HA_URL", "HA_TOKEN", "ALLOWED_CLIENT", "METRICS_FILE"):
    os.environ.pop(name, None)
