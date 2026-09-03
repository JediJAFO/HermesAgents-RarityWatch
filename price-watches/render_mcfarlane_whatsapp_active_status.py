import json
from pathlib import Path
from mcfarlane_wallet_aliases import last_sale_text, seller_label

STATE = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json")
OUTPUT = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-whatsapp-active-status.md")
state = json.loads(STATE.read_text(encoding="utf-8"))

def display_currency(value):
    return "POL" if value == "POLYGON" else value

active = [c for c in state["collections"] if (c.get("baseline") or {}).get("for_sale")]
lines = [
    "*McFarlane Exotic Watch — active listings (saved data)*",
    f"Last checked: {state.get('last_successful_monitor_run') or '—'}",
    "",
]
for c in active:
    baseline = c["baseline"]
    listings = baseline.get("listings") or []
    count = baseline.get("listing_count", len(listings))
    prices = ", ".join(
        f"{x['price']:,} {display_currency(x.get('currency', state.get('currency', 'POLYGON')))} by {seller_label(x)}"
        for x in listings
    ) or "—"
    sale = c.get("last_exotic_sale")
    last = last_sale_text(sale, state.get("currency", "POLYGON"))
    lines.append(
        f"• *{c['name']}* — *🟢 YES* | {count} listing(s) | *🟢 {prices}* | Last: {last}"
    )

if not active:
    lines.append("No active Exotic buy-now listings.")

OUTPUT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
print(f"Wrote {len(active)} active WhatsApp collection lines.")
