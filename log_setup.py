"""
Logging setup for yoosee-nvr.

Only entry points call `configure()`. Modules just do
`logging.getLogger(__name__)` and inherit the root configuration - calling
`basicConfig()` from a module is a no-op after the first caller wins, which
silently drops the handlers everybody else expected.
"""

import logging
import logging.handlers
from pathlib import Path

import config

_configured = False

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Keep 5 rotations of 10 MB. An unbounded log file on a Raspberry Pi with a
# 32 GB card is a slow-motion outage.
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def configure(verbose=False):
    """Configure root logging once, for the current process."""
    global _configured
    if _configured:
        return

    level = logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL)

    log_path = Path(config.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, stream_handler]

    # Flask's request log is noisy at INFO and says nothing we don't already log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _configured = True
