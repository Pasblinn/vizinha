"""
Local storage rotation.

Two independent rules delete old footage:
  1. Age  - anything older than RETENTION_DAYS.
  2. Space - oldest segments first, until the recordings folder is under
     MAX_STORAGE_GB *and* the disk has more than MIN_FREE_DISK_GB free.

Rule 2 exists because rule 1 alone cannot save a disk that fills up faster
than the retention window.
"""

import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

log = logging.getLogger(__name__)


def get_recordings_size_gb():
    """Total size of the recordings folder, in GB."""
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        return 0.0
    total = sum(f.stat().st_size for f in rec_dir.rglob("*") if f.is_file())
    return total / (1024**3)


def get_free_disk_gb():
    """Free space on the filesystem holding the recordings, in GB."""
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        rec_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(rec_dir).free / (1024**3)


def get_oldest_recordings(limit=50):
    """Oldest recordings first, by modification time."""
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        return []
    files = [f for f in rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime)
    return files[:limit]


def _is_pending_upload(path, uploaded):
    """True when cloud backup is on and this file has not been uploaded yet."""
    if not config.CLOUD_ENABLED:
        return False
    rec_dir = Path(config.RECORDING_DIR)
    try:
        key = str(path.relative_to(rec_dir))
    except ValueError:
        return False
    return key not in uploaded


def _uploaded_keys():
    """Read the set of files already backed up, as recorded by the uploader."""
    ledger = Path(config.RECORDING_DIR) / ".uploaded"
    if not ledger.exists():
        return set()
    return {line for line in ledger.read_text().splitlines() if line.strip()}


def delete_old_by_retention():
    """Delete recordings older than the retention window."""
    cutoff = datetime.now() - timedelta(days=config.RETENTION_DAYS)
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        return 0

    uploaded = _uploaded_keys()
    deleted = 0

    for f in rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}"):
        if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
            continue
        if _is_pending_upload(f, uploaded):
            # Deleting footage that never reached the cloud would lose it for
            # good. Retention waits; the space rule may still force the issue.
            log.warning("Past retention but not yet uploaded, keeping: %s", f.name)
            continue
        size_mb = f.stat().st_size / (1024**2)
        log.info(
            "Deleting (retention %sd): %s (%.1f MB)", config.RETENTION_DAYS, f.name, size_mb
        )
        f.unlink()
        deleted += 1

    _remove_empty_day_folders(rec_dir)
    return deleted


def delete_oldest_to_free_space():
    """Delete oldest recordings until the size and free-space limits are met."""
    deleted = 0

    while True:
        size_gb = get_recordings_size_gb()
        free_gb = get_free_disk_gb()

        over_quota = size_gb > config.MAX_STORAGE_GB
        disk_low = free_gb < config.MIN_FREE_DISK_GB
        if not (over_quota or disk_low):
            break

        oldest = get_oldest_recordings(1)
        if not oldest:
            log.warning(
                "Storage still tight (%.1f GB used, %.1f GB free) but no recordings left to delete",
                size_gb,
                free_gb,
            )
            break

        f = oldest[0]
        size_mb = f.stat().st_size / (1024**2)
        log.info(
            "Deleting (space): %s (%.1f MB) | used %.1f GB | free %.1f GB",
            f.name,
            size_mb,
            size_gb,
            free_gb,
        )
        f.unlink()
        deleted += 1

    if deleted:
        _remove_empty_day_folders(Path(config.RECORDING_DIR))
    return deleted


def _remove_empty_day_folders(rec_dir):
    """Remove day folders left empty after a deletion pass."""
    for d in rec_dir.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            log.info("Removed empty folder: %s", d.name)


def cleanup():
    """Run one full rotation pass. Returns the number of files deleted."""
    size_gb = get_recordings_size_gb()
    free_gb = get_free_disk_gb()
    log.info("Storage check: %.2f GB recorded | %.2f GB free on disk", size_gb, free_gb)

    deleted = delete_old_by_retention() + delete_oldest_to_free_space()

    if deleted:
        log.info(
            "Cleanup done: %d file(s) deleted | now %.2f GB",
            deleted,
            get_recordings_size_gb(),
        )
    return deleted


def get_storage_stats():
    """Storage summary consumed by the dashboard."""
    rec_dir = Path(config.RECORDING_DIR)
    file_count = 0
    if rec_dir.exists():
        file_count = len(list(rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}")))

    return {
        "used_gb": round(get_recordings_size_gb(), 2),
        "max_gb": config.MAX_STORAGE_GB,
        "free_disk_gb": round(get_free_disk_gb(), 2),
        "file_count": file_count,
        "retention_days": config.RETENTION_DAYS,
    }


def run_loop():
    """Run cleanup forever, every CLEANUP_INTERVAL seconds."""
    while True:
        try:
            cleanup()
        except Exception as exc:  # keep the daemon alive across transient errors
            log.error("Cleanup failed: %s", exc)
        time.sleep(config.CLEANUP_INTERVAL)


if __name__ == "__main__":
    import log_setup

    log_setup.configure()
    cleanup()
