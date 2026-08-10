"""
Standalone sound level meter.

The recorder already measures levels from the RTSP stream it is relaying, at
no extra cost. This module is the separate answer to "how loud is it right
now?" when you are NOT recording: it opens its own stream through ffmpeg,
decodes the audio to PCM, and appends one JSON line per reading.

    python3 decibel_meter.py                 # measure until Ctrl+C
    python3 decibel_meter.py --duration 60   # measure for a minute

Readings are estimates, not calibrated SPL - see audio_level.py.
"""

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

import audio_level
import config

log = logging.getLogger(__name__)

levels = audio_level.LevelTracker()

BYTES_PER_SAMPLE = 2  # 16-bit PCM


def get_current_reading():
    """Latest reading plus 60-second peak and average."""
    return levels.current()


def get_history(seconds=300):
    """Readings from the last N seconds."""
    return levels.history(seconds)


def _ffmpeg_command():
    """ffmpeg invocation that turns the camera audio into raw mono PCM."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", config.rtsp_url(),
        "-vn",                                   # audio only
        "-acodec", "pcm_s16le",
        "-ar", str(config.DECIBEL_SAMPLE_RATE),
        "-ac", "1",                              # mono
        "-f", "s16le",
        "-loglevel", "error",
        "-",                                     # write to stdout
    ]


def _spawn_ffmpeg():
    return subprocess.Popen(
        _ffmpeg_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def log_measurement(db_value):
    """Record a reading in memory and append it to the decibel log."""
    entry = levels.record(db_value)

    log_path = Path(config.DECIBEL_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if levels.is_alert(db_value):
        log.warning(
            "Sound level alert: %s dB (threshold %s)",
            db_value,
            config.DECIBEL_ALERT_THRESHOLD,
        )
    return entry


def run_meter(duration=0):
    """
    Measure until `duration` seconds elapse (0 = forever).

    ffmpeg is restarted if the stream drops, which happens whenever the camera
    reboots or Wi-Fi hiccups.
    """
    log.info("Sound level meter starting - source %s", config.rtsp_url(redacted=True))

    process = _spawn_ffmpeg()
    running = {"value": True}

    def cleanup(signum=None, frame=None):
        running["value"] = False
        process.kill()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    chunk_size = int(
        config.DECIBEL_SAMPLE_RATE * config.DECIBEL_INTERVAL * BYTES_PER_SAMPLE
    )
    log.info(
        "Reading %d-byte chunks (%.1fs at %d Hz)",
        chunk_size,
        config.DECIBEL_INTERVAL,
        config.DECIBEL_SAMPLE_RATE,
    )

    started = time.time()
    last_report = 0.0

    try:
        while running["value"]:
            if duration and time.time() - started >= duration:
                break

            raw = process.stdout.read(chunk_size)
            if not raw:
                stderr = process.stderr.read().decode(errors="replace").strip()
                log.error("Audio stream ended%s", f": {stderr[-200:]}" if stderr else "")
                if not running["value"]:
                    break
                log.info("Restarting ffmpeg in 5s...")
                process.kill()
                time.sleep(5)
                process = _spawn_ffmpeg()
                continue

            db = audio_level.samples_to_db(audio_level.pcm16_to_linear(raw))
            log_measurement(db)

            # Report once every 10 seconds, on elapsed time rather than on the
            # wall clock (`int(time.time()) % 10` silently skips or repeats).
            now = time.time()
            if now - last_report >= 10:
                reading = levels.current()
                log.info(
                    "dB %.1f | peak(60s) %.1f | avg(60s) %.1f",
                    reading["value"],
                    reading["peak"],
                    reading["avg"],
                )
                last_report = now
    finally:
        process.kill()

    log.info("Meter stopped after %.0fs", time.time() - started)


def main():
    parser = argparse.ArgumentParser(description="Measure camera sound level")
    parser.add_argument(
        "--duration", type=int, default=0, help="seconds to measure (0 = until Ctrl+C)"
    )
    args = parser.parse_args()

    import log_setup

    log_setup.configure()

    try:
        run_meter(args.duration)
    except OSError as exc:
        log.error("Meter failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
