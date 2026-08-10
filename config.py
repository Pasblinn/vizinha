"""
Central configuration for yoosee-nvr.

Every setting can be overridden through an environment variable or a `.env`
file placed next to this module. Secrets (camera password, dashboard password,
Flask secret key) have NO default value on purpose: the process refuses to
start rather than run with a password that is public knowledge in this repo.

See `.env.example` for a documented template.
"""

import os
import secrets
import stat
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# .env LOADING
# ============================================================
def _load_env_file(path):
    """Load KEY=VALUE pairs from a .env file without overriding real env vars."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Real environment variables win over the file, so systemd/Docker can
        # override a checked-out .env without editing it.
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


# ============================================================
# HELPERS
# ============================================================
class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _require(name, hint=""):
    """Read a mandatory setting, failing loudly when it is absent."""
    value = os.environ.get(name, "").strip()
    if not value:
        suffix = f" {hint}" if hint else ""
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in.{suffix}"
        )
    return value


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}")


def _env_float(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}")


def _env_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {value!r}")


def _env_path(name, default):
    """Resolve a path setting, expanding ~ and making it absolute."""
    value = os.environ.get(name, "").strip()
    path = Path(value).expanduser() if value else Path(default)
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


# ============================================================
# TIMEZONE
# ============================================================
# Recording windows are wall-clock ("record from 21:00 to 07:00"), so the
# process must agree with the user's local time, not the server's UTC.
TIMEZONE = os.environ.get("TZ", "").strip() or "America/Sao_Paulo"
os.environ["TZ"] = TIMEZONE
try:
    import time as _time

    _time.tzset()
except AttributeError:
    pass  # Windows has no tzset()


# ============================================================
# CAMERA
# ============================================================
CAMERA_IP = _require("CAMERA_IP", "Example: CAMERA_IP=192.168.1.50")
CAMERA_USER = os.environ.get("CAMERA_USER", "admin").strip()
CAMERA_PASS = _require("CAMERA_PASS", "This is the password set in the Yoosee app.")
CAMERA_PORT = _env_int("CAMERA_PORT", 554)
CAMERA_PATH = os.environ.get("CAMERA_PATH", "/onvif1").strip()

# Local RTSP proxy that fixes the camera's malformed Transport header.
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1").strip()
PROXY_PORT = _env_int("PROXY_PORT", 8554)


def rtsp_url(host=None, port=None, redacted=False):
    """
    Build the RTSP URL for the camera (or for the local proxy).

    `redacted=True` replaces the password with `***`, which is what every log
    line and error message must use - credentials never reach the log file.
    """
    password = "***" if redacted else CAMERA_PASS
    host = host or CAMERA_IP
    port = port if port is not None else CAMERA_PORT
    return f"rtsp://{CAMERA_USER}:{password}@{host}:{port}{CAMERA_PATH}"


def proxy_rtsp_url(redacted=False):
    """RTSP URL pointing at the local proxy rather than the camera."""
    return rtsp_url(host=PROXY_HOST, port=PROXY_PORT, redacted=redacted)


# ============================================================
# RECORDING
# ============================================================
RECORDING_DIR = _env_path("RECORDING_DIR", "recordings")
SEGMENT_DURATION = _env_int("SEGMENT_DURATION", 600)  # seconds per file
CONTAINER_FORMAT = os.environ.get("CONTAINER_FORMAT", "mp4").strip()

# 24h clock. A start hour greater than the end hour means the window crosses
# midnight (the default 21 -> 7 records overnight).
RECORDING_START_HOUR = _env_int("RECORDING_START_HOUR", 21)
RECORDING_END_HOUR = _env_int("RECORDING_END_HOUR", 7)

for _hour_name, _hour in (
    ("RECORDING_START_HOUR", RECORDING_START_HOUR),
    ("RECORDING_END_HOUR", RECORDING_END_HOUR),
):
    if not 0 <= _hour <= 23:
        raise ConfigError(f"{_hour_name} must be between 0 and 23, got {_hour}")


# ============================================================
# CLOUD BACKUP (rclone remote - Google Drive, S3, Dropbox, ...)
# ============================================================
CLOUD_ENABLED = _env_bool("CLOUD_ENABLED", True)
# Name of the rclone remote created by `rclone config` (see docs/setup).
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive").strip()
CLOUD_FOLDER_NAME = os.environ.get("CLOUD_FOLDER_NAME", "yoosee-nvr").strip()
CLOUD_UPLOAD_INTERVAL = _env_int("CLOUD_UPLOAD_INTERVAL", 60)  # seconds
CLOUD_UPLOAD_TIMEOUT = _env_int("CLOUD_UPLOAD_TIMEOUT", 300)  # seconds per file
# Free local disk as soon as a segment is safely in the cloud.
CLOUD_DELETE_LOCAL_AFTER_UPLOAD = _env_bool("CLOUD_DELETE_LOCAL_AFTER_UPLOAD", False)


# ============================================================
# LOCAL STORAGE ROTATION
# ============================================================
MAX_STORAGE_GB = _env_float("MAX_STORAGE_GB", 50)
MIN_FREE_DISK_GB = _env_float("MIN_FREE_DISK_GB", 5)
RETENTION_DAYS = _env_int("RETENTION_DAYS", 7)
CLEANUP_INTERVAL = _env_int("CLEANUP_INTERVAL", 3600)  # seconds


# ============================================================
# SOUND LEVEL METER
# ============================================================
DECIBEL_SAMPLE_RATE = _env_int("DECIBEL_SAMPLE_RATE", 16000)
DECIBEL_INTERVAL = _env_float("DECIBEL_INTERVAL", 1.0)  # seconds between readings
DECIBEL_ALERT_THRESHOLD = _env_float("DECIBEL_ALERT_THRESHOLD", 85)
DECIBEL_HISTORY_SIZE = _env_int("DECIBEL_HISTORY_SIZE", 3600)  # ~1h at 1/s
# Cheap camera microphones read high; this offset compensates. Readings are an
# uncalibrated estimate, never a substitute for a real SPL meter.
DECIBEL_MIC_OFFSET = _env_float("DECIBEL_MIC_OFFSET", -25)
DECIBEL_LOG_FILE = _env_path("DECIBEL_LOG_FILE", "decibels.log")


# ============================================================
# WEB DASHBOARD
# ============================================================
# Defaults to loopback. Binding to 0.0.0.0 exposes the dashboard to the whole
# network and is only safe behind the firewall rules in scripts/harden.sh.
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1").strip()
WEB_PORT = _env_int("WEB_PORT", 9847)

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
# Either a PBKDF2 hash (preferred, generate with scripts/hash_password.py) or a
# plaintext password. Exactly one of the two must be set.
DASHBOARD_PASS_HASH = os.environ.get("DASHBOARD_PASS_HASH", "").strip()
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "").strip()
if not DASHBOARD_PASS_HASH and not DASHBOARD_PASS:
    raise ConfigError(
        "Set DASHBOARD_PASS_HASH (preferred) or DASHBOARD_PASS. "
        "Generate a hash with: python3 scripts/hash_password.py"
    )

SESSION_TIMEOUT = _env_int("SESSION_TIMEOUT", 3600)  # seconds
MAX_LOGIN_ATTEMPTS = _env_int("MAX_LOGIN_ATTEMPTS", 5)
LOGIN_LOCKOUT_SECONDS = _env_int("LOGIN_LOCKOUT_SECONDS", 900)

CERT_DIR = _env_path("CERT_DIR", "certs")


def _load_or_create_secret_key():
    """
    Return the Flask session signing key.

    A random key generated per process would silently log everybody out on
    every restart, so when FLASK_SECRET_KEY is unset the key is generated once
    and persisted with 0600 permissions.
    """
    from_env = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if from_env:
        return from_env

    key_file = BASE_DIR / ".flask_secret_key"
    if key_file.exists():
        stored = key_file.read_text().strip()
        if stored:
            return stored

    key = secrets.token_hex(32)
    key_file.write_text(key)
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return key


FLASK_SECRET_KEY = _load_or_create_secret_key()


# ============================================================
# LOGGING
# ============================================================
LOG_FILE = _env_path("LOG_FILE", "yoosee-nvr.log")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    raise ConfigError(f"LOG_LEVEL must be a Python log level, got {LOG_LEVEL!r}")
