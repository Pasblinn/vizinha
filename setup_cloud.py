#!/usr/bin/env python3
"""
Interactive check for the cloud backup remote.

This does not reimplement `rclone config` - it verifies that the remote the
rest of the app expects actually exists and can be written to, and prints the
exact commands to fix it when it cannot.

Run with: python3 start.py --setup
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import config
import cloud_uploader


def _print_header():
    print("=" * 60)
    print("  yoosee-nvr - cloud backup setup")
    print("=" * 60)
    print()


def _print_install_instructions():
    print("rclone is not installed (or not on PATH).\n")
    print("Install it:")
    print("  curl https://rclone.org/install.sh | sudo bash")
    print("  # or: sudo apt install rclone")
    print()


def _print_remote_instructions():
    remote = config.RCLONE_REMOTE
    print(f"The rclone remote {remote!r} does not exist yet.\n")
    print("Create it:")
    print("  rclone config")
    print("    n) New remote")
    print(f"    name> {remote}")
    print("    Storage> drive          (Google Drive)")
    print("    client_id>              (press Enter to use rclone's default)")
    print("    client_secret>          (press Enter)")
    print("    scope> 1                (full access)")
    print("    Edit advanced config? n")
    print("    Use auto config? y      (opens a browser to authorize)")
    print()
    print("On a headless box answer 'n' to auto config and follow the")
    print("`rclone authorize drive` instructions it prints.")
    print()
    print(f"Then run this check again: python3 start.py --setup")
    print()


def _write_probe():
    """Upload and delete a small probe file to prove write access."""
    remote_dir = f"{config.RCLONE_REMOTE}:{config.CLOUD_FOLDER_NAME}"

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / ".yoosee-nvr-write-test"
        probe.write_text("yoosee-nvr write test\n")

        up = subprocess.run(
            ["rclone", "copy", str(probe), f"{remote_dir}/"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if up.returncode != 0:
            return False, up.stderr.strip()[-300:]

        subprocess.run(
            ["rclone", "delete", f"{remote_dir}/{probe.name}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    return True, ""


def main():
    _print_header()

    try:
        cloud_uploader.ensure_rclone()
    except cloud_uploader.RcloneMissingError:
        _print_install_instructions()
        return 1

    version = subprocess.run(
        ["rclone", "version"], capture_output=True, text=True, timeout=30
    )
    print(f"rclone found: {version.stdout.splitlines()[0]}")

    if not cloud_uploader.remote_configured():
        _print_remote_instructions()
        return 1

    print(f"Remote found:  {config.RCLONE_REMOTE}:")
    print(f"Target folder: {config.CLOUD_FOLDER_NAME}/<date>/")
    print()
    print("Testing write access...")

    ok, error = _write_probe()
    if not ok:
        print(f"\nWrite test FAILED: {error}")
        print("\nCheck that the remote has write permission and enough quota.")
        return 1

    print("Write test OK.\n")
    print("Cloud backup is ready. Recordings will be uploaded to:")
    print(f"  {config.RCLONE_REMOTE}:{config.CLOUD_FOLDER_NAME}/<date>/")
    return 0


if __name__ == "__main__":
    import log_setup

    log_setup.configure()
    sys.exit(main())
