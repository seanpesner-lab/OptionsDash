#!/usr/bin/env python3
"""
options_scan.py - call contracts that fit your rules, for the top 10 stocks of the latest scan.

For each of the top 10 (same ranking as the daily email, see notify.py) it pulls the call chain
from Alpaca's options snapshots and keeps contracts that fit:

  Expiration   30 to 60 days out
  Strike       delta 0.40 to 0.60
  Liquidity    bid and ask both above zero, spread at most 10% of the mid price
  Ranking      contracts that traded today first, then tightest spread; 3 per stock
  Earnings     flagged when the contract expires after the next announced report

These are contracts that match rules, not recommendations.

Environment
  ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY
  ALPACA_OPTIONS_FEED   indicative (default, free) or opra (paid, real time)

Usage
  python options_scan.py site/data/latest.json site/data/options.json
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

from notify import TIER_LABEL, reason, tier, top10

SNAPSHOTS = "https://data.alpaca.markets/v1beta1/options/snapshots/{}"
ET = ZoneInfo("America/New_York")

RULES = {
    "dte_min": 30, "dte_max": 60,
    "delta_min": 0.40, "delta_max": 0.60,
    "max_spread_pct": 10.0,
    "per_stock": 3,
    # Exit plan (tested in backtest_filters.py and a stop-loss study):
    #   take profit when the call is up 50%, otherwise sell after 10 trading days,
    #   and sell if the stock closes 8% or more below its price at entry.
    # The 8% stop cost about 0.4% per trade on average in the test but caps the worst losers;
    # tighter stops cost more.
    "take_profit_pct": 50,
    "hold_days": 10,
    "stop_pct": 8,
}

# NYSE full-day closures, for the sell-by date.
NYSE_HOLIDAYS = [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
]


def sell_by(entry_day, hold_days=RULES["hold_days"]):
    """Close of the hold_days-th trading day, counting the entry day as day 1."""
    import numpy as np
    d = np.busday_offset(np.datetime64(entry_day), 0, roll="forward", holidays=NYSE_HOLIDAYS)
    return str(np.busday_offset(d, hold_days - 1, holidays=NYSE_HOLIDAYS))


def parse_occ(sym):
    """AAPL261218C00150000 -> (expiration date, 'C', 150.0). Root length varies, so parse from the end."""
    tail = sym[-15:]
    return dt.date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6])), tail[6], int(tail[7:]) / 1000


def fetch_chain(sess, underlying, spot, today, feed):
    params = {
        "feed": feed, "type": "call", "limit": 1000,
        "expiration_date_gte": (today + dt.timedelta(days=RULES["dte_min"])).isoformat(),
        "expiration_date_lte": (today + dt.timedelta(days=RULES["dte_max"])).isoformat(),
        # Delta 0.40-0.60 sits close to the money; this band keeps the download small.
        "strike_price_gte": round(spot * 0.75, 2), "strike_price_lte": round(spot * 1.30, 2),
    }
    out, tries = {}, 0
    while True:
        r = sess.get(SNAPSHOTS.format(underlying), params=params, timeout=60)
        if r.status_code == 429 and tries < 5:
            tries += 1
            time.sleep(2 * tries)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca options {r.status_code}: {r.text[:200]}")
        j = r.json()
        out.update(j.get("snapshots") or {})
        token = j.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token


def pick_contracts(chain, spot, today, next_earnings):
    rows = []
    for sym, s in chain.items():
        try:
            exp, cp, strike = parse_occ(sym)
        except (ValueError, IndexError):
            continue
        if cp != "C":
            continue
        q, g = s.get("latestQuote") or {}, s.get("greeks") or {}
        bid, ask, delta = q.get("bp") or 0, q.get("ap") or 0, g.get("delta")
        dte = (exp - today).days
        if not (RULES["dte_min"] <= dte <= RULES["dte_max"]) or delta is None:
            continue
        if not (RULES["delta_min"] <= delta <= RULES["delta_max"]) or bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if spread_pct > RULES["max_spread_pct"]:
            continue
        vol = int(((s.get("dailyBar") or {}).get("v")) or 0)
        iv = s.get("impliedVolatility")
        theta = g.get("theta")
        after_earnings = bool(next_earnings and today <= dt.date.fromisoformat(next_earnings[:10]) <= exp)
        rows.append({
            "symbol": sym, "expiration": exp.isoformat(), "dte": dte, "strike": strike,
            "bid": round(bid, 2), "ask": round(ask, 2), "mid": round(mid, 2), "cost": round(mid * 100),
            "spread_pct": round(spread_pct, 1), "delta": round(delta, 2),
            "iv_pct": None if iv is None else round(iv * 100, 1),
            "theta": None if theta is None else round(theta, 3),
            "breakeven": round(strike + mid, 2), "move_pct": round(((strike + mid) / spot - 1) * 100, 1),
            "volume": vol, "after_earnings": after_earnings,
            "take_profit": round(mid * (1 + RULES["take_profit_pct"] / 100), 2),
            "sell_by": sell_by(today.isoformat()),
        })
    rows.sort(key=lambda c: (c["volume"] == 0, c["spread_pct"], abs(c["delta"] - 0.5)))
    return rows[:RULES["per_stock"]]


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    feed = os.environ.get("ALPACA_OPTIONS_FEED", "indicative")
    today = dt.datetime.now(ET).date()
    picks = top10(data.get("rows", []))
    result = {
        "meta": {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "bar_date": data.get("meta", {}).get("bar_date"), "feed": feed, "rules": RULES, "error": None},
        "stocks": [],
    }
    sess = None
    if key and secret:
        import requests
        sess = requests.Session()
        sess.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    else:
        result["meta"]["error"] = "No Alpaca keys, so no option chains were fetched."

    for r in picks:
        stock = {"ticker": r["ticker"], "tier": TIER_LABEL[tier(r)], "setup": r["setup"], "status": r["status"],
                 "reason": reason(r), "close": r["close"], "score": r["score"],
                 "stop_price": round(r["close"] * (1 - RULES["stop_pct"] / 100), 2),
                 "next_earnings": r.get("next_earnings") or "", "contracts": [], "checked": 0, "error": None}
        if sess:
            try:
                chain = fetch_chain(sess, r["ticker"], r["close"], today, feed)
                stock["checked"] = len(chain)
                stock["contracts"] = pick_contracts(chain, r["close"], today, stock["next_earnings"])
            except Exception as e:  # one bad ticker shouldn't sink the rest
                stock["error"] = str(e)[:200]
                print(f"{r['ticker']}: {e}", file=sys.stderr)
        result["stocks"].append(stock)
        print(f"{r['ticker']}: {stock['checked']} calls checked, {len(stock['contracts'])} fit", file=sys.stderr)

    if sess and result["stocks"] and all(s["error"] for s in result["stocks"]):
        result["meta"]["error"] = result["stocks"][0]["error"]
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(result, fh, separators=(",", ":"))
    print(f"Wrote {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
