#!/usr/bin/env python3
"""
backtest_breakout.py - stocks about to break out vs the current oversold-reversal rule.

Setups (checked on each day's close, S&P 500, liquid names only):
  REVERSAL   the current rule: oversold in the last 7 days, MACD crossed up in the last 2 days.
  COIL       about to break out: an uptrend (close > 50-day > 200-day, 50-day rising), within 5% of
             the 60-day high, a tight 15-day range (high-to-low 10% or less), relative strength vs SPY
             at or near its 60-day high, and RSI 50 to 70 (strong but not stretched).
  COIL+DRY   COIL, and volume has dried up (10-day average below 85% of the 50-day average).
  BREAKOUT   the trigger: close above the prior 20-day high on volume at least 1.5x the 50-day
             average, in an uptrend with relative strength at a high, after a COIL in the prior 5 days.

Entry at the next day's open. Measured: stock returns at 5, 10, 20 days vs SPY, how often the move
faltered, and the call proxy (see backtest.py) with three exits: hold 10 days, +50% take profit,
and your plan (after RSI 70, sell when momentum fades; 8% stop; 20 days).

  faltered    the stock never got 3% above entry in 10 days and was below entry on day 10
  failed BO   (BREAKOUT only) closed back below the breakout level within 5 days

Uses the 5-year Yahoo data cached by backtest_filters.py.
"""
import math
import sys

import numpy as np
import pandas as pd

import backtest as bt
import backtest_filters as bf
import breakout_scanner as bs

MAXD = 20


def setups(df, spy_c):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    f = bs.indicators(c, h, v)
    sma50, sma200, rsi = f["sma50"], f["sma200"], f["rsi"]
    liquid = (c >= bs.MIN_PRICE) & (f["dollar_vol20"] >= bs.MIN_DOLLAR_VOL)
    trend = (c > sma50) & (sma50 > sma200) & (sma50 > sma50.shift(10))
    near_high = c >= 0.95 * h.rolling(60).max()
    base = (h.rolling(15).max() - l.rolling(15).min()) / c <= 0.10
    spy = spy_c.reindex(c.index).ffill()
    rs = c / spy
    rs_high = rs >= 0.97 * rs.rolling(60).max()
    rs_beat = (c / c.shift(63)) > (spy / spy.shift(63))
    rsi_ok = (rsi >= 50) & (rsi <= 70)
    coil = trend & near_high & base & rs_high & rs_beat & rsi_ok & liquid
    dry = v.rolling(10).mean() < 0.85 * v.rolling(50).mean()
    pivot = h.shift(1).rolling(20).max()
    vol_surge = v > 1.5 * v.shift(1).rolling(50).mean()
    recent_coil = coil.shift(1, fill_value=False).astype(int).rolling(5).max() > 0
    breakout = (c > pivot) & vol_surge & trend & rs_high & liquid & recent_coil
    rev = bt.signals_for(df)[0]["TRIGGERED"]
    # The scanner's other "Ready"/"Building" statuses, as in breakout_scanner.evaluate
    hist, macd, sig = f["hist"], f["macd"], f["sig"]
    d50 = c / sma50 - 1
    oversold = (rsi.rolling(7).min() <= 33) & (d50.rolling(7).min() <= -0.05)
    cross = (macd > sig) & (macd.shift(1) <= sig.shift(1))
    cross7 = cross.astype(int).rolling(7).max() > 0
    turned = oversold & ~rev & cross7 & (hist > 0) & (rsi < 60) & (d50 <= 0) & liquid
    momentum = (rsi >= 45) & (rsi <= 60) & (macd > 0) & (hist > 0) & (d50 >= 0) & (d50 <= 0.03) & liquid & ~oversold
    return {"REVERSAL": rev, "TURNED": turned, "MOMENTUM": momentum, "COIL": coil, "COIL+DRY": coil & dry,
            "BREAKOUT": breakout}, f, pivot


def main():
    ymap, data = bf.load("5y")
    spy = data["SPY"].dropna(subset=["Close"])
    spy_c = spy["Close"]
    spy_up = spy_c > spy_c.rolling(200).mean()
    rows = []
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High", "Low", "Volume"])
        except KeyError:
            continue
        if len(df) < 260:
            continue
        sigs, f, pivot = setups(df, spy_c)
        o, c, h = df["Open"].values, df["Close"].values, df["High"].values
        rsi, hist, macd, sig = f["rsi"].values, f["hist"].values, f["macd"].values, f["sig"].values
        piv = pivot.values
        vol20 = (np.log(df["Close"]).diff().rolling(20).std() * math.sqrt(252)).values
        for name, mask in sigs.items():
            for i in bt.first_days(mask.fillna(False)):
                e = i + 1
                if i < 200 or e + MAXD >= len(df) or not vol20[i] or np.isnan(vol20[i]):
                    continue
                entry, day = o[e], df.index[i]
                r = {"setup": name, "ticker": t, "date": day.date(), "spy_up": bool(spy_up.get(day, False)),
                     "down2": bool(c[i] < c[i - 1] < c[i - 2])}   # stock closed down 2 days in a row into the signal
                spy_e = spy["Open"].get(df.index[e])
                for n in (5, 10, 20):
                    x = e + n - 1
                    r[f"ret{n}"] = c[x] / entry - 1
                    sx = spy_c.get(df.index[x])
                    r[f"vs{n}"] = r[f"ret{n}"] - ((sx / spy_e - 1) if spy_e and sx else np.nan)
                r["max10"] = h[e:e + 10].max() / entry - 1
                r["faltered"] = r["max10"] < 0.03 and r["ret10"] < 0
                r["failed_bo"] = bool((c[e:e + 5] < piv[i]).any()) if name == "BREAKOUT" else np.nan
                path = bf.call_path(c[e:e + MAXD], entry, vol20[i])
                if path is None:
                    continue
                r["call_hold10"] = path[9]
                r["call_tp50"] = next((p for p in path[:10] if p >= 0.5), path[9])
                # your plan: after RSI 70, sell when momentum fades; 8% stop; 20 days
                stop, hit70, val = entry * 0.92, False, path[MAXD - 1]
                for k in range(MAXD):
                    d = e + k
                    if c[d] < stop:
                        val = path[k]; break
                    hit70 = hit70 or rsi[d] >= 70
                    fade = (hist[d] < hist[d - 1] < hist[d - 2] and hist[d - 2] > 0) or (macd[d] < sig[d] and macd[d - 1] >= sig[d - 1])
                    if hit70 and fade:
                        val = path[k]; break
                r["call_yourplan"] = val
                rows.append(r)

    s = pd.DataFrame(rows)
    s.to_csv(".cache/bt_breakout_signals.csv", index=False)
    pct = lambda x: f"{x * 100:+.1f}%"
    order = ["REVERSAL", "TURNED", "MOMENTUM", "COIL", "COIL+DRY", "BREAKOUT"]
    print(f"{len(s)} signals, {s.date.min()} to {s.date.max()}\n")
    table = {}
    for k in order:
        d = s[s.setup == k]
        if d.empty:
            continue
        table[k] = {
            "signals": len(d), "per month": round(len(d) / (len(spy) / 21), 1),
            "stock 10d avg": pct(d.ret10.mean()), "stock 10d up": f"{(d.ret10 > 0).mean() * 100:.0f}%",
            "stock 20d avg": pct(d.ret20.mean()), "vs SPY 20d": pct(d.vs20.mean()),
            "faltered": f"{d.faltered.mean() * 100:.0f}%", "failed breakout": f"{d.failed_bo.mean() * 100:.0f}%" if k == "BREAKOUT" else "-",
            "call hold10 avg": pct(d.call_hold10.mean()), "call hold10 med": pct(d.call_hold10.median()),
            "call +50% TP med": pct(d.call_tp50.median()), "call +50% TP win": f"{(d.call_tp50 > 0).mean() * 100:.0f}%",
            "call your plan avg": pct(d.call_yourplan.mean()), "call your plan med": pct(d.call_yourplan.median()),
            "call your plan win": f"{(d.call_yourplan > 0).mean() * 100:.0f}%",
        }
    print(pd.DataFrame(table).to_string())

    print("\nIn market downtrends (SPY below its 200-day) vs uptrends: 10-day stock avg, % faltered")
    for k in order:
        d = s[s.setup == k]
        up, dn = d[d.spy_up], d[~d.spy_up]
        print(f"  {k:<10} up-market {pct(up.ret10.mean())} ({up.faltered.mean() * 100:.0f}% faltered, n={len(up)})   "
              f"down-market {pct(dn.ret10.mean()) if len(dn) else '-'} ({dn.faltered.mean() * 100 if len(dn) else 0:.0f}% faltered, n={len(dn)})")

    base = s[s.setup == "REVERSAL"]
    print("\nVs REVERSAL (standard errors; about 2 or more is meaningful):")
    for k in order[1:]:
        d = s[s.setup == k]
        out = []
        for col in ("ret10", "ret20", "call_tp50", "call_yourplan"):
            se = math.sqrt(d[col].var() / len(d) + base[col].var() / len(base))
            out.append(f"{col} {pct(d[col].mean() - base[col].mean())} ({(d[col].mean() - base[col].mean()) / se:+.1f} SE)")
        print(f"  {k:<10} " + "   ".join(out))

    print("\nSkip picks where the stock closed down 2 days in a row (kept vs skipped):")
    for k in order:
        d = s[s.setup == k]
        keep, skip = d[~d.down2], d[d.down2]
        se = math.sqrt(keep.ret10.var() / len(keep) + skip.ret10.var() / max(len(skip), 2)) if len(skip) > 1 else float("nan")
        print(f"  {k:<10} skipped {len(skip) / len(d) * 100:.0f}%   kept: 10d {pct(keep.ret10.mean())}, faltered {keep.faltered.mean() * 100:.0f}%, "
              f"call TP50 med {pct(keep.call_tp50.median())}, your plan avg {pct(keep.call_yourplan.mean())}   |   "
              f"skipped: 10d {pct(skip.ret10.mean()) if len(skip) else '-'}, faltered {skip.faltered.mean() * 100 if len(skip) else 0:.0f}%   "
              f"(diff {(keep.ret10.mean() - skip.ret10.mean()) / se:+.1f} SE)")

    s["year"] = pd.to_datetime(s.date).dt.year
    print("\n20-day stock average by year (n):")
    for y, d in s.groupby("year"):
        print(f"  {y}  " + "   ".join(f"{k} {pct(d[d.setup == k].ret20.mean()) if (d.setup == k).any() else '-'} ({(d.setup == k).sum()})" for k in order))


if __name__ == "__main__":
    sys.exit(main())

