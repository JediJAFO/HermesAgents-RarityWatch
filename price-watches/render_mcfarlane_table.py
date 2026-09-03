import json
from pathlib import Path
from mcfarlane_wallet_aliases import last_sale_text

STATE = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json")
TABLE = Path(r"C:/Users/jltfo/nft_exotic_watch.md")

state = json.loads(STATE.read_text(encoding="utf-8"))

def display_currency(value):
    return "POL" if value == "POLYGON" else value

lines = [
    "# McFarlane Exotic Watch",
    "",
    f"Last monitor run: {state.get('last_successful_monitor_run') or 'No complete global run recorded'}",
    "",
    "| Collection | For sale? | Buy-now listings | Current price(s) | Last successful update | Marketplace view | Last |",
    "|---|---:|---:|---|---|---|---|",
]

for collection in state["collections"]:
    baseline = collection.get("baseline") or {}
    listings = baseline.get("listings") or []
    for_sale = "Yes" if baseline.get("for_sale") else "No"
    count = baseline.get("listing_count", len(listings))
    prices = " — " if not listings else ", ".join(
        f"{item['price']:,} {display_currency(item.get('currency', state.get('currency', 'POLYGON')))}"
        for item in listings
    )
    sale = collection.get("last_exotic_sale")
    last_sale = last_sale_text(sale, state.get("currency", "POLYGON"))
    source_url = collection["source_url"]
    lines.append(
        f"| {collection['name']} | {for_sale} | {count} | {prices} | "
        f"{collection.get('last_successful_observation') or '—'} | "
        f"[view]({source_url}) | {last_sale} |"
    )

# One CRLF-terminated physical row per line for Discord-friendly Markdown output.
TABLE.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
print(f"Wrote {len(state['collections'])} CRLF-terminated table rows without token/item identities.")
