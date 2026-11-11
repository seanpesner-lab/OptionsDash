#!/usr/bin/env python3
"""
backtest_filters.py - for the current REVERSAL TRIGGERED rule, test trend filters and call exits.

Filters (on the signal day):
  ALL            every TRIGGERED signal
  SPY>200        SPY closed above its 200-day average (market in an uptrend)
  SPY<200        SPY closed below it
  STOCK>200      the stock itself closed above its 200-day average (a pullback, not a breakdown)
  BOTH           SPY>200 and STOCK>200

Exits, for a 35-day at-the-money call bought at the next day's open (Black-Scholes, 20-day realized
volatility held constant, valued at each day's close; see backtest.py for the caveats):
  HOLD5 / HOLD10 / HOLD20   sell after 5, 10 or 20 trading days
  TP50                      sell the first close the call is up 50% or more, else after 10 days
  TP50/SL50                 also sell the first close it is down 50% or more
  TP100                     sell at +100%, else after 20 days

Usage
  python backtest_filters.py            # 5 years
  python backtest_filters.py --period 10y
"""
import argparse
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd

import backtest as bt
import breakout_scanner as bs

CALL_DTE = 35


def load(period):
    """Download once and keep a local copy in .cache/ (ignored by git)."""
    path = os.path.join(".cache", f"bt_{period}.pkl")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    import yfinance as yf
    tickers = bs.load_universe(argparse.Namespace(tickers=None, file=None))
    ymap = {t.replace(".", "-"): t for t in tickers}
    print(f"Downloading {len(ymap)} tickers + SPY, {period}...", file=sys.stderr)
    data = yf.download(list(ymap) + ["SPY"], period=period, interval="1d", auto_adjust=True,
                       group_by="ticker", threads=True, progress=False)
    os.makedirs(".cache", exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump((ymap, data), fh)
    return ymap, data


def call_path(closes, entry, vol):
    """Call value / entry value - 1 at each of the next len(closes) closes."""
    p0 = bt.bs_call(entry, entry, CALL_DTE / 365, vol)
    if p0 <= 0:
        return None
    return [bt.bs_call(s, entry, max(0.0, CALL_DTE - (k + 1) * 7 / 5) / 365, vol) / p0 - 1
            for k, s in enumerate(closes)]


def exits(path):
    def first(cond, upto, default_k):
        for k in range(upto):
            if cond(path[k]):
                return path[k]
        return path[default_k]
    return {
        "HOLD5": path[4], "HOLD10": path[9], "HOLD20": path[19],
        "TP50": first(lambda x: x >= 0.5, 10, 9),
        "TP50/SL50": first(lambda x: x >= 0.5 or x <= -0.5, 10, 9),
        "TP100": first(lambda x: x >= 1.0, 20, 19),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y")
    args = ap.parse_args()
    ymap, data = load(args.period)
    spy = data["SPY"].dropna(subset=["Close"])
    spy_above = spy["Close"] > spy["Close"].rolling(200).mean()

    rows = []
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High"])
        except KeyError:
            continue
        if len(df) < 260:
            continue
        sigs, _, f = bt.signals_for(df)
        o, c = df["Open"].values, df["Close"].values
        vol20 = (np.log(df["Close"]).diff().rolling(20).std() * math.sqrt(252)).values
        stock_above = (df["Close"] > f["sma200"]).values
        for i in bt.first_days(sigs["TRIGGERED"]):
            e = i + 1
            if i < 200 or e + 20 >= len(df) or not vol20[i] or np.isnan(vol20[i]):
                continue
            day = df.index[i]
            path = call_path(c[e:e + 20], o[e], vol20[i])
            if path is None:
                continue
            r = {"ticker": t, "date": day.date(), "spy_above": bool(spy_above.get(day, False)),
                 "stock_above": bool(stock_above[i]), "ret10": c[e + 9] / o[e] - 1, "ret20": c[e + 19] / o[e] - 1}
            r.update(exits(path))
            rows.append(r)

    s = pd.DataFrame(rows)
    subsets = {
        "ALL": s, "SPY>200": s[s.spy_above], "SPY<200": s[~s.spy_above],
        "STOCK>200": s[s.stock_above], "BOTH": s[s.spy_above & s.stock_above],
    }
    exit_cols = ["HOLD5", "HOLD10", "HOLD20", "TP50", "TP50/SL50", "TP100"]
    pct = lambda x: f"{x * 100:+.1f}%"

    print(f"{len(s)} TRIGGERED signals, {s.date.min()} to {s.date.max()}\n")
    print("Stock returns by filter:")
    for k, d in subsets.items():
        print(f"  {k:<10} n={len(d):>5}  10d avg {pct(d.ret10.mean())} up {(d.ret10 > 0).mean() * 100:.0f}%   "
              f"20d avg {pct(d.ret20.mean())} up {(d.ret20 > 0).mean() * 100:.0f}%")

    print("\nCall proxy, average return per trade (median, % of trades that made money):")
    print(f"  {'':<10} " + "".join(f"{x:>24}" for x in exit_cols))
    for k, d in subsets.items():
        cells = [f"{pct(d[x].mean())} ({pct(d[x].median())}, {(d[x] > 0).mean() * 100:.0f}%)" for x in exit_cols]
        print(f"  {k:<10} " + "".join(f"{cell:>24}" for cell in cells))

    base = s
    print("\nFilters vs ALL, HOLD10 call proxy and 10d stock return (standard errors; about 2 or more is meaningful):")
    for k in ["SPY>200", "STOCK>200", "BOTH"]:
        d, rest = subsets[k], s.drop(subsets[k].index)
        out = []
        for col in ("ret10", "HOLD10", "TP50/SL50"):
            se = math.sqrt(d[col].var() / len(d) + rest[col].var() / len(rest))
            out.append(f"{col} {pct(d[col].mean() - rest[col].mean())} vs the rest ({(d[col].mean() - rest[col].mean()) / se:+.1f} SE)")
        print(f"  {k:<10} " + "   ".join(out))

    s["year"] = pd.to_datetime(s.date).dt.year
    print("\nBy year, HOLD10 call proxy average (n): ALL vs SPY>200 vs BOTH")
    for y, d in s.groupby("year"):
        a, b = d[d.spy_above], d[d.spy_above & d.stock_above]
        print(f"  {y}  ALL {pct(d.HOLD10.mean())} ({len(d)})   SPY>200 {pct(a.HOLD10.mean()) if len(a) else '-'} ({len(a)})"
              f"   BOTH {pct(b.HOLD10.mean()) if len(b) else '-'} ({len(b)})   SPY above 200d on {d.spy_above.mean() * 100:.0f}% of signals")
    s.to_csv(os.path.join(".cache", "bt_filters_signals.csv"), index=False)


if __name__ == "__main__":
    main()
