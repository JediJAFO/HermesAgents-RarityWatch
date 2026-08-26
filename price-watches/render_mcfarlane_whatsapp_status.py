import json
from pathlib import Path

STATE = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json")
OUTPUT = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-whatsapp-status.md")
state = json.loads(STATE.read_text(encoding="utf-8"))

def display_currency(value):
    return "POL" if value == "POLYGON" else value

lines = [
    "*McFarlane Exotic Watch — saved data (no refresh)*",
    f"Last checked: {state.get('last_successful_monitor_run') or '—'}",
    "",
]
for c in state["collections"]:
    baseline = c.get("baseline") or {}
    listings = baseline.get("listings") or []
    for_sale = bool(baseline.get("for_sale"))
    count = baseline.get("listing_count", len(listings))
    prices = "—" if not listings else ", ".join(
        f"{x['price']:,} {display_currency(x.get('currency', state.get('currency', 'POLYGON')))}" for x in listings
    )
    sale = c.get("last_exotic_sale")
    last_sale = (
        f"{sale['price']:,} {display_currency(sale.get('currency', state.get('currency', 'POLYGON')))}, {sale['activity_time']}"
        if sale else "Not yet verified"
    )
    if for_sale:
        availability = "*🟢 YES*"
        price_text = f"*🟢 {prices}*"
        detail = f"{availability} | {count} listing(s) | {price_text}"
    else:
        detail = f"No | 0 listing(s) | —"
    # WhatsApp exposes inline URLs in full, so source URLs stay internal.
    lines.append(f"• *{c['name']}* — {detail} | Last: {last_sale}")

# CRLF physical lines; no NFT item names, token IDs, or URLs.
OUTPUT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
print(f"Wrote WhatsApp status for {len(state['collections'])} collections.")
