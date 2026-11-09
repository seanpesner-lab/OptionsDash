#!/usr/bin/env python3
"""
breakout_scanner.py

Scans the S&P 500 (or your own list) for the setups behind your best options trades.
Daily bars. RSI 14 (Wilder), MACD 12/26/9, same settings as TradingView defaults.

Setups
  REVERSAL  Based on MCD (+349%) and DAL Nov 6 (+253%).
            In the last 7 days RSI hit 33 or lower and the close sat 5% or more below the 50-day SMA.
            TRIGGERED = MACD crossed above its signal (histogram above zero) in the last 2 days.
            TURNED = crossed 3 to 7 days ago, histogram still above zero, RSI under 60 and
                     close still at or below the 50-day SMA. Turned but not yet run away.
            WATCH = histogram still below zero but rising. Selling is fading, no turn yet.
            EARLY = oversold and still falling. Too soon for calls, keep on the list.
            On MCD this rule fired Oct 8, one day after your first buy, and never fired
            during the September slide. It does not fire on META yet.
  MOMENTUM  Based on AAPL (+116%).
            Pullback in an uptrend: RSI 45 to 60, MACD above zero and above its signal,
            close 0 to 3% above the 50-day SMA.
  SQUEEZE   Classic pre-breakout.
            Bollinger bandwidth in the tightest 20% of the last 6 months,
            close within 3% of the 20-day high and above the 50-day SMA.
  AVOID     Based on UBER (-26%).
            RSI 38 to 46, MACD histogram flat near zero, close 4% or more below the 50-day SMA.
            A slow drift, not a real flush. Time decay eats calls here.

Data sources
  alpaca   Alpaca Market Data (free account). Used automatically when ALPACA_API_KEY_ID and
           ALPACA_API_SECRET_KEY are set. ALPACA_FEED=sip (default, 15-min delayed on the
           free plan) or iex.
  yahoo    yfinance. No key needed, but unofficial and often blocked from cloud servers.

Earnings dates
  With ALPHAVANTAGE_API_KEY set, --earnings uses Alpha Vantage's earnings calendar
  (one request, cached for 20 hours in .cache/). Without it, falls back to yfinance.

Usage
  pip install -r requirements.txt
  python breakout_scanner.py                      # S&P 500
  python breakout_scanner.py --tickers MCD DAL ELAN
  python breakout_scanner.py --file watchlist.txt # one ticker per line
  python breakout_scanner.py --earnings           # add next earnings date for hits
  python breakout_scanner.py --asof 2026-10-07    # what the scan would have shown that day
  python breakout_scanner.py --earnings --site site   # also write site/data/latest.json for the web page
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/bars"
ET = ZoneInfo("America/New_York")

MIN_PRICE = 10.0
MIN_DOLLAR_VOL = 100e6  # 20-day average. Rough proxy for liquid options.

ORDER = {"REVERSAL": 0, "MOMENTUM": 1, "SQUEEZE": 2, "AVOID": 3}
RANK = {"TRIGGERED": 0, "TURNED": 1, "WATCH": 2, "EARLY": 3}


def indicators(close, high=None, volume=None):
    d = close.diff()
    au = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    sma20 = close.rolling(20).mean()
    bbw = 4 * close.rolling(20).std() / sma20
    f = pd.DataFrame({
        "close": close,
        "rsi": 100 - 100 / (1 + au / ad),
        "macd": macd,
        "sig": sig,
        "hist": macd - sig,
        "sma20": sma20,
        "sma50": close.rolling(50).mean(),
        "sma200": close.rolling(200).mean(),
        "bbw_pct": bbw.rolling(126, min_periods=60).rank(pct=True),
        "high20": (high if high is not None else close).rolling(20).max(),
    })
    if volume is not None:
        f["dollar_vol20"] = (close * volume).rolling(20).mean()
    return f


def evaluate(f):
    """Classify the last bar of an indicator frame. Returns a dict or None."""
    f = f.dropna(subset=["rsi", "sma50", "hist"])
    if len(f) < 6:
        return None
    r, p = f.iloc[-1], f.iloc[-2]
    dist20 = r.close / r.sma20 - 1
    dist50 = r.close / r.sma50 - 1
    hist_up = r["hist"] > p["hist"]
    hist_up2 = hist_up and p["hist"] > f["hist"].iloc[-3]
    crosses = (f.macd > f.sig) & (f.macd.shift(1) <= f.sig.shift(1))

    rsi_min7 = f.rsi.iloc[-7:].min()
    d50_min7 = (f.close / f.sma50 - 1).iloc[-7:].min()
    oversold = rsi_min7 <= 33 and d50_min7 <= -0.05

    setup, status, score = None, "", 0.0
    if oversold and crosses.iloc[-2:].any():
        setup, status = "REVERSAL", "TRIGGERED"
        score = (33 - rsi_min7) + abs(d50_min7) * 100 + 10
    elif oversold and crosses.iloc[-7:].any() and r["hist"] > 0 and r.rsi < 60 and dist50 <= 0:
        setup, status = "REVERSAL", "TURNED"
        score = (33 - rsi_min7) + abs(d50_min7) * 100 + (5 if hist_up else 0)
    elif oversold and hist_up and r["hist"] < 0:
        setup, status = "REVERSAL", "WATCH"
        score = (33 - rsi_min7) + abs(d50_min7) * 100 + (5 if hist_up2 else 0)
    elif oversold and r["hist"] < 0:
        setup, status = "REVERSAL", "EARLY"
        score = (33 - rsi_min7) + abs(d50_min7) * 100 - 10
    elif 45 <= r.rsi <= 60 and r.macd > 0 and r["hist"] > 0 and 0 <= dist50 <= 0.03:
        setup, status = "MOMENTUM", "TRIGGERED"
        score = 20 - abs(r.rsi - 52) + (5 if hist_up else 0)
    elif pd.notna(r.bbw_pct) and r.bbw_pct <= 0.20 and r.close >= 0.97 * r.high20 and dist50 > 0:
        setup, status = "SQUEEZE", "WATCH"
        score = (0.20 - r.bbw_pct) * 100 + (5 if r.close >= r.high20 else 0)
    elif 38 <= r.rsi <= 46 and abs(r["hist"]) / r.close < 0.002 and dist50 <= -0.04:
        setup, status = "AVOID", "drift"

    if setup is None:
        return None
    out = {
        "bar_date": str(pd.Timestamp(f.index[-1]).date()),
        "setup": setup, "status": status, "score": round(score, 1),
        "close": round(r.close, 2), "rsi": round(r.rsi, 1), "rsi_min7": round(rsi_min7, 1),
        "macd_hist": round(r["hist"], 3), "hist_rising": bool(hist_up),
        "vs_sma20_pct": round(dist20 * 100, 1), "vs_sma50_pct": round(dist50 * 100, 1),
        "above_sma200": (None if pd.isna(r.sma200) else bool(r.close > r.sma200)),
    }
    if "dollar_vol20" in f:
        out["dollar_vol20_M"] = round(r.dollar_vol20 / 1e6)
    return out


# ---------------------------------------------------------------- universe

def load_universe(args):
    """Tickers in exchange form, with a dot for share classes (BRK.B)."""
    if args.tickers:
        return [t.upper().replace("-", ".") for t in args.tickers]
    if args.file:
        with open(args.file) as fh:
            return [ln.strip().upper().replace("-", ".") for ln in fh
                    if ln.strip() and not ln.strip().startswith("#")]
    sp = pd.read_csv(SP500_CSV)
    return sorted(sp.Symbol.str.replace("-", ".", regex=False).tolist())


# ---------------------------------------------------------------- data sources

def fetch_yahoo(tickers):
    import yfinance as yf
    ymap = {t.replace(".", "-"): t for t in tickers}
    data = yf.download(list(ymap), period="2y", interval="1d", auto_adjust=True,
                       group_by="ticker", threads=True, progress=False)
    frames = {}
    for yt, t in ymap.items():
        try:
            df = data[yt] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna(subset=["Close"])
            if len(df) > 60:
                frames[t] = df
        except KeyError:
            pass
    return frames


def fetch_alpaca(tickers, key, secret, feed="sip"):
    import requests
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)  # free plan: SIP is 15 min delayed
    start = end - dt.timedelta(days=740)
    sess = requests.Session()
    sess.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    raw = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                  "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "limit": 10000, "adjustment": "all", "feed": feed}
        tries = 0
        while True:
            r = sess.get(ALPACA_BARS, params=params, timeout=60)
            if r.status_code == 429 and tries < 5:
                tries += 1
                time.sleep(2 * tries)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca {r.status_code}: {r.text[:300]}")
            j = r.json()
            for sym, lst in (j.get("bars") or {}).items():
                raw.setdefault(sym, []).extend(lst)
            token = j.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        print(f"  Alpaca: {min(i + 100, len(tickers))}/{len(tickers)} tickers", file=sys.stderr)
    frames = {}
    for sym, lst in raw.items():
        b = pd.DataFrame(lst)
        idx = pd.to_datetime(b["t"], utc=True).dt.tz_convert(ET).dt.tz_localize(None).dt.normalize()
        df = pd.DataFrame({"Close": b["c"].astype(float).values, "High": b["h"].astype(float).values,
                           "Volume": b["v"].astype(float).values}, index=pd.DatetimeIndex(idx))
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if len(df) > 60:
            frames[sym] = df
    return frames


# ---------------------------------------------------------------- earnings

def earnings_alphavantage(key, cache_dir=".cache", max_age_h=20):
    """Map ticker -> next report date (YYYY-MM-DD) from one Alpha Vantage call, cached."""
    import requests
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "earnings_calendar.csv")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600
    if not fresh:
        r = requests.get("https://www.alphavantage.co/query",
                         params={"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": key}, timeout=60)
        r.raise_for_status()
        if not r.text.lower().startswith("symbol"):
            raise RuntimeError(f"Alpha Vantage: {r.text[:200]}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(r.text)
    cal = pd.read_csv(path)
    cal["symbol"] = cal["symbol"].astype(str).str.upper().str.replace("-", ".", regex=False)
    cal = cal.sort_values("reportDate").drop_duplicates("symbol")
    return dict(zip(cal.symbol, cal.reportDate.astype(str)))


def earnings_yahoo(t):
    try:
        import yfinance as yf
        cal = yf.Ticker(t.replace(".", "-")).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        return str(dates[0]) if dates else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--file")
    ap.add_argument("--asof", help="YYYY-MM-DD, scan as of that close")
    ap.add_argument("--earnings", action="store_true")
    ap.add_argument("--all-liquidity", action="store_true", help="skip price and volume filters")
    ap.add_argument("--source", choices=["auto", "alpaca", "yahoo"], default="auto")
    ap.add_argument("--site", help="folder of the web dashboard; writes <site>/data/latest.json and .csv")
    args = ap.parse_args()

    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    source = args.source if args.source != "auto" else ("alpaca" if key and secret else "yahoo")

    tickers = load_universe(args)
    print(f"Downloading {len(tickers)} tickers from {source}...", file=sys.stderr)
    if source == "alpaca":
        if not (key and secret):
            sys.exit("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY to use Alpaca.")
        frames = fetch_alpaca(tickers, key, secret, os.environ.get("ALPACA_FEED", "sip"))
    else:
        frames = fetch_yahoo(tickers)
    print(f"Got bars for {len(frames)} tickers.", file=sys.stderr)

    rows = []
    for t, df in frames.items():
        if args.asof:
            df = df.loc[:args.asof]
        f = indicators(df["Close"], df.get("High"), df.get("Volume"))
        if not args.all_liquidity and "dollar_vol20" in f:
            last = f.iloc[-1]
            if last.close < MIN_PRICE or last.dollar_vol20 < MIN_DOLLAR_VOL:
                continue
        res = evaluate(f)
        if res:
            rows.append({"ticker": t, **res})

    if not rows:
        print("No setups found.")
        if not args.site:
            return
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["setup", "status", "score"], key=lambda s: s.map(ORDER) if s.name == "setup"
                              else s.map(RANK).fillna(4) if s.name == "status" else -s)
    if args.earnings and len(out):
        av = os.environ.get("ALPHAVANTAGE_API_KEY")
        if av:
            try:
                cal = earnings_alphavantage(av)
                out["next_earnings"] = [cal.get(t, "") if s != "AVOID" else "" for t, s in zip(out.ticker, out.setup)]
            except Exception as e:
                print(f"Earnings calendar failed ({e}); leaving dates blank.", file=sys.stderr)
                out["next_earnings"] = ""
        else:
            out["next_earnings"] = [earnings_yahoo(t) if s != "AVOID" else "" for t, s in zip(out.ticker, out.setup)]

    pd.set_option("display.width", 200, "display.max_rows", 500)
    if len(out):
        print(out.to_string(index=False))
    name = f"scan_{args.asof or dt.date.today().isoformat()}.csv"
    out.to_csv(name, index=False)
    out.to_csv("latest_scan.csv", index=False)
    print(f"\nSaved {name}", file=sys.stderr)

    if args.site:
        now_et = dt.datetime.now(ET)
        bar_date = out["bar_date"].max() if len(out) else None
        # A bar for today is still moving until the 4 PM close, and Alpaca's free SIP feed
        # lags 15 minutes, so treat it as final only from 4:20 PM Eastern.
        final_at = dt.time(16, 20) if source == "alpaca" else dt.time(16, 0)
        provisional = bool(bar_date == now_et.date().isoformat() and now_et.time() < final_at and not args.asof)
        meta = {
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bar_date": bar_date,
            "provisional": provisional,
            "source": source,
            "universe": len(tickers),
            "with_data": len(frames),
            "hits": int(len(out)),
        }
        records = json.loads(out.to_json(orient="records")) if len(out) else []  # numpy types -> plain JSON
        os.makedirs(os.path.join(args.site, "data"), exist_ok=True)
        with open(os.path.join(args.site, "data", "latest.json"), "w", encoding="utf-8") as fh:
            json.dump({"meta": meta, "rows": records}, fh, separators=(",", ":"))
        out.to_csv(os.path.join(args.site, "data", "latest.csv"), index=False)
        print(f"Wrote {args.site}/data/latest.json ({meta})", file=sys.stderr)


if __name__ == "__main__":
    main()
