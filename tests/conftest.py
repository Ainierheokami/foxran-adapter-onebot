"""Make the optional host package location explicit for source checkouts."""

import os
import sys
from pathlib import Path


host_root = os.environ.get("FOXRAN_HOST_ROOT", "").strip()
if host_root:
    resolved = Path(host_root).expanduser().resolve()
    if (resolved / "app").is_dir():
        sys.path.insert(0, str(resolved))
