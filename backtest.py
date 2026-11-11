#!/usr/bin/env python3
"""
backtest.py - compare reversal entries: at the MACD cross (current rule) vs just before it.

Every variant starts from the same oversold condition as the scanner's REVERSAL setup:
in the last 7 days RSI(14) hit 33 or lower and the close was 5% or more below the 50-day SMA.

  TRIGGERED  (current)  MACD crossed above its signal today or yesterday.
  PRE-CROSS  (new)      Histogram still below zero, rising 2 days in a row, and at its current
                        pace it reaches zero within 2 days: hist + 2 * (hist - hist_yesterday) >= 0.
  PRE+UP                PRE-CROSS, and the stock closed up on the day.
  PRE+HIGH              PRE-CROSS, and the stock closed above the previous day's high.
  WATCH      (context)  Histogram below zero and rising (the scanner's WATCH status).

Each signal is counted on its first day, and a ticker can't fire the same variant again for
10 trading days. Entry is the NEXT day's open, which is when you could act on the morning email.
Results are measured at 5, 10 and 20 trading days, against SPY over the same days.

The "call proxy" prices a 35-day at-the-money call with Black-Scholes at entry, using 20-day
realized volatility as the implied volatility, and values it again 10 trading days later at the
same volatility. It ignores volatility changes, skew and spreads, so treat it as rough.

Data: Yahoo Finance daily bars, split- and dividend-adjusted, for today's S&P 500 members.
Using today's members leaves out stocks that dropped out of the index (survivorship bias).

Usage
  python backtest.py                 # 5 years
  python backtest.py --period 10y
"""
import argparse
import math
import sys

import numpy as np
import pandas as pd

import breakout_scanner as bs

HORIZONS = (5, 10, 20)
VARIANTS = ["TRIGGERED", "PRE-CROSS", "PRE+UP", "PRE+HIGH", "WATCH"]
COOLDOWN = 10
CALL_DTE, CALL_HOLD = 35, 10


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(s, k, t_years, vol, r=0.04):
    if t_years <= 0 or vol <= 0:
        return max(0.0, s - k)
    d1 = (math.log(s / k) + (r + vol * vol / 2) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    return s * norm_cdf(d1) - k * math.exp(-r * t_years) * norm_cdf(d2)


def signals_for(df):
    """Boolean Series per variant for one ticker's OHLCV frame."""
    c = df["Close"]
    f = bs.indicators(c, df["High"], df["Volume"])
    hist, macd, sig = f["hist"], f["macd"], f["sig"]
    d50 = c / f["sma50"] - 1
    oversold = (f["rsi"].rolling(7).min() <= 33) & (d50.rolling(7).min() <= -0.05)
    cross = (macd > sig) & (macd.shift(1) <= sig.shift(1))
    rising = hist > hist.shift(1)
    rising2 = rising & (hist.shift(1) > hist.shift(2))
    liquid = (c >= bs.MIN_PRICE) & (f["dollar_vol20"] >= bs.MIN_DOLLAR_VOL)
    trig = oversold & (cross | cross.shift(1, fill_value=False)) & liquid
    pre = oversold & (hist < 0) & rising2 & (hist + 2 * (hist - hist.shift(1)) >= 0) & liquid
    watch = oversold & (hist < 0) & rising & liquid
    up_day = c > c.shift(1)
    above_prior_high = c > df["High"].shift(1)
    return {"TRIGGERED": trig, "PRE-CROSS": pre, "PRE+UP": pre & up_day,
            "PRE+HIGH": pre & above_prior_high, "WATCH": watch}, cross, f


def first_days(mask, cooldown=COOLDOWN):
    """Days where the condition starts, with a cooldown between counted signals."""
    starts = mask & ~mask.shift(1, fill_value=False)
    out, last = [], -10**9
    for i in np.flatnonzero(starts.values):
        if i - last > cooldown:
            out.append(i)
            last = i
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y")
    ap.add_argument("--out", default="backtest_signals.csv")
    args = ap.parse_args()

    import yfinance as yf
    tickers = bs.load_universe(argparse.Namespace(tickers=None, file=None))
    ymap = {t.replace(".", "-"): t for t in tickers}
    print(f"Downloading {len(ymap)} tickers + SPY, {args.period}...", file=sys.stderr)
    data = yf.download(list(ymap) + ["SPY"], period=args.period, interval="1d", auto_adjust=True,
                       group_by="ticker", threads=True, progress=False)
    spy = data["SPY"].dropna(subset=["Close"])
    spy_close = spy["Close"]

    rows = []
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High"])
        except KeyError:
            continue
        if len(df) < 260:
            continue
        sigs, cross, f = signals_for(df)
        o, c, h = df["Open"].values, df["Close"].values, df["High"].values
        logret = np.log(df["Close"]).diff()
        vol20 = (logret.rolling(20).std() * math.sqrt(252)).values
        cross_idx = np.flatnonzero(cross.values)
        for name, mask in sigs.items():
            for i in first_days(mask):
                e = i + 1                        # enter at next day's open
                if e + max(HORIZONS) >= len(df) or i < 200:
                    continue
                entry = o[e]
                day = df.index[i]
                r = {"variant": name, "ticker": t, "signal_date": day.date(), "entry": round(entry, 2),
                     "rsi": round(f["rsi"].iloc[i], 1), "hist": round(f["hist"].iloc[i], 3)}
                spy_e = spy["Open"].get(df.index[e])
                for n in HORIZONS:
                    x = e + n - 1
                    r[f"ret{n}"] = c[x] / entry - 1
                    spy_x = spy_close.get(df.index[x])
                    r[f"spy{n}"] = (spy_x / spy_e - 1) if spy_e and spy_x else np.nan
                r["max20"] = h[e:e + 20].max() / entry - 1
                # Did the MACD actually cross within 5 days after the signal (for PRE-CROSS)?
                later = cross_idx[(cross_idx >= i) & (cross_idx <= i + 5)]
                r["crossed_5d"] = len(later) > 0
                r["days_to_cross"] = int(later[0] - i) if len(later) else np.nan
                v = vol20[i]
                if v and not np.isnan(v):
                    p0 = bs_call(entry, entry, CALL_DTE / 365, v)
                    s1 = c[e + CALL_HOLD - 1]
                    p1 = bs_call(s1, entry, (CALL_DTE - CALL_HOLD * 7 / 5) / 365, v)
                    r["call10"] = p1 / p0 - 1 if p0 > 0 else np.nan
                else:
                    r["call10"] = np.nan
                rows.append(r)

    sig = pd.DataFrame(rows)
    sig.to_csv(args.out, index=False)
    print(f"Saved {len(sig)} signals to {args.out}", file=sys.stderr)

    def pct(x):
        return f"{x * 100:+.2f}%"

    out = []
    for name in VARIANTS:
        s = sig[sig.variant == name]
        if s.empty:
            continue
        row = {"variant": name, "signals": len(s), "per_year": round(len(s) / (len(spy) / 252), 1)}
        for n in HORIZONS:
            row[f"avg{n}"] = pct(s[f"ret{n}"].mean())
            row[f"med{n}"] = pct(s[f"ret{n}"].median())
            row[f"win{n}"] = f"{(s[f'ret{n}'] > 0).mean() * 100:.0f}%"
            row[f"vsSPY{n}"] = pct((s[f"ret{n}"] - s[f"spy{n}"]).mean())
        row["hit+5%_20d"] = f"{(s.max20 >= 0.05).mean() * 100:.0f}%"
        row["hit+10%_20d"] = f"{(s.max20 >= 0.10).mean() * 100:.0f}%"
        row["call10_avg"] = pct(s.call10.mean())
        row["call10_med"] = pct(s.call10.median())
        row["call10_win"] = f"{(s.call10 > 0).mean() * 100:.0f}%"
        row["crossed_within_5d"] = f"{s.crossed_5d.mean() * 100:.0f}%"
        row["avg_days_to_cross"] = round(s.days_to_cross.mean(), 1)
        fs = s[~s.crossed_5d]
        row["false_starts"] = f"{len(fs) / len(s) * 100:.0f}%" if name != "TRIGGERED" else "-"
        row["false_start_call10_med"] = pct(fs.call10.median()) if name != "TRIGGERED" and len(fs) else "-"
        out.append(row)
    summary = pd.DataFrame(out).set_index("variant").T
    pd.set_option("display.width", 200, "display.max_rows", 100)
    print(summary.to_string())

    # Year-by-year 10-day average, to see whether one regime drives the result
    sig["year"] = pd.to_datetime(sig.signal_date).dt.year
    by_year = sig.pivot_table(index="year", columns="variant", values="ret10", aggfunc=["mean", "count"])
    print("\n10-day average return by year (count):")
    for y, rr in by_year.iterrows():
        print(y, "  ".join(f"{v}: {rr[('mean', v)] * 100:+.2f}% ({int(rr[('count', v)])})"
                          for v in VARIANTS if ("mean", v) in rr and not pd.isna(rr[("mean", v)])))

    # Significance of each variant against the current rule
    base = sig[sig.variant == "TRIGGERED"]
    print("\nDifference vs TRIGGERED (in standard errors; about 2 or more is meaningful):")
    for v in VARIANTS[1:]:
        a = sig[sig.variant == v]
        parts = []
        for col in ("ret10", "ret20", "call10"):
            x, y2 = a[col].dropna(), base[col].dropna()
            se = math.sqrt(x.var() / len(x) + y2.var() / len(y2))
            parts.append(f"{col} {(x.mean() - y2.mean()) * 100:+.2f}% ({(x.mean() - y2.mean()) / se:+.1f} SE)")
        print(f"  {v:<10} " + "   ".join(parts))


if __name__ == "__main__":
    main()
