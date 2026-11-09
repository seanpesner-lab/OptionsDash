#!/usr/bin/env python3
"""
track.py - keep a record of each morning's top 10 and score how they did.

  python track.py record site/data/latest.json site/data/options.json history/picks.csv
      Appends today's top 10 (same ranking as the email) with the best-fit call contract
      and its quote at the time. Runs once per weekday, with the morning email.

  python track.py update history/picks.csv site/data/performance.json
      Scores every recorded pick:
        stock    entry = the signal day's close; return to the latest price, and after 5, 10
                 and 20 trading days, each next to SPY over the same days
        option   entry = the contract's mid price when it was picked; return to its current mid,
                 or to its value at expiration (stock close minus strike, or zero) once expired

Needs ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY for prices.
"""
import csv
import datetime as dt
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SNAPSHOTS = "https://data.alpaca.markets/v1beta1/options/snapshots"
FIELDS = ["signal_date", "picked_on", "rank", "ticker", "tier", "setup", "status", "score", "entry_close",
          "contract", "expiration", "strike", "entry_bid", "entry_ask", "entry_mid"]
HORIZONS = (5, 10, 20)


def today_et():
    return dt.datetime.now(ET).date()


def read_picks(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- record

def record(latest_path, options_path, picks_path):
    from notify import TIER_LABEL, tier, top10
    with open(latest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    best = {}
    if os.path.exists(options_path):
        with open(options_path, encoding="utf-8") as fh:
            for s in json.load(fh).get("stocks", []):
                if s.get("contracts"):
                    best[s["ticker"]] = s["contracts"][0]
    today = today_et().isoformat()
    existing = read_picks(picks_path)
    if any(p["picked_on"] == today for p in existing):
        print(f"Picks for {today} already recorded.")
        return
    signal = data.get("meta", {}).get("bar_date")
    rows = []
    for i, r in enumerate(top10(data.get("rows", [])), 1):
        c = best.get(r["ticker"]) or {}
        rows.append({
            "signal_date": signal, "picked_on": today, "rank": i, "ticker": r["ticker"],
            "tier": TIER_LABEL[tier(r)], "setup": r["setup"], "status": r["status"], "score": r["score"],
            "entry_close": r["close"], "contract": c.get("symbol", ""), "expiration": c.get("expiration", ""),
            "strike": c.get("strike", ""), "entry_bid": c.get("bid", ""), "entry_ask": c.get("ask", ""),
            "entry_mid": c.get("mid", ""),
        })
    new_file = not os.path.exists(picks_path)
    os.makedirs(os.path.dirname(picks_path) or ".", exist_ok=True)
    with open(picks_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"Recorded {len(rows)} picks for {today} (signal {signal}).")


# ---------------------------------------------------------------- update

def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def option_marks(symbols, key, secret, feed):
    import requests
    sess = requests.Session()
    sess.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    marks = {}
    for i in range(0, len(symbols), 100):
        params = {"symbols": ",".join(symbols[i:i + 100]), "feed": feed, "limit": 1000}
        while True:
            r = sess.get(SNAPSHOTS, params=params, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca options {r.status_code}: {r.text[:200]}")
            j = r.json()
            for sym, s in (j.get("snapshots") or {}).items():
                q = s.get("latestQuote") or {}
                bid, ask = q.get("bp") or 0, q.get("ap") or 0
                if ask > 0:
                    marks[sym] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
    return marks


def close_on_or_before(df, day):
    s = df.loc[:str(day), "Close"]
    return (float(s.iloc[-1]), s.index[-1].date()) if len(s) else (None, None)


def ret(a, b):
    return None if a is None or b is None or a == 0 else round((b / a - 1) * 100, 2)


def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def winrate(xs):
    xs = [x for x in xs if x is not None]
    return round(100 * sum(1 for x in xs if x > 0) / len(xs), 1) if xs else None


def score_picks(picks, frames, marks, today):
    spy = frames.get("SPY")
    out = []
    for p in picks:
        sig = dt.date.fromisoformat(p["signal_date"])
        entry = num(p["entry_close"])
        df = frames.get(p["ticker"])
        row = {k: p[k] for k in ("signal_date", "picked_on", "ticker", "tier", "setup", "status", "contract", "expiration")}
        row.update(rank=int(p["rank"]), score=num(p["score"]), entry_close=entry, strike=num(p["strike"]),
                   entry_mid=num(p["entry_mid"]))
        last = last_date = None
        if df is not None and len(df):
            last, last_date = float(df["Close"].iloc[-1]), df.index[-1].date()
            after = df.loc[df.index.date > sig, "Close"]
            row["days_held"] = int(len(after))
            row["last_close"] = round(last, 2)
            row["stock_pct"] = ret(entry, last)
            spy_entry = close_on_or_before(spy, sig)[0] if spy is not None else None
            spy_last = float(spy["Close"].iloc[-1]) if spy is not None else None
            row["spy_pct"] = ret(spy_entry, spy_last)
            row["vs_spy_pct"] = (None if row["stock_pct"] is None or row["spy_pct"] is None
                                 else round(row["stock_pct"] - row["spy_pct"], 2))
            for h in HORIZONS:
                if len(after) >= h:
                    d_h = after.index[h - 1]
                    s_ret = ret(entry, float(after.iloc[h - 1]))
                    spy_h = close_on_or_before(spy, d_h.date())[0] if spy is not None else None
                    row[f"d{h}_pct"] = s_ret
                    row[f"d{h}_vs_spy"] = (None if s_ret is None or ret(spy_entry, spy_h) is None
                                           else round(s_ret - ret(spy_entry, spy_h), 2))
                else:
                    row[f"d{h}_pct"] = row[f"d{h}_vs_spy"] = None
        else:
            row.update(days_held=None, last_close=None, stock_pct=None, spy_pct=None, vs_spy_pct=None,
                       **{f"d{h}_pct": None for h in HORIZONS}, **{f"d{h}_vs_spy": None for h in HORIZONS})

        # Option
        row["option_state"], row["option_value"], row["option_pct"] = "none", None, None
        if p["contract"] and row["entry_mid"]:
            exp = dt.date.fromisoformat(p["expiration"])
            if today > exp and df is not None:
                c_exp, _ = close_on_or_before(df, exp)
                if c_exp is not None and row["strike"] is not None:
                    row["option_state"] = "expired"
                    row["option_value"] = round(max(0.0, c_exp - row["strike"]), 2)
            else:
                row["option_state"] = "open"
                m = marks.get(p["contract"])
                if m:
                    row["option_value"] = round(m["mid"], 2)
            row["option_pct"] = ret(row["entry_mid"], row["option_value"])
        out.append(row)
    return out


def summarize(rows):
    def block(all_rs):
        # Picks made since the last close haven't had a chance to move; count them, but leave them out of averages.
        rs = [r for r in all_rs if r.get("days_held")]
        return {
            "picks": len(all_rs),
            "scored": len(rs),
            "stock_avg_pct": avg([r.get("stock_pct") for r in rs]),
            "stock_win_rate": winrate([r.get("stock_pct") for r in rs]),
            "vs_spy_avg_pct": avg([r.get("vs_spy_pct") for r in rs]),
            "beat_spy_rate": winrate([r.get("vs_spy_pct") for r in rs]),
            "option_picks": sum(1 for r in rs if r.get("option_pct") is not None),
            "option_avg_pct": avg([r.get("option_pct") for r in rs]),
            "option_win_rate": winrate([r.get("option_pct") for r in rs]),
            **{f"d{h}_avg_pct": avg([r.get(f"d{h}_pct") for r in rs]) for h in HORIZONS},
            **{f"d{h}_vs_spy_avg": avg([r.get(f"d{h}_vs_spy") for r in rs]) for h in HORIZONS},
            **{f"d{h}_n": sum(1 for r in rs if r.get(f"d{h}_pct") is not None) for h in HORIZONS},
        }
    tiers = ["Ready", "Building", "Setting up"]
    return {"all": block(rows), "by_tier": {t: block([r for r in rows if r["tier"] == t]) for t in tiers}}


def update(picks_path, out_path):
    picks = read_picks(picks_path)
    today = today_et()
    result = {"meta": {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "first_pick": None, "pick_days": 0, "error": None},
              "summary": summarize([]), "picks": []}
    if picks:
        key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
        if not (key and secret):
            sys.exit("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.")
        from breakout_scanner import fetch_alpaca
        first = min(dt.date.fromisoformat(p["signal_date"]) for p in picks)
        tickers = sorted({p["ticker"] for p in picks} | {"SPY"})
        frames = fetch_alpaca(tickers, key, secret, os.environ.get("ALPACA_FEED", "sip"),
                              days=(today - first).days + 10, min_bars=1)
        open_syms = sorted({p["contract"] for p in picks if p["contract"] and p["expiration"]
                            and dt.date.fromisoformat(p["expiration"]) >= today})
        marks = {}
        if open_syms:
            try:
                marks = option_marks(open_syms, key, secret, os.environ.get("ALPACA_OPTIONS_FEED", "indicative"))
            except Exception as e:
                result["meta"]["error"] = f"Option prices unavailable: {e}"[:250]
        rows = score_picks(picks, frames, marks, today)
        rows.sort(key=lambda r: (r["picked_on"], -r["rank"]), reverse=True)
        result.update(summary=summarize(rows), picks=rows)
        result["meta"].update(first_pick=first.isoformat(), pick_days=len({p["picked_on"] for p in picks}))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, separators=(",", ":"))
    print(f"Wrote {out_path}: {len(result['picks'])} picks, summary {result['summary']['all']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "record" and len(sys.argv) == 5:
        record(*sys.argv[2:5])
    elif cmd == "update" and len(sys.argv) == 4:
        update(*sys.argv[2:4])
    else:
        sys.exit(__doc__)
