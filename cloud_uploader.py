"""
Cloud backup of finished recordings, through rclone.

Why rclone instead of the Google Drive Python SDK: rclone already solves OAuth
refresh, resumable uploads and checksum verification, and the same code path
works for Drive, S3, Backblaze, Dropbox or a second NAS - the user picks the
remote, this module does not care.

Uploads are tracked in a `.uploaded` ledger inside the recordings folder, one
relative path per line, appended only after rclone reports success.
"""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

# A segment still being written must not be uploaded. ffmpeg closes a segment
# when it rolls over, so a file untouched for this long is finished.
QUIET_PERIOD_SECONDS = 30


class RcloneMissingError(RuntimeError):
    """rclone is not installed or not on PATH."""


def _remote_base():
    return f"{config.RCLONE_REMOTE}:{config.CLOUD_FOLDER_NAME}"


def ensure_rclone():
    """Raise unless the rclone binary is available."""
    if shutil.which("rclone") is None:
        raise RcloneMissingError(
            "rclone not found on PATH. Install it from https://rclone.org/install/ "
            "and configure a remote with: rclone config"
        )


def remote_configured():
    """True when the configured rclone remote exists."""
    try:
        ensure_rclone()
    except RcloneMissingError:
        return False
    result = subprocess.run(
        ["rclone", "listremotes"], capture_output=True, text=True, timeout=30
    )
    remotes = {line.rstrip(":") for line in result.stdout.split()}
    return config.RCLONE_REMOTE in remotes


def _ledger_path():
    return Path(config.RECORDING_DIR) / ".uploaded"


def read_ledger():
    """Set of relative paths already uploaded."""
    ledger = _ledger_path()
    if not ledger.exists():
        return set()
    return {line.strip() for line in ledger.read_text().splitlines() if line.strip()}


def _append_to_ledger(key):
    with open(_ledger_path(), "a") as f:
        f.write(key + "\n")


def upload_file(local_path):
    """
    Upload one recording, preserving its day folder on the remote.

    Returns True on success. rclone verifies the checksum after transfer, so a
    True here means the bytes on the remote match the bytes on disk.
    """
    ensure_rclone()
    local_path = Path(local_path)
    day_folder = local_path.parent.name  # e.g. "2026-04-06"
    remote_path = f"{_remote_base()}/{day_folder}/"

    log.info("Uploading %s -> %s", local_path.name, remote_path)
    started = time.time()

    try:
        result = subprocess.run(
            ["rclone", "copy", str(local_path), remote_path],
            capture_output=True,
            text=True,
            timeout=config.CLOUD_UPLOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "Upload timed out after %ss: %s (raise CLOUD_UPLOAD_TIMEOUT for slow links)",
            config.CLOUD_UPLOAD_TIMEOUT,
            local_path.name,
        )
        return False

    elapsed = time.time() - started
    if result.returncode == 0:
        size_mb = local_path.stat().st_size / (1024**2)
        speed = size_mb / elapsed if elapsed > 0 else 0
        log.info(
            "Uploaded %s (%.1f MB in %.0fs, %.1f MB/s)",
            local_path.name,
            size_mb,
            elapsed,
            speed,
        )
        return True

    log.error("Upload FAILED for %s: %s", local_path.name, result.stderr.strip()[-300:])
    return False


def upload_pending():
    """Upload every finished recording not yet in the ledger. Returns the count."""
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        return 0

    uploaded = read_ledger()
    count = 0

    for video in sorted(rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}")):
        key = str(video.relative_to(rec_dir))
        if key in uploaded:
            continue

        stat = video.stat()
        if time.time() - stat.st_mtime < QUIET_PERIOD_SECONDS:
            continue  # still being written by ffmpeg
        if stat.st_size == 0:
            continue

        if not upload_file(video):
            continue

        _append_to_ledger(key)
        uploaded.add(key)
        count += 1

        if config.CLOUD_DELETE_LOCAL_AFTER_UPLOAD:
            video.unlink()
            log.info("Local copy removed after upload: %s", video.name)

    if count:
        log.info("Cloud backup: %d file(s) uploaded", count)
    return count


def upload_loop():
    """Upload pending recordings forever, every CLOUD_UPLOAD_INTERVAL seconds."""
    while True:
        try:
            if config.CLOUD_ENABLED:
                upload_pending()
        except RcloneMissingError as exc:
            log.error("%s", exc)
            log.error("Cloud backup disabled until rclone is available")
            return
        except Exception as exc:  # keep the daemon alive across transient errors
            log.error("Upload loop error: %s", exc)
        time.sleep(config.CLOUD_UPLOAD_INTERVAL)


def start_upload_thread():
    """Start the background upload loop. Returns the thread."""
    ensure_rclone()
    t = threading.Thread(target=upload_loop, daemon=True, name="cloud-upload")
    t.start()
    log.info(
        "Cloud backup started (remote %s, every %ss)",
        _remote_base(),
        config.CLOUD_UPLOAD_INTERVAL,
    )
    return t


def get_upload_stats():
    """Upload summary consumed by the dashboard."""
    rec_dir = Path(config.RECORDING_DIR)
    uploaded_count = len(read_ledger())

    total_files = 0
    if rec_dir.exists():
        total_files = len(
            [
                f
                for f in rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}")
                if f.stat().st_size > 0
            ]
        )

    return {
        "uploaded": uploaded_count,
        "total": total_files,
        "pending": max(0, total_files - uploaded_count),
        "enabled": config.CLOUD_ENABLED,
        "remote": _remote_base(),
    }
