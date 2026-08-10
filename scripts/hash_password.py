#!/usr/bin/env python3
"""
Generate a PBKDF2 hash for the dashboard password.

    python3 scripts/hash_password.py
    # paste the output into .env as DASHBOARD_PASS_HASH=...

Storing the hash instead of the plaintext means a leaked .env (or a backup of
it) does not immediately hand over the dashboard.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    # Import late: dashboard.py pulls in config, which needs a usable .env.
    from dashboard import hash_password

    password = getpass.getpass("Dashboard password: ")
    if not password:
        print("Empty password, aborting.", file=sys.stderr)
        return 1

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1

    print("\nAdd this line to your .env file:\n")
    print(f"DASHBOARD_PASS_HASH={hash_password(password)}")
    print("\nThen remove any DASHBOARD_PASS line.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
