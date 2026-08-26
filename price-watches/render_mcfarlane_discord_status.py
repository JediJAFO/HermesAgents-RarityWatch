import json
from pathlib import Path

STATE = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-exotics.json")
OUTPUT = Path(r"C:/Users/jltfo/AppData/Local/hermes/price-watches/mcfarlane-discord-status.md")
state = json.loads(STATE.read_text(encoding="utf-8"))

def display_currency(value):
    return "POL" if value == "POLYGON" else value

lines = [
    "**McFarlane Exotic Watch — saved data (no refresh)**",
    f"Last checked: {state.get('last_successful_monitor_run') or '—'}",
    "",
]
for c in state["collections"]:
    baseline = c.get("baseline") or {}
    listings = baseline.get("listings") or []
    status = "For sale" if baseline.get("for_sale") else "Not for sale"
    count = baseline.get("listing_count", len(listings))
    prices = "—" if not listings else ", ".join(
        f"{x['price']:,} {display_currency(x.get('currency', state.get('currency', 'POLYGON')))}" for x in listings
    )
    sale = c.get("last_exotic_sale")
    last_sale = (
        f"{sale['price']:,} {display_currency(sale.get('currency', state.get('currency', 'POLYGON')))}, {sale['activity_time']}"
        if sale else "Not yet verified"
    )
    if baseline.get("for_sale"):
        # Discord supports bold but not arbitrary text color in normal messages;
        # the green indicator makes active rows stand out without an embed.
        active_status = "**🟢 YES**"
        active_price = f"**🟢 {prices}**"
        lines.append(
            f"• **{c['name']}** — {active_status}; {count} listing(s); {active_price}; "
            f"last: {last_sale}; [View](<{c['source_url']}>)"
        )
    else:
        lines.append(f"• **{c['name']}** — No; {count} listing(s); {prices}; last: {last_sale}")

# Discord-native list: exactly one CRLF-terminated collection line; angle-bracket View links do not embed.
OUTPUT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
print(f"Wrote {len(state['collections'])} Discord-native collection lines.")
