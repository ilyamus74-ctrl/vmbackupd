from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

RECLAIM_DELETE_COMMAND = "vmbackupd-reclaim-delete-v1"

def run_receiver_reclaim_delete() -> int:
    payload = json.loads(
        os.environ.get("VMBACKUPD_RECLAIM_DELETE", "{}")
    )
    target = (
        Path("/srv/vmbackupd")
        / "storages"
        / payload["storage_id"]
        / "published"
        / payload["bundle_object_id"]
    )
    if target.exists():
        shutil.rmtree(target)
    return 0
