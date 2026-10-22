import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ALIASES = Path(r"__HERMES_HOME__/price-watches/mcfarlane-wallet-aliases.json")
DISPLAY_SUFFIX_LENGTH = 9


def normalize_wallet(wallet: object) -> str | None:
    if not isinstance(wallet, str) or not wallet.strip():
        return None
    normalized = wallet.split(":", 1)[-1].lower().strip()
    if not normalized.startswith("0x") or len(normalized) != 42:
        return None
    return normalized


def alias_for_wallet(wallet: object, aliases: dict | None = None) -> str | None:
    """Resolve schema-3 canonical aliases first, then legacy views."""
    normalized = normalize_wallet(wallet)
    if not normalized:
        return None
    if aliases is None:
        try:
            aliases = json.loads(ALIASES.read_text(encoding="utf-8"))
        except Exception:
            aliases = {}
    canonical = aliases.get("aliases", []) if isinstance(aliases, dict) else []
    exact_matches = {
        row.get("alias") for row in canonical
        if isinstance(row, dict)
        and isinstance(row.get("wallet"), str)
        and row["wallet"].lower() == normalized
        and isinstance(row.get("alias"), str)
        and row.get("alias").strip()
    }
    if len(exact_matches) == 1:
        return next(iter(exact_matches))
    if len(exact_matches) > 1:
        return None
    exact = aliases.get("exact_aliases", {}) if isinstance(aliases, dict) else {}
    if isinstance(exact, dict):
        for address, alias in exact.items():
            if str(address).lower() == normalized and isinstance(alias, str) and alias.strip():
                return alias.strip()
    direct = aliases.get(normalized) if isinstance(aliases, dict) else None
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    canonical_masks = {
        row.get("alias") for row in canonical
        if isinstance(row, dict) and not row.get("wallet")
        and isinstance(row.get("masked_prefix"), str)
        and isinstance(row.get("masked_suffix"), str)
        and normalized.startswith(row["masked_prefix"].lower())
        and normalized.endswith(row["masked_suffix"].lower())
        and isinstance(row.get("alias"), str) and row.get("alias").strip()
    }
    if len(canonical_masks) == 1:
        return next(iter(canonical_masks))
    if len(canonical_masks) > 1:
        return None
    legacy_masks = {
        row.get("alias", row.get("name_tag"))
        for row in aliases.get("masked_aliases", [])
        if isinstance(row, dict)
        and isinstance(row.get("masked_prefix", row.get("prefix")), str)
        and isinstance(row.get("masked_suffix", row.get("suffix")), str)
        and normalized.startswith(row.get("masked_prefix", row.get("prefix")).lower())
        and normalized.endswith(row.get("masked_suffix", row.get("suffix")).lower())
        and isinstance(row.get("alias", row.get("name_tag")), str)
        and row.get("alias", row.get("name_tag")).strip()
    }
    return next(iter(legacy_masks)) if len(legacy_masks) == 1 else None


def wallet_label(wallet: object, aliases: dict | None = None, missing: str = "Buyer not recorded") -> str:
    normalized = normalize_wallet(wallet)
    if not normalized:
        return missing
    return alias_for_wallet(normalized, aliases) or "..." + normalized[-DISPLAY_SUFFIX_LENGTH:]


def wallet_display(wallet: object) -> str:
    """Render an unmatched wallet as an ellipsis plus a fixed suffix only."""
    if not isinstance(wallet, str) or not wallet.strip():
        return "Buyer not recorded"
    normalized = wallet.split(":", 1)[-1].lower().strip()
    return "..." + normalized[-DISPLAY_SUFFIX_LENGTH:]


def resolve_buyer(wallet: object, aliases: dict | None = None) -> dict:
    """Keep the raw normalized wallet and a Name Tag only when unique."""
    if not isinstance(wallet, str) or not wallet.strip():
        return {"buyer_wallet": None, "buyer_name_tag": None}
    normalized = normalize_wallet(wallet)
    if not normalized:
        return {"buyer_wallet": None, "buyer_name_tag": None}
    return {"buyer_wallet": normalized, "buyer_name_tag": alias_for_wallet(normalized, aliases)}


def party_label(record: dict | None, party: str) -> str:
    """Prefer the current shared alias; fall back to saved labels and suffix."""
    if not isinstance(record, dict):
        return f"{party.title()} not recorded"
    resolved = resolve_buyer(record.get(f"{party}_wallet") or record.get(party))
    if resolved["buyer_name_tag"]:
        return resolved["buyer_name_tag"]
    raw = record.get(f"{party}_display") or record.get(f"{party}_name_tag")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if not resolved["buyer_wallet"]:
        return f"{party.title()} not recorded"
    return wallet_display(resolved["buyer_wallet"])


def buyer_label(sale: dict | None) -> str:
    return party_label(sale, "buyer")


def seller_label(listing: dict | None) -> str:
    return party_label(listing, "seller")


def sale_time_text(sale: dict | None) -> str:
    """Render an authoritative sale timestamp like a listing set-time label."""
    if not isinstance(sale, dict):
        return "time not recorded"
    raw = sale.get("activity_date")
    if not isinstance(raw, str) or not raw.strip():
        return sale.get("activity_time") or "time not recorded"
    try:
        observed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return sale.get("activity_time") or "time not recorded"
        eastern = observed.astimezone(ZoneInfo("America/New_York"))
        return f"{eastern.strftime('%b')} {eastern.day}, {eastern.strftime('%Y, %H:%M %Z')}"
    except (TypeError, ValueError):
        return sale.get("activity_time") or "time not recorded"


def listing_price_text(listing: dict, state: dict) -> str:
    """Show a POL listing with its display-time current spot USD equivalent."""
    price = listing.get("price")
    currency = listing.get("currency", state.get("currency", "POLYGON"))
    rendered = f"{float(price):,.0f}" if isinstance(price, (int, float)) else str(price or "Unknown")
    rendered_currency = "POL" if currency == "POLYGON" else currency
    rate = ((state.get("current_pol_usd") or {}).get("rate"))
    if currency != "POLYGON" or not isinstance(price, (int, float)) or not isinstance(rate, (int, float)):
        return f"{rendered} {rendered_currency}"
    return f"{rendered} {rendered_currency} (${float(price) * rate:,.2f})"


def last_sale_text(sale: dict | None, currency: str) -> str:
    if not isinstance(sale, dict):
        return "Not yet verified"
    price = sale.get("price")
    if isinstance(price, (int, float)):
        rendered_price = f"{price:,}"
    else:
        rendered_price = str(price or "Unknown")
    rendered_currency = "POL" if sale.get("currency", currency) == "POLYGON" else sale.get("currency", currency)
    usd = sale.get("usd_amount")
    usd_text = f" (${float(usd):,.2f})" if isinstance(usd, (int, float, str)) and str(usd).strip() else ""
    when = sale_time_text(sale)
    return f"{rendered_price} {rendered_currency}{usd_text}, {when} by {buyer_label(sale)}"
