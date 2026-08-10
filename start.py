#!/usr/bin/env python3
"""
yoosee-nvr entry point.

Starts, in one process:
  1. storage rotation  - deletes old footage before the disk fills up
  2. cloud backup      - uploads finished segments through rclone
  3. scheduler         - runs the recorder inside the configured time window
  4. web dashboard     - status, live sound level, recordings list

Usage:
  python3 start.py                # normal operation
  python3 start.py --now          # record immediately, ignore the schedule
  python3 start.py --setup        # check the cloud backup remote
  python3 start.py --test         # record 30 seconds and exit
  python3 start.py --no-web       # no dashboard
"""

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

import config
import log_setup

log = None  # assigned in main(), after logging is configured


def _banner(args):
    window = "always (--now)" if args.now else scheduler_window()
    return f"""
  yoosee-nvr
  ----------------------------------------------------------
  Camera      {config.CAMERA_IP}:{config.CAMERA_PORT}
  Window      {window}
  Recordings  {config.RECORDING_DIR}
  Cloud       {"rclone -> " + config.RCLONE_REMOTE + ":" + config.CLOUD_FOLDER_NAME
               if config.CLOUD_ENABLED else "disabled"}
  Dashboard   http://{config.WEB_HOST}:{config.WEB_PORT}
  ----------------------------------------------------------
  Ctrl+C to stop
"""


def scheduler_window():
    import scheduler

    return scheduler.describe_window()


def start_storage_thread():
    import storage_manager

    t = threading.Thread(target=storage_manager.run_loop, daemon=True, name="storage")
    t.start()
    log.info("Storage rotation started (every %ss)", config.CLEANUP_INTERVAL)
    return t


def start_cloud_thread():
    if not config.CLOUD_ENABLED:
        log.info("Cloud backup disabled (CLOUD_ENABLED=false)")
        return None

    import cloud_uploader

    try:
        return cloud_uploader.start_upload_thread()
    except cloud_uploader.RcloneMissingError as exc:
        log.warning("%s", exc)
        log.warning("Continuing without cloud backup - run: python3 start.py --setup")
        return None


def start_scheduler_thread(force_record):
    import scheduler

    t = threading.Thread(
        target=scheduler.run_loop, args=(force_record,), daemon=True, name="scheduler"
    )
    t.start()
    log.info("Scheduler started (window %s)", scheduler.describe_window())
    return t


def run_dashboard():
    """Run the Flask dashboard. Blocks the calling thread."""
    from dashboard import app

    cert_file = Path(config.CERT_DIR) / "server.crt"
    key_file = Path(config.CERT_DIR) / "server.key"

    if cert_file.exists() and key_file.exists():
        log.info("Dashboard on https://%s:%s", config.WEB_HOST, config.WEB_PORT)
        app.run(
            host=config.WEB_HOST,
            port=config.WEB_PORT,
            debug=False,
            ssl_context=(str(cert_file), str(key_file)),
        )
        return

    if config.WEB_HOST not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Dashboard is bound to %s WITHOUT TLS - passwords cross the network "
            "in clear text. Generate certificates with scripts/harden.sh, or set "
            "WEB_HOST=127.0.0.1 and reach it over SSH/VPN.",
            config.WEB_HOST,
        )
    log.info("Dashboard on http://%s:%s", config.WEB_HOST, config.WEB_PORT)
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)


def run_test(seconds=30):
    """Record a short clip and exit - the fastest end-to-end check."""
    import recorder

    log.info("Test mode: recording %s seconds", seconds)
    rec = recorder.CameraRecorder()

    def stop_later():
        time.sleep(seconds)
        log.info("Test finished")
        rec.stop()

    threading.Thread(target=stop_later, daemon=True).start()
    rec.record()

    written = sorted(Path(config.RECORDING_DIR).rglob(f"*.{config.CONTAINER_FORMAT}"))
    if written:
        log.info("Wrote %d file(s), newest: %s", len(written), written[-1])
    else:
        log.error("No files were written - run `python3 rtsp_probe.py --describe`")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="yoosee-nvr")
    parser.add_argument("--now", action="store_true", help="record now, ignore schedule")
    parser.add_argument("--setup", action="store_true", help="check cloud backup remote")
    parser.add_argument("--test", action="store_true", help="record 30s and exit")
    parser.add_argument("--no-web", action="store_true", help="do not start the dashboard")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    log_setup.configure(verbose=args.verbose)

    global log
    import logging

    log = logging.getLogger("yoosee-nvr")

    if args.setup:
        import setup_cloud

        return setup_cloud.main()

    Path(config.RECORDING_DIR).mkdir(parents=True, exist_ok=True)

    if args.test:
        return run_test()

    def shutdown(signum=None, frame=None):
        import recorder

        log.info("Shutting down...")
        recorder.stop_recording()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(_banner(args))

    start_storage_thread()
    start_cloud_thread()
    start_scheduler_thread(args.now)

    if args.no_web:
        while True:
            time.sleep(60)
    else:
        run_dashboard()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
