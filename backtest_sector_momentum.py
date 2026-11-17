#!/usr/bin/env python3
"""
backtest_sector_momentum.py - does sector momentum or rotation make the scanner's setups work better?

Sector index: equal-weighted daily return of each GICS sector's S&P 500 members.
Each day, every sector gets:
  mom20 / mom63   sector return minus SPY return over the last 20 / 63 trading days
  rank20 / rank63 1 = strongest of the 11 sectors
  rotating_in     rank20 improved by 3 or more places over the last 10 days
  rotating_out    rank20 worsened by 3 or more places over the last 10 days

Part 1: every setup the scanner finds (first day of each, 10-day cooldown per stock), split by
its sector's rank and rotation. Part 2: the daily top 10 rebuilt with the live rules, compared with
versions that only take picks from the strongest sectors or rank them first.

Entry next open; stock returns at 10/20 days, "faltered" (never 3% up in 10 days and below entry
on day 10), and the call proxy with +50% take profit and with your exit plan.
Uses the cached 5-year data and today's sector membership (survivorship bias applies).
"""
import math

import numpy as np
import pandas as pd

import backtest as bt
import backtest_filters as bf
import backtest_sectors as bsec
import breakout_scanner as bs

MAXD = 20


def outcome(df, f, i):
    e = i + 1
    if e + MAXD >= len(df):
        return None
    o, c, h = df["Open"].values, df["Close"].values, df["High"].values
    entry = o[e]
    vol = float(np.log(df["Close"]).diff().iloc[max(0, i - 19):i + 1].std() * math.sqrt(252))
    r = {"ret10": c[e + 9] / entry - 1, "ret20": c[e + 19] / entry - 1}
    r["faltered"] = (h[e:e + 10].max() / entry - 1) < 0.03 and r["ret10"] < 0
    path = bf.call_path(c[e:e + MAXD], entry, vol) if vol > 0 else None
    if not path:
        return None
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
    return r


def main():
    ymap, data = bf.load("5y")
    spy_c = data["SPY"]["Close"].dropna()
    sectors = pd.read_csv(bs.SP500_CSV).set_index("Symbol")["GICS Sector"].to_dict()

    rets, frames, inds, sec_of = {}, {}, {}, {}
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High", "Low", "Volume"])
        except KeyError:
            continue
        if len(df) < 260:
            continue
        sec = sectors.get(t) or sectors.get(t.replace(".", "-"))
        if not sec:
            continue
        frames[t], sec_of[t] = df, sec
        rets[t] = df["Close"].pct_change()

    # Equal-weight sector indexes and their relative strength vs SPY
    R = pd.DataFrame(rets)
    sec_ret = R.T.groupby(pd.Series(sec_of)).mean().T          # daily return per sector
    sec_idx = (1 + sec_ret.fillna(0)).cumprod()
    spy = spy_c.reindex(sec_idx.index).ffill()
    mom20 = sec_idx.pct_change(20).sub(spy.pct_change(20), axis=0)
    mom63 = sec_idx.pct_change(63).sub(spy.pct_change(63), axis=0)
    rank20 = mom20.rank(axis=1, ascending=False)
    rank63 = mom63.rank(axis=1, ascending=False)
    rank_chg = rank20.shift(10) - rank20                          # positive = moved up the table
    n_sec = sec_idx.shape[1]
    print(f"{n_sec} sectors, {len(frames)} stocks\n")

    # ---------------- Part 1: every setup, by its sector's momentum
    rows, cand_all = [], []
    for t, df in frames.items():
        cd, f = bsec.candidates(df)
        inds[t] = f
        cd = cd[cd.index >= df.index[200]]
        if cd.empty:
            continue
        cand_all.append(cd.assign(ticker=t, sector=sec_of[t]).rename_axis("date").reset_index())
        mask = pd.Series(False, index=df.index)
        mask[cd.index] = True
        for i in bt.first_days(mask):
            day = df.index[i]
            oc = outcome(df, f, i)
            if not oc:
                continue
            s = sec_of[t]
            rows.append({**oc, "ticker": t, "date": day, "tier": int(cd.loc[day, "tier"]), "sector": s,
                         "rank20": rank20.at[day, s], "rank63": rank63.at[day, s], "rank_chg": rank_chg.at[day, s]})
    p = pd.DataFrame(rows).dropna(subset=["rank20", "rank63", "rank_chg"])
    pct = lambda x: f"{x * 100:+.1f}%"

    def bucket_table(groups, title):
        print(title)
        print(f"  {'':<28}{'n':>6}{'10d':>8}{'20d':>8}{'falter':>8}{'TP50 avg':>10}{'TP50 med':>10}{'plan avg':>10}{'plan med':>10}")
        for name, g in groups:
            if len(g) < 30:
                continue
            print(f"  {name:<28}{len(g):>6}{pct(g.ret10.mean()):>8}{pct(g.ret20.mean()):>8}{g.faltered.mean() * 100:>7.0f}%"
                  f"{pct(g.tp50.mean()):>10}{pct(g.tp50.median()):>10}{pct(g.plan.mean()):>10}{pct(g.plan.median()):>10}")

    def se_diff(a, b, col):
        se = math.sqrt(a[col].var() / len(a) + b[col].var() / len(b))
        return f"{pct(a[col].mean() - b[col].mean())} ({(a[col].mean() - b[col].mean()) / se:+.1f} SE)"

    top, bot = p[p.rank20 <= 3], p[p.rank20 >= n_sec - 2]
    bucket_table([("All setups", p), ("Sector top 3 (1-month)", top), ("Sector middle", p[(p.rank20 > 3) & (p.rank20 < n_sec - 2)]),
                  ("Sector bottom 3 (1-month)", bot)], "Part 1a: by sector strength over the last month")
    print(f"  top 3 minus bottom 3: 20d {se_diff(top, bot, 'ret20')}   plan {se_diff(top, bot, 'plan')}   TP50 {se_diff(top, bot, 'tp50')}\n")
    top63, bot63 = p[p.rank63 <= 3], p[p.rank63 >= n_sec - 2]
    bucket_table([("Sector top 3 (3-month)", top63), ("Sector bottom 3 (3-month)", bot63)], "Part 1b: by sector strength over 3 months")
    print(f"  top 3 minus bottom 3: 20d {se_diff(top63, bot63, 'ret20')}   plan {se_diff(top63, bot63, 'plan')}\n")
    rin, rout, flat = p[p.rank_chg >= 3], p[p.rank_chg <= -3], p[p.rank_chg.abs() < 3]
    bucket_table([("Rotating in (+3 ranks/10d)", rin), ("Steady", flat), ("Rotating out (-3 ranks)", rout)], "Part 1c: by sector rotation")
    print(f"  in minus out: 20d {se_diff(rin, rout, 'ret20')}   plan {se_diff(rin, rout, 'plan')}\n")
    lead_in = p[(p.rank20 <= 4) & (p.rank_chg >= 2)]
    bucket_table([("Leading AND rotating in", lead_in), ("Everything else", p.drop(lead_in.index))], "Part 1d: top 4 sector that is also climbing")
    print()
    for tnum, label in ((1, "Ready (reversals)"), (2, "Building (momentum/squeeze)")):
        g = p[p.tier == tnum]
        bucket_table([(f"{label}: sector top 3", g[g.rank20 <= 3]), (f"{label}: sector bottom 3", g[g.rank20 >= n_sec - 2])], f"Part 1e: {label}")
    p["year"] = pd.to_datetime(p.date).dt.year
    print("\n20d stock return by year, sector top 3 vs bottom 3 (1-month):")
    for y, g in p.groupby("year"):
        a, b = g[g.rank20 <= 3], g[g.rank20 >= n_sec - 2]
        print(f"  {y}  top {pct(a.ret20.mean())} ({len(a)})   bottom {pct(b.ret20.mean())} ({len(b)})")

    # ---------------- Part 2: the daily top 10 with a sector preference
    allc = pd.concat(cand_all)
    allc["rank20"] = [rank20.at[d, s] if d in rank20.index else np.nan for d, s in zip(allc.date, allc.sector)]
    cache = {}

    def oc(t, d):
        k = (t, d)
        if k not in cache:
            cache[k] = outcome(frames[t], inds[t], frames[t].index.get_loc(d))
        return cache[k]

    variants = {
        "LIVE (as now)": lambda g: g.sort_values(["tier", "score"], ascending=[True, False]),
        "Top 6 sectors only": lambda g: g[g.rank20 <= 6].sort_values(["tier", "score"], ascending=[True, False]),
        "Leading sectors first": lambda g: g.assign(lead=(g.rank20 <= 4).astype(int)).sort_values(["lead", "tier", "score"], ascending=[False, True, False]),
        "Skip bottom 3 sectors": lambda g: g[g.rank20 < n_sec - 2].sort_values(["tier", "score"], ascending=[True, False]),
    }
    print("\nPart 2: daily top 10 (picks per day can be fewer than 10 when a filter leaves fewer)")
    print(f"  {'':<24}{'picks':>7}{'10d':>8}{'20d':>8}{'falter':>8}{'TP50 avg':>10}{'plan avg':>10}{'worst 5% days':>15}")
    base_days = None
    for name, fn in variants.items():
        picks, days = [], []
        for d, g in allc.dropna(subset=["rank20"]).groupby("date"):
            out = [x for x in (oc(r.ticker, d) for r in fn(g).head(10).itertuples()) if x]
            if out:
                picks += out
                days.append({"date": d, "avg10": np.mean([x["ret10"] for x in out]), "plan": np.mean([x["plan"] for x in out])})
        pp, dd = pd.DataFrame(picks), pd.DataFrame(days).set_index("date")
        print(f"  {name:<24}{len(pp):>7}{pct(pp.ret10.mean()):>8}{pct(pp.ret20.mean()):>8}{pp.faltered.mean() * 100:>7.0f}%"
              f"{pct(pp.tp50.mean()):>10}{pct(pp.plan.mean()):>10}{pct(dd.avg10.quantile(0.05)):>15}")
        if base_days is None:
            base_days = dd
        else:
            j = dd.join(base_days, rsuffix="_b", how="inner")
            for col in ("avg10", "plan"):
                diff = j[col] - j[col + "_b"]
                print(f"      vs live, {col}: {pct(diff.mean())} ({diff.mean() / (diff.std() / math.sqrt(len(diff))):+.1f} SE, days overlap so rough)")


if __name__ == "__main__":
    main()
