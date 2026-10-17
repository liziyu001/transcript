"""Remove only Studio-owned media after its durable upload checkpoint."""

import json
import logging
import shutil
from pathlib import Path


def remove_uploaded_audio(item_root):
    item_root = Path(item_root)
    checkpoint = item_root / "checkpoint.json"
    if not checkpoint.exists() or not json.loads(checkpoint.read_text(encoding="utf-8")).get(
        "uploaded"
    ):
        return False
    audio = item_root / "audio"
    if not audio.exists():
        return False
    # Never follow a replaced directory to user files or another task's cache.
    if (
        item_root.is_symlink()
        or audio.is_symlink()
        or audio.resolve() != item_root.resolve() / "audio"
    ):
        raise ValueError("拒绝清理工作目录以外的音频路径。")
    shutil.rmtree(audio)
    return True


def sweep_uploaded_audio(store):
    """Recover a crash after checkpointing upload but before deleting local media."""
    removed = 0
    for job in store.jobs():
        if job["kind"] not in {"download", "pipeline"} or job.get("dry_run"):
            continue
        for item in job["items"]:
            root = store.root / "items" / item["id"]
            if root.resolve().parent != (store.root / "items").resolve():
                continue
            try:
                removed += remove_uploaded_audio(root)
            except (OSError, ValueError):
                logging.getLogger(__name__).warning(
                    "Local audio cleanup pending for item %s", item["id"]
                )
    return removed
