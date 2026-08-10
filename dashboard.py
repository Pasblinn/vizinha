"""
Web dashboard: recording status, live sound level, storage and cloud backup.

Security posture, in short:
  - every route except /login requires a session;
  - passwords are compared in constant time, and preferably as PBKDF2 hashes;
  - failed logins are rate limited per IP, with a lockout;
  - sessions expire, and the cookie is HttpOnly + SameSite=Lax;
  - the server binds to loopback by default (see WEB_HOST).

This is a home NVR, not a public service: expose it through the firewall rules
in scripts/harden.sh or a VPN, never straight onto the internet.
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import cloud_uploader
import config
import recorder
import storage_manager

log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Only send the cookie over HTTPS when TLS is actually in use, otherwise the
# browser would drop it and login would fail in a way that looks like a bug.
app.config["SESSION_COOKIE_SECURE"] = (
    Path(config.CERT_DIR, "server.crt").exists()
    and Path(config.CERT_DIR, "server.key").exists()
)
app.config["PERMANENT_SESSION_LIFETIME"] = config.SESSION_TIMEOUT

# {ip: {"count": int, "locked_until": float}}
_login_attempts = {}

PBKDF2_ITERATIONS = 260_000
PBKDF2_PREFIX = "pbkdf2_sha256"


# ============================================================
# AUTHENTICATION
# ============================================================
def hash_password(password, iterations=PBKDF2_ITERATIONS, salt=None):
    """Hash a password as `pbkdf2_sha256$iterations$salt$hash` (hex)."""
    import secrets

    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations
    ).hex()
    return f"{PBKDF2_PREFIX}${iterations}${salt}${digest}"


def _verify_hashed(password, stored_hash):
    """Check a password against a PBKDF2 hash, in constant time."""
    try:
        algorithm, iterations, salt, _ = stored_hash.split("$", 3)
    except ValueError:
        log.error("DASHBOARD_PASS_HASH is malformed - regenerate it")
        return False
    if algorithm != PBKDF2_PREFIX:
        log.error("Unsupported password hash algorithm: %s", algorithm)
        return False
    candidate = hash_password(password, int(iterations), salt)
    return hmac.compare_digest(candidate, stored_hash)


def _verify_plaintext(provided, expected):
    """Constant-time comparison for the plaintext fallback."""
    return hmac.compare_digest(provided.encode(), expected.encode())


def check_credentials(username, password):
    """Validate a login attempt against the configured credentials."""
    if not _verify_plaintext(username, config.DASHBOARD_USER):
        return False
    if config.DASHBOARD_PASS_HASH:
        return _verify_hashed(password, config.DASHBOARD_PASS_HASH)
    return _verify_plaintext(password, config.DASHBOARD_PASS)


def _is_locked_out(ip):
    """True while this IP is serving a lockout. Expired lockouts are cleared."""
    info = _login_attempts.get(ip)
    if not info:
        return False
    if info["locked_until"] > time.time():
        return True
    if info["locked_until"]:
        _login_attempts.pop(ip, None)  # lockout served, start fresh
    return False


def _record_failed_login(ip):
    info = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
    info["count"] += 1
    if info["count"] >= config.MAX_LOGIN_ATTEMPTS:
        info["locked_until"] = time.time() + config.LOGIN_LOCKOUT_SECONDS
        log.warning(
            "IP %s locked out for %ss after %d failed attempts",
            ip,
            config.LOGIN_LOCKOUT_SECONDS,
            info["count"],
        )


def _clear_failed_login(ip):
    _login_attempts.pop(ip, None)


def _wants_json():
    return request.is_json or request.path.startswith("/api/")


def login_required(f):
    """Require an authenticated, unexpired session."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return (
                (jsonify({"error": "unauthorized"}), 401)
                if _wants_json()
                else redirect(url_for("login"))
            )

        if time.time() - session.get("login_time", 0) > config.SESSION_TIMEOUT:
            session.clear()
            return (
                (jsonify({"error": "session_expired"}), 401)
                if _wants_json()
                else redirect(url_for("login"))
            )

        return f(*args, **kwargs)

    return decorated


# ============================================================
# ROUTES
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"

        if _is_locked_out(ip):
            remaining = int(_login_attempts[ip]["locked_until"] - time.time())
            error = f"Too many attempts. Try again in {remaining // 60 + 1} min."
        elif check_credentials(
            request.form.get("username", ""), request.form.get("password", "")
        ):
            _clear_failed_login(ip)
            session.clear()  # new session id on login
            session["authenticated"] = True
            session["login_time"] = time.time()
            session.permanent = True
            log.info("Login succeeded from %s", ip)
            return redirect(url_for("index"))
        else:
            _record_failed_login(ip)
            error = "Invalid username or password"
            log.warning("Login failed from %s", ip)

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "dashboard.html",
        threshold=config.DECIBEL_ALERT_THRESHOLD,
        start_hour=config.RECORDING_START_HOUR,
        end_hour=config.RECORDING_END_HOUR,
    )


@app.route("/api/storage")
@login_required
def api_storage():
    return jsonify(storage_manager.get_storage_stats())


@app.route("/api/decibel")
@login_required
def api_decibel():
    return jsonify(recorder.get_db_reading())


@app.route("/api/decibel/history")
@login_required
def api_decibel_history():
    try:
        seconds = int(request.args.get("seconds", 300))
    except ValueError:
        return jsonify({"error": "seconds must be an integer"}), 400
    seconds = max(1, min(seconds, config.DECIBEL_HISTORY_SIZE))
    return jsonify(recorder.get_db_history(seconds))


@app.route("/api/schedule")
@login_required
def api_schedule():
    import scheduler

    return jsonify(
        {
            "start_hour": config.RECORDING_START_HOUR,
            "end_hour": config.RECORDING_END_HOUR,
            "is_recording_hour": scheduler.is_recording_hour(),
            "status": recorder.recorder_status,
        }
    )


@app.route("/api/cloud")
@login_required
def api_cloud():
    return jsonify(cloud_uploader.get_upload_stats())


@app.route("/api/recordings")
@login_required
def api_recordings():
    rec_dir = Path(config.RECORDING_DIR)
    if not rec_dir.exists():
        return jsonify([])

    files = [f for f in rec_dir.rglob(f"*.{config.CONTAINER_FORMAT}") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    uploaded = cloud_uploader.read_ledger()
    result = []
    for f in files[:50]:
        key = f.relative_to(rec_dir).as_posix()
        result.append(
            {
                "name": key,
                "size_mb": round(f.stat().st_size / (1024**2), 1),
                "date": datetime.fromtimestamp(f.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "uploaded": key in uploaded,
            }
        )
    return jsonify(result)


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe - reports no private information."""
    return jsonify({"status": "ok"})
