#!/usr/bin/env python3
"""
backtest_sectors.py - does capping picks per sector make the daily top 10 safer?

Rebuilds the email's top 10 for every trading day of the last 5 years with the live rules
(same setups, statuses, scores and tiers as breakout_scanner.evaluate and notify.top10,
skipping stocks down 2 days in a row), then compares:
  NO CAP     the top 10 as ranked
  CAP 3      at most 3 picks from one GICS sector
  CAP 2      at most 2 picks from one sector

Each pick is bought at the next day's open. Measured per pick (stock at 10 and 20 days, call proxy
with +50% take profit and with your exit plan) and per day's top 10 as a group: the average of the
10 picks, how bad the worst days got, and how often a whole day's picks fell together.

Sectors are today's GICS sectors for today's S&P 500 members. Uses the cached 5-year data.
"""
import math

import numpy as np
import pandas as pd

import backtest_filters as bf
import breakout_scanner as bs

MAXD = 20


def candidates(df):
    """Per-day tier and score with the scanner's precedence (first match wins)."""
    c, h, v = df["Close"], df["High"], df["Volume"]
    f = bs.indicators(c, h, v)
    rsi, macd, sig, hist = f["rsi"], f["macd"], f["sig"], f["hist"]
    d50 = c / f["sma50"] - 1
    rsi_min7, d50_min7 = rsi.rolling(7).min(), d50.rolling(7).min()
    oversold = (rsi_min7 <= 33) & (d50_min7 <= -0.05)
    cross = (macd > sig) & (macd.shift(1) <= sig.shift(1))
    cross2 = cross | cross.shift(1, fill_value=False)
    cross7 = cross.astype(int).rolling(7).max() > 0
    hist_up = hist > hist.shift(1)
    hist_up2 = hist_up & (hist.shift(1) > hist.shift(2))
    liquid = (c >= bs.MIN_PRICE) & (f["dollar_vol20"] >= bs.MIN_DOLLAR_VOL)
    down2 = (c < c.shift(1)) & (c.shift(1) < c.shift(2))
    base = (33 - rsi_min7) + d50_min7.abs() * 100

    trig = oversold & cross2
    turned = oversold & ~trig & cross7 & (hist > 0) & (rsi < 60) & (d50 <= 0)
    watch = oversold & ~trig & ~turned & hist_up & (hist < 0)
    early = oversold & ~trig & ~turned & ~watch & (hist < 0)
    taken = trig | turned | watch | early | oversold
    mom = ~taken & (rsi >= 45) & (rsi <= 60) & (macd > 0) & (hist > 0) & (d50 >= 0) & (d50 <= 0.03)
    sq = ~taken & ~mom & (f["bbw_pct"] <= 0.20) & (c >= 0.97 * f["high20"]) & (d50 > 0)

    tier = pd.Series(np.nan, index=df.index)
    score = pd.Series(np.nan, index=df.index)
    tier[trig], score[trig] = 1, base + 10
    tier[turned], score[turned] = 1, base + hist_up.astype(int) * 5
    tier[mom & hist_up], score[mom & hist_up] = 2, 20 - (rsi - 52).abs() + 5
    tier[sq], score[sq] = 2, (0.20 - f["bbw_pct"]) * 100 + (c >= f["high20"]).astype(int) * 5
    tier[watch], score[watch] = 3, base + hist_up2.astype(int) * 5
    tier[mom & ~hist_up], score[mom & ~hist_up] = 3, 20 - (rsi - 52).abs()
    ok = tier.notna() & liquid & ~down2
    return pd.DataFrame({"tier": tier, "score": score})[ok], f


def main():
    ymap, data = bf.load("5y")
    spy = data["SPY"].dropna(subset=["Close"])
    sectors = pd.read_csv(bs.SP500_CSV).set_index("Symbol")["GICS Sector"].to_dict()

    cands, frames, inds = [], {}, {}
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High", "Low", "Volume"])
        except KeyError:
            continue
        if len(df) < 260:
            continue
        cd, f = candidates(df)
        cd = cd[cd.index >= df.index[200]]
        if cd.empty:
            continue
        cd = cd.assign(ticker=t, sector=sectors.get(t, sectors.get(t.replace(".", "-"), "Unknown")))
        cands.append(cd)
        frames[t], inds[t] = df, f
    allc = pd.concat(cands).rename_axis("date").reset_index()
    print(f"{len(allc)} candidate stock-days across {allc.date.nunique()} trading days")

    def pick(day_rows, cap):
        out, per = [], {}
        for _, r in day_rows.sort_values(["tier", "score"], ascending=[True, False]).iterrows():
            if cap and per.get(r.sector, 0) >= cap:
                continue
            out.append(r)
            per[r.sector] = per.get(r.sector, 0) + 1
            if len(out) == 10:
                break
        return out

    outcome_cache = {}

    def outcome(t, day):
        key = (t, day)
        if key in outcome_cache:
            return outcome_cache[key]
        df, f = frames[t], inds[t]
        i = df.index.get_loc(day)
        e = i + 1
        if e + MAXD >= len(df):
            outcome_cache[key] = None
            return None
        o, c = df["Open"].values, df["Close"].values
        entry = o[e]
        vol = float(np.log(df["Close"]).diff().iloc[max(0, i - 19):i + 1].std() * math.sqrt(252))
        r = {"ret10": c[e + 9] / entry - 1, "ret20": c[e + 19] / entry - 1}
        path = bf.call_path(c[e:e + MAXD], entry, vol) if vol > 0 else None
        if path:
            r["tp50"] = next((p for p in path[:10] if p >= 0.5), path[9])
            rsi, hist, macd, sig = f["rsi"].values, f["hist"].values, f["macd"].values, f["sig"].values
            stop, hit70, val = entry * 0.92, False, path[MAXD - 1]
            for k in range(MAXD):
                d = e + k
                if c[d] < stop:
                    val = path[k]; break
                hit70 = hit70 or rsi[d] >= 70
                if hit70 and ((hist[d] < hist[d - 1] < hist[d - 2] and hist[d - 2] > 0) or (macd[d] < sig[d] and macd[d - 1] >= sig[d - 1])):
                    val = path[k]; break
            r["plan"] = val
        outcome_cache[key] = r
        return r

    results = {}
    for name, cap in (("NO CAP", None), ("CAP 3", 3), ("CAP 2", 2)):
        picks, days = [], []
        for day, rows in allc.groupby("date"):
            chosen = pick(rows, cap)
            outs = []
            for r in chosen:
                oc = outcome(r.ticker, day)
                if oc:
                    outs.append({**oc, "sector": r.sector, "date": day, "ticker": r.ticker, "tier": int(r.tier)})
            if outs:
                picks += outs
                top_share = pd.Series([x["sector"] for x in outs]).value_counts(normalize=True).iloc[0]
                days.append({"date": day, "n": len(outs), "avg10": np.mean([x["ret10"] for x in outs]),
                             "avg20": np.mean([x["ret20"] for x in outs]),
                             "plan": np.nanmean([x.get("plan", np.nan) for x in outs]),
                             "tp50": np.nanmean([x.get("tp50", np.nan) for x in outs]), "top_sector_share": top_share})
        results[name] = (pd.DataFrame(picks), pd.DataFrame(days))

    pct = lambda x: f"{x * 100:+.1f}%"
    table = {}
    for name, (p, d) in results.items():
        table[name] = {
            "picks": len(p), "days": len(d),
            "avg biggest sector share": f"{d.top_sector_share.mean() * 100:.0f}%",
            "pick: stock 10d avg": pct(p.ret10.mean()), "pick: stock 20d avg": pct(p.ret20.mean()),
            "pick: call +50% TP avg": pct(p.tp50.mean()), "pick: call your plan avg": pct(p.plan.mean()),
            "day: 10d avg of the 10": pct(d.avg10.mean()),
            "day: spread of 10d avg (std)": f"{d.avg10.std() * 100:.1f}%",
            "day: worst 5% of days, 10d": pct(d.avg10.quantile(0.05)),
            "days whole top 10 fell 5%+": f"{(d.avg10 <= -0.05).mean() * 100:.1f}%",
            "day: your plan, worst 5% of days": pct(d.plan.quantile(0.05)),
            "day: your plan, avg": pct(d.plan.mean()),
        }
    print(pd.DataFrame(table).to_string())

    base_d = results["NO CAP"][1].set_index("date")
    print("\nPaired by day vs NO CAP (standard errors; days overlap, so treat these as rough):")
    for name in ("CAP 3", "CAP 2"):
        d = results[name][1].set_index("date").join(base_d, rsuffix="_b", how="inner")
        for col in ("avg10", "plan"):
            diff = d[col] - d[col + "_b"]
            print(f"  {name} {col}: {pct(diff.mean())} ({diff.mean() / (diff.std() / math.sqrt(len(diff))):+.1f} SE)")

    p, d = results["NO CAP"]
    print("\nBy group within the top 10 (NO CAP):")
    for tnum, label in ((1, "Ready"), (2, "Building"), (3, "Setting up")):
        g = p[p.tier == tnum]
        print(f"  {label:<11} share {len(g) / len(p) * 100:.0f}%   stock 10d {pct(g.ret10.mean())}  20d {pct(g.ret20.mean())}   "
              f"call +50% TP {pct(g.tp50.mean())} (median {pct(g.tp50.median())})   your plan {pct(g.plan.mean())} (median {pct(g.plan.median())})")
    worst = d.nsmallest(5, "avg10")
    print("\nWorst 5 days with NO CAP (10-day average of the top 10, biggest sector share):")
    for _, w in worst.iterrows():
        print(f"  {w.date.date()}  {pct(w.avg10)}  {w.top_sector_share * 100:.0f}% one sector")


if __name__ == "__main__":
    main()
