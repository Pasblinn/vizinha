"""
Recording window scheduler.

Decides when the recorder should be running, based on a wall-clock window that
may cross midnight (the default 21:00 -> 07:00 records overnight).
"""

import logging
import time
from datetime import datetime

import config
import recorder

log = logging.getLogger(__name__)

# How often the scheduler re-evaluates the window.
POLL_INTERVAL = 30


def is_recording_hour(now=None):
    """
    True when the current time falls inside the recording window.

    A start hour greater than the end hour means the window wraps past
    midnight, so the test becomes "after start OR before end".
    """
    hour = (now or datetime.now()).hour
    start = config.RECORDING_START_HOUR
    end = config.RECORDING_END_HOUR

    if start == end:
        return True  # a zero-width window means "always on"
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def describe_window():
    """Human-readable recording window, e.g. '21:00 - 07:00'."""
    return f"{config.RECORDING_START_HOUR:02d}:00 - {config.RECORDING_END_HOUR:02d}:00"


def run_loop(force_record=False):
    """
    Start and stop the recorder as the window opens and closes.

    `force_record=True` (the --now flag) ignores the schedule entirely.
    """
    is_recording = False

    while True:
        should_record = force_record or is_recording_hour()

        if should_record and not is_recording:
            log.info("Entering recording window (%s) - starting", describe_window())
            recorder.start_recording_thread()
            is_recording = True

        elif not should_record and is_recording:
            log.info("Leaving recording window (%s) - stopping", describe_window())
            recorder.stop_recording()
            is_recording = False

        time.sleep(POLL_INTERVAL)
