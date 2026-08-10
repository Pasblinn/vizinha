"""
Sound level measurement shared by the recorder and the standalone meter.

Two callers feed this module from different sources:
  - recorder.py    - raw A-law RTP payloads pulled straight off the RTSP proxy
  - decibel_meter.py - 16-bit PCM produced by ffmpeg

Both end up as linear samples, so the RMS -> dB math lives here once.

About the numbers: these are ESTIMATES. A calibrated SPL meter measures sound
pressure against a reference; a camera microphone reports whatever gain its
firmware applied. DECIBEL_MIC_OFFSET exists to shift the curve toward reality
for a given camera. Treat the readings as relative - "it got much louder at
23:40" is sound, "it was exactly 78 dB SPL" is not.
"""

import math
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

import config

# Full-scale value for 16-bit signed audio.
FULL_SCALE = 32768.0
# dBFS floor reported for digital silence.
SILENCE_DBFS = -96.0


def _build_alaw_table():
    """Precompute the 256-entry A-law -> linear PCM lookup table (ITU-T G.711)."""
    table = []
    for byte in range(256):
        value = byte ^ 0x55
        sign = -1 if value & 0x80 else 1
        value &= 0x7F
        segment = (value >> 4) & 0x07
        quant = value & 0x0F
        if segment == 0:
            sample = (quant << 4) + 8
        else:
            sample = ((quant << 4) + 8 + 256) << (segment - 1)
        table.append(sample * sign)
    return np.array(table, dtype=np.int16)


ALAW_TABLE = _build_alaw_table()


def alaw_to_linear(payload):
    """Decode A-law bytes into linear samples as float64."""
    return ALAW_TABLE[np.frombuffer(payload, dtype=np.uint8)].astype(np.float64)


def pcm16_to_linear(payload):
    """Read little-endian 16-bit PCM bytes as float64 samples."""
    return np.frombuffer(payload, dtype=np.int16).astype(np.float64)


def rms_to_dbfs(rms):
    """Convert an RMS amplitude to dBFS."""
    if rms <= 0:
        return SILENCE_DBFS
    return 20 * math.log10(rms / FULL_SCALE)


def samples_to_db(samples):
    """
    Convert linear samples to an estimated dB reading.

    dBFS runs from -96 (silence) to 0 (clipping); adding 96 maps that onto a
    0-96 scale, and DECIBEL_MIC_OFFSET compensates for the microphone.
    """
    if samples.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(samples**2))
    dbfs = rms_to_dbfs(rms)
    return round(max(0.0, dbfs + 96 + config.DECIBEL_MIC_OFFSET), 1)


class LevelTracker:
    """
    Thread-safe ring buffer of readings, with 60-second peak and average.

    The recorder writes from its network thread while the dashboard reads from
    Flask request threads, so every access takes the lock.
    """

    def __init__(self, maxlen=None):
        self._history = deque(maxlen=maxlen or config.DECIBEL_HISTORY_SIZE)
        self._current = {"value": 0.0, "timestamp": "", "peak": 0.0, "avg": 0.0}
        self._lock = threading.Lock()

    def record(self, db_value):
        """Add a reading and refresh the rolling stats. Returns the entry."""
        now = time.time()
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": db_value,
            "epoch": now,
        }

        with self._lock:
            self._history.append(entry)
            self._current["value"] = db_value
            self._current["timestamp"] = entry["timestamp"]

            recent = [e["db"] for e in self._history if now - e["epoch"] < 60]
            if recent:
                self._current["peak"] = max(recent)
                self._current["avg"] = round(sum(recent) / len(recent), 1)

        return entry

    def current(self):
        """Latest reading plus 60-second peak and average."""
        with self._lock:
            return dict(self._current)

    def history(self, seconds=300):
        """Readings from the last N seconds, oldest first."""
        cutoff = time.time() - seconds
        with self._lock:
            return [e for e in self._history if e["epoch"] > cutoff]

    def is_alert(self, db_value):
        """True when a reading crosses the configured alert threshold."""
        return db_value > config.DECIBEL_ALERT_THRESHOLD
