"""Confirm high-priority Exotic listing candidates ten minutes after a normal run.

Reads saved state first. It invokes the normal deterministic collector only when an
unconfirmed candidate adds a token absent from the prior verified baseline, or when
a currently listed token's price falls by 10% or more in the same currency.
Removals and non-discount price-only changes remain for the next main interval.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from exotic_execution_lock import BUSY, run_locked

STATE = Path(r"__HERMES_HOME__/price-watches/mcfarlane-exotics.json")
COLLECTOR = Path(r"__HERMES_HOME__/scripts/run_mcfarlane_exotic_deterministic.py")


def token_identity(listing: dict) -> str | None:
    token_id = listing.get("token_id")
    if isinstance(token_id, (str, int)) and str(token_id).strip():
        return f"token:{str(token_id).strip().lower()}"
    url = listing.get("url")
    return f"url:{url.strip().lower()}" if isinstance(url, str) and url.strip() else None


def is_priority_listing_candidate(collection: dict, fingerprint: str) -> bool:
    try:
        candidate = json.loads(fingerprint)
    except (TypeError, ValueError):
        return False
    if not isinstance(candidate, dict) or not candidate.get("for_sale"):
        return False
    prior = collection.get("baseline") or {}
    prior_by_id = {
        identity: item
        for item in prior.get("listings") or []
        if isinstance(item, dict)
        for identity in [token_identity(item)]
        if identity
    }
    candidate_by_id = {
        identity: item
        for item in candidate.get("listings") or []
        if isinstance(item, dict)
        for identity in [token_identity(item)]
        if identity
    }
    # A newly listed token is always purchase-priority.
    if set(candidate_by_id) - set(prior_by_id):
        return True
    # For an existing listing, only a same-currency price cut of at least 10%
    # is purchase-priority. Invalid/missing prices are deliberately ignored.
    for identity, current in candidate_by_id.items():
        previous = prior_by_id.get(identity)
        if not previous or current.get("currency") != previous.get("currency"):
            continue
        try:
            old_price = float(previous["price"])
            new_price = float(current["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if old_price > 0 and new_price <= old_price * 0.90:
            return True
    return False


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    candidates = state.get("pending_exotic_change_candidates") or {}
    priority = [
        collection["name"]
        for collection in state.get("collections") or []
        if collection.get("name") in candidates
        and is_priority_listing_candidate(collection, candidates[collection["name"]])
    ]
    if not priority:
        # Empty stdout intentionally produces no delivery from this no-agent job.
        return 0
    environment = os.environ.copy()
    environment["MCFARLANE_RUN_KIND"] = "confirmation"
    environment["MCFARLANE_COLLECTION_NAMES"] = "|".join(priority)
    result = subprocess.run([sys.executable, str(COLLECTOR)], env=environment, check=False)
    return result.returncode


def cron_entry() -> int:
    # A primary collector may still own the shared browser/state lease at the
    # confirmation minute. That is an intentional deferral, not an error.
    result = run_locked(main)
    return 0 if result == BUSY else result


if __name__ == "__main__":
    raise SystemExit(cron_entry())
