"""Refresh the current POL/USD spot rate for Exotic listing display.

Failures preserve the prior verified rate and never block the marketplace collector.
"""
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(r"__HERMES_HOME__/price-watches/mcfarlane-exotics.json")
URL = "https://api.coingecko.com/api/v3/simple/price?ids=polygon-ecosystem-token&vs_currencies=usd"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def atomic(state):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=STATE.parent, suffix=".tmp") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temp = file.name
    os.replace(temp, STATE)


state = json.loads(STATE.read_text(encoding="utf-8"))
try:
    with urllib.request.urlopen(urllib.request.Request(URL, headers=HEADERS), timeout=30) as response:
        data = json.load(response)
    rate = float(data["polygon-ecosystem-token"]["usd"])
    if rate <= 0:
        raise ValueError("non-positive POL/USD value")
except Exception as error:
    print(f"POL/USD refresh unavailable; retained prior saved rate: {error}")
    raise SystemExit(0)
state["current_pol_usd"] = {
    "rate": rate,
    "source": "CoinGecko simple price — current POL/USD spot",
    "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
atomic(state)
print(f"POL/USD refreshed: {rate}")
