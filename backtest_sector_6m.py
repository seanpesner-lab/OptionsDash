#!/usr/bin/env python3
"""
backtest_sector_6m.py - longer-horizon sector and industry momentum for the scanner's setups.

Same setups, outcomes and caveats as backtest_sector_momentum.py, with momentum measured over
the horizons where it is usually documented:
  SECTOR 6M     sector return minus SPY over the last 126 trading days, ranked among 11 sectors
  SECTOR 12-1   the classic: return from 12 months ago to 1 month ago (skips the latest month)
  INDUSTRY 6M   the same 6-month measure for GICS sub-industries (e.g. Semiconductors) with at
                least 4 members, as a percentile (top 20% / bottom 20%)
Then the daily top 10 rebuilt with each as a filter.
"""
import math

import numpy as np
import pandas as pd

import backtest as bt
import backtest_filters as bf
import backtest_sector_momentum as bsm
import backtest_sectors as bsec
import breakout_scanner as bs


def main():
    ymap, data = bf.load("5y")
    spy_c = data["SPY"]["Close"].dropna()
    meta = pd.read_csv(bs.SP500_CSV).set_index("Symbol")
    rets, frames, inds, sec_of, ind_of = {}, {}, {}, {}, {}
    for yt, t in ymap.items():
        try:
            df = data[yt].dropna(subset=["Close", "Open", "High", "Low", "Volume"])
        except KeyError:
            continue
        key = t if t in meta.index else t.replace(".", "-")
        if len(df) < 260 or key not in meta.index:
            continue
        frames[t], sec_of[t], ind_of[t] = df, meta.at[key, "GICS Sector"], meta.at[key, "GICS Sub-Industry"]
        rets[t] = df["Close"].pct_change()
    R = pd.DataFrame(rets)
    spy = spy_c.reindex(R.index).ffill()

    def group_scores(groups, min_members=1):
        g = pd.Series(groups)
        counts = g.value_counts()
        keep = counts[counts >= min_members].index
        cols = [t for t in R.columns if g[t] in keep]
        idx = (1 + R[cols].T.groupby(g[cols]).mean().T.fillna(0)).cumprod()
        m6 = idx.pct_change(126).sub(spy.pct_change(126), axis=0)
        m12_1 = (idx.shift(21) / idx.shift(252) - 1).sub(spy.shift(21) / spy.shift(252) - 1, axis=0)
        return m6.rank(axis=1, pct=True, ascending=False), m12_1.rank(axis=1, pct=True, ascending=False)

    sec6, sec12 = group_scores(sec_of)                    # percentile rank, 0 = best
    ind6, _ = group_scores(ind_of, min_members=4)
    print(f"{len(frames)} stocks, 11 sectors, {ind6.shape[1]} sub-industries with 4+ members\n")

    rows, cand_all = [], []
    for t, df in frames.items():
        cd, f = bsec.candidates(df)
        inds[t] = f
        cd = cd[cd.index >= df.index[252]]                 # need a year of history for 12-1
        if cd.empty:
            continue
        s, ind = sec_of[t], ind_of[t]
        cand_all.append(cd.assign(ticker=t, sector=s, industry=ind).rename_axis("date").reset_index())
        mask = pd.Series(False, index=df.index)
        mask[cd.index] = True
        for i in bt.first_days(mask):
            day = df.index[i]
            oc = bsm.outcome(df, f, i)
            if not oc:
                continue
            rows.append({**oc, "date": day, "tier": int(cd.loc[day, "tier"]),
                         "sec6": sec6.at[day, s], "sec12": sec12.at[day, s],
                         "ind6": ind6.at[day, ind] if ind in ind6.columns else np.nan})
    p = pd.DataFrame(rows)
    pct = lambda x: f"{x * 100:+.1f}%"

    def show(title, col, lo=0.27, hi=0.73):
        d = p.dropna(subset=[col])
        top, mid, bot = d[d[col] <= lo], d[(d[col] > lo) & (d[col] < hi)], d[d[col] >= hi]
        print(title)
        print(f"  {'':<10}{'n':>6}{'10d':>8}{'20d':>8}{'falter':>8}{'TP50 avg':>10}{'TP50 med':>10}{'plan avg':>10}{'plan med':>10}")
        for name, g in (("leaders", top), ("middle", mid), ("laggards", bot)):
            print(f"  {name:<10}{len(g):>6}{pct(g.ret10.mean()):>8}{pct(g.ret20.mean()):>8}{g.faltered.mean() * 100:>7.0f}%"
                  f"{pct(g.tp50.mean()):>10}{pct(g.tp50.median()):>10}{pct(g.plan.mean()):>10}{pct(g.plan.median()):>10}")
        out = []
        for c in ("ret20", "tp50", "plan"):
            se = math.sqrt(top[c].var() / len(top) + bot[c].var() / len(bot))
            out.append(f"{c} {pct(top[c].mean() - bot[c].mean())} ({(top[c].mean() - bot[c].mean()) / se:+.1f} SE)")
        print("  leaders minus laggards: " + "   ".join(out))
        d = d.assign(year=pd.to_datetime(d.date).dt.year)
        print("  20d by year, leaders vs laggards: " + "  ".join(
            f"{y} {pct(g[g[col] <= lo].ret20.mean())}/{pct(g[g[col] >= hi].ret20.mean())}" for y, g in d.groupby("year")))
        print()

    show("SECTOR 6-MONTH momentum (leaders = top 3 of 11, laggards = bottom 3)", "sec6")
    show("SECTOR 12-1 MONTH momentum", "sec12")
    show("INDUSTRY 6-MONTH momentum (leaders = top 20%, laggards = bottom 20%)", "ind6", 0.2, 0.8)

    allc = pd.concat(cand_all)
    allc["sec6"] = [sec6.at[d, s] for d, s in zip(allc.date, allc.sector)]
    allc["ind6"] = [ind6.at[d, i] if i in ind6.columns else np.nan for d, i in zip(allc.date, allc.industry)]
    cache = {}

    def oc(t, d):
        k = (t, d)
        if k not in cache:
            cache[k] = bsm.outcome(frames[t], inds[t], frames[t].index.get_loc(d))
        return cache[k]

    order = lambda g: g.sort_values(["tier", "score"], ascending=[True, False])
    variants = {"LIVE (as now)": order,
                "Top half sectors (6M)": lambda g: order(g[g.sec6 <= 0.5]),
                "Skip bottom 3 sectors (6M)": lambda g: order(g[g.sec6 < 0.73]),
                "Top half industries (6M)": lambda g: order(g[g.ind6 <= 0.5])}
    print("Daily top 10 rebuilt with each filter")
    print(f"  {'':<28}{'picks':>7}{'10d':>8}{'20d':>8}{'falter':>8}{'TP50 avg':>10}{'plan avg':>10}{'worst 5% days':>15}")
    base = None
    for name, fn in variants.items():
        picks, days = [], []
        for d, g in allc.groupby("date"):
            out = [x for x in (oc(r.ticker, d) for r in fn(g).head(10).itertuples()) if x]
            if out:
                picks += out
                days.append({"date": d, "avg10": np.mean([x["ret10"] for x in out]), "avg20": np.mean([x["ret20"] for x in out]),
                             "plan": np.mean([x["plan"] for x in out])})
        pp, dd = pd.DataFrame(picks), pd.DataFrame(days).set_index("date")
        print(f"  {name:<28}{len(pp):>7}{pct(pp.ret10.mean()):>8}{pct(pp.ret20.mean()):>8}{pp.faltered.mean() * 100:>7.0f}%"
              f"{pct(pp.tp50.mean()):>10}{pct(pp.plan.mean()):>10}{pct(dd.avg10.quantile(0.05)):>15}")
        if base is None:
            base = dd
        else:
            j = dd.join(base, rsuffix="_b", how="inner")
            print("      vs live: " + "   ".join(
                f"{c} {pct((j[c] - j[c + '_b']).mean())} ({(j[c] - j[c + '_b']).mean() / ((j[c] - j[c + '_b']).std() / math.sqrt(len(j))):+.1f} SE)"
                for c in ("avg20", "plan")) + "   (days overlap, so rough)")


if __name__ == "__main__":
    main()
