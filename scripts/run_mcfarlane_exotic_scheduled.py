"""Cron-only entrypoint that marks the verified scheduled context."""
import os

from exotic_execution_lock import run_locked
from run_mcfarlane_exotic_deterministic import main as collector_main


def main() -> int:
    previous = os.environ.get("HERMES_CRON_SESSION")
    os.environ["HERMES_CRON_SESSION"] = "1"
    try:
        return run_locked(collector_main)
    finally:
        if previous is None:
            os.environ.pop("HERMES_CRON_SESSION", None)
        else:
            os.environ["HERMES_CRON_SESSION"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
