"""Map Kalshi market objects to our `pm.*` schema shape.

Kalshi returns numeric fields as strings ('0.0000', '12.34'). All parsing
here returns Python `Decimal`/`float` or `None`, never strings.
"""

from datetime import datetime, timezone


def _f(v) -> float | None:
    """Parse Kalshi numeric-string ('12.34') to float; return None on '' / 0
    sentinels that mean 'no data'."""
    if v is None or v == "":
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out


def _ts(v) -> datetime | None:
    """Parse Kalshi ISO timestamp (with 'Z' suffix) to tz-aware UTC datetime."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# Map a Kalshi series_ticker prefix to a normalized category. The series prefix
# is the most reliable signal — Kalshi adds new markets daily but the series
# taxonomy is stable.
_CATEGORY_PREFIXES = [
    # multivariate / parlays — check first since these can contain sports/crypto subs
    ("KXMVE", "multivariate"),

    # crypto
    ("KXBTC", "crypto"), ("KXETH", "crypto"), ("KXSOL", "crypto"),
    ("KXXRP", "crypto"), ("KXDOGE", "crypto"), ("KXADA", "crypto"),
    ("KXAVAX", "crypto"), ("KXLINK", "crypto"), ("KXMATIC", "crypto"),
    ("KXLITECOIN", "crypto"), ("KXBNB", "crypto"),

    # sports — leagues/teams/players
    ("KXNBA", "sports"), ("KXNFL", "sports"), ("KXMLB", "sports"),
    ("KXNHL", "sports"), ("KXMLS", "sports"), ("KXNCAA", "sports"),
    ("KXCFB", "sports"), ("KXCBB", "sports"), ("KXWNBA", "sports"),
    ("KXBUNDESLIGA", "sports"), ("KXPREMIERLEAGUE", "sports"),
    ("KXLIGUE1", "sports"), ("KXLALIGA", "sports"), ("KXSERIE", "sports"),
    ("KXUEFA", "sports"), ("KXCHAMPIONS", "sports"), ("KXEUROPALEAGUE", "sports"),
    ("KXFIFA", "sports"), ("KXWORLDCUP", "sports"), ("KXOLYMPIC", "sports"),
    ("KXATP", "sports"), ("KXWTA", "sports"), ("KXTENNIS", "sports"),
    ("KXGOLF", "sports"), ("KXPGA", "sports"), ("KXMASTERS", "sports"),
    ("KXUFC", "sports"), ("KXMMA", "sports"), ("KXBOXING", "sports"),
    ("KXF1", "sports"), ("KXNASCAR", "sports"), ("KXINDYCAR", "sports"),
    ("KXSPORTS", "sports"),

    # politics / elections
    ("KXELECTION", "politics"), ("KXPRES", "politics"), ("KXSENATE", "politics"),
    ("KXHOUSE", "politics"), ("KXTRUMP", "politics"), ("KXBIDEN", "politics"),
    ("KXHARRIS", "politics"), ("KXVANCE", "politics"), ("KXNEWSOM", "politics"),
    ("KXGOV", "politics"), ("KXMAYOR", "politics"), ("KXCONGRESS", "politics"),
    ("KXSCOTUS", "politics"), ("KXPOLI", "politics"), ("KXPRIMARY", "politics"),

    # economics / finance / fed
    ("KXFED", "economics"), ("KXGDP", "economics"), ("KXCPI", "economics"),
    ("KXJOBS", "economics"), ("KXNFP", "economics"), ("KXUNEMP", "economics"),
    ("KXRATE", "economics"), ("KXINFLA", "economics"), ("KXTREASURY", "economics"),
    ("KXSPX", "economics"), ("KXDJIA", "economics"), ("KXNASDAQ", "economics"),
    ("KXVIX", "economics"), ("KXOIL", "economics"), ("KXGOLD", "economics"),
    ("KXMARKET", "economics"), ("KXSTOCK", "economics"),

    # weather
    ("KXTEMP", "weather"), ("KXSNOW", "weather"), ("KXRAIN", "weather"),
    ("KXHURRICANE", "weather"), ("KXFROST", "weather"), ("KXWEATHER", "weather"),

    # entertainment / culture
    ("KXOSCAR", "entertainment"), ("KXEMMY", "entertainment"),
    ("KXGRAMMY", "entertainment"), ("KXMOVIE", "entertainment"),
    ("KXBOX", "entertainment"), ("KXTIME", "entertainment"),
    ("KXBILLBOARD", "entertainment"), ("KXNETFLIX", "entertainment"),
]


def categorize(series_ticker: str | None, event_ticker: str | None) -> str:
    """Best-effort category for a Kalshi market. Falls back to 'other'."""
    candidates = [s for s in (series_ticker, event_ticker) if s]
    for c in candidates:
        up = c.upper()
        for prefix, cat in _CATEGORY_PREFIXES:
            if up.startswith(prefix):
                return cat
    return "other"


def normalize_market(m: dict) -> dict:
    """Map a raw Kalshi market object to a row ready for pm.markets upsert."""
    ticker = m.get("ticker") or ""
    event_ticker = m.get("event_ticker")
    # Kalshi exposes the series via custom_strike / mve_collection_ticker / a
    # convention where the ticker is `<SERIES>-<EVENT>-<MARKET>`. The simplest
    # reliable extraction: take the first hyphen-segment of the event_ticker
    # (drops the date suffix), falling back to the ticker prefix.
    series_ticker = None
    if event_ticker:
        series_ticker = event_ticker.split("-", 1)[0]
    elif ticker:
        series_ticker = ticker.split("-", 1)[0]

    return {
        "id": f"kalshi:{ticker}",
        "platform": "kalshi",
        "external_id": ticker,
        "slug": ticker,                              # Kalshi tickers double as slugs
        "title": (m.get("title") or "").strip() or ticker,
        "category": categorize(series_ticker, event_ticker),
        "series_ticker": series_ticker,
        "event_ticker": event_ticker,
        "status": m.get("status"),
        "open_at": _ts(m.get("open_time")),
        "close_at": _ts(m.get("close_time")),
        "settled_at": _ts(m.get("expiration_time")) if m.get("status") in ("settled", "finalized") else None,
        "settled_outcome": (m.get("result") or "") or None,
        "metadata": {
            "subtitle": m.get("yes_sub_title") or m.get("no_sub_title"),
            "market_type": m.get("market_type"),
            "strike_type": m.get("strike_type"),
            "rules_primary": m.get("rules_primary"),
            "notional_value_dollars": _f(m.get("notional_value_dollars")),
            "fractional_trading_enabled": m.get("fractional_trading_enabled"),
        },
    }


def snapshot_row(market_id: str, m: dict, taken_at: datetime) -> dict:
    """Build a pm.market_snapshots row from a raw Kalshi market object."""
    return {
        "market_id": market_id,
        "taken_at": taken_at,
        "yes_price": _f(m.get("last_price_dollars")),
        "yes_bid":   _f(m.get("yes_bid_dollars")),
        "yes_ask":   _f(m.get("yes_ask_dollars")),
        "no_bid":    _f(m.get("no_bid_dollars")),
        "no_ask":    _f(m.get("no_ask_dollars")),
        "volume_total_usd": _f(m.get("volume_fp")),
        "volume_24h_usd":   _f(m.get("volume_24h_fp")),
        "liquidity_usd":    _f(m.get("liquidity_dollars")),
        "open_interest":    _f(m.get("open_interest_fp")),
    }
