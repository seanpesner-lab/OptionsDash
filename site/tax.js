/* tax.js - US federal tax estimate for trading gains. Pure functions, no storage, no network.
 *
 * 2026 figures from IRS Rev. Proc. 2025-32 (sections 4.01, 4.03, 4.14).
 * NIIT thresholds are set by law and not inflation-adjusted.
 * Estimates only: ignores AMT, credits, phase-outs, QBI, Section 1256 contracts and wash-sale basis rules.
 */
(function (root) {
  var YEARS = {
    2026: {
      source: 'IRS Rev. Proc. 2025-32',
      brackets: {   // upper bound of each band; rates 10, 12, 22, 24, 32, 35, 37
        single: [12400, 50400, 105700, 201775, 256225, 640600],
        mfj:    [24800, 100800, 211400, 403550, 512450, 768700],
        mfs:    [12400, 50400, 105700, 201775, 256225, 384350],
        hoh:    [17700, 67450, 105700, 201750, 256200, 640600]
      },
      rates: [0.10, 0.12, 0.22, 0.24, 0.32, 0.35, 0.37],
      standard: {single: 16100, mfj: 32200, mfs: 16100, hoh: 24150},
      ltcg: {       // [max 0% amount, max 15% amount] of total taxable income
        single: [49450, 545500], mfj: [98900, 613700], mfs: [49450, 306850], hoh: [66200, 579600]
      },
      niit: {single: 200000, mfj: 250000, mfs: 125000, hoh: 200000},
      lossLimit: {single: 3000, mfj: 3000, mfs: 1500, hoh: 3000},
      estimatedDue: ['2026-04-15', '2026-06-15', '2026-09-15', '2027-01-15']
    }
  };

  function round2(x) { return Math.round(x * 100) / 100; }

  function ordinaryTax(taxable, y, status) {
    var b = y.brackets[status], r = y.rates, tax = 0, lo = 0;
    for (var i = 0; i < r.length; i++) {
      var hi = i < b.length ? b[i] : Infinity;
      if (taxable > lo) tax += (Math.min(taxable, hi) - lo) * r[i];
      lo = hi;
    }
    return tax;
  }

  function marginalOrdinaryRate(taxable, y, status) {
    var b = y.brackets[status];
    for (var i = 0; i < b.length; i++) if (taxable < b[i]) return y.rates[i];
    return y.rates[y.rates.length - 1];
  }

  /* One closed trade -> {cost, proceeds, gain, term, days}.
   * trade: {kind: 'option'|'shares', qty, buy, sell, fees, opened, closed, disallowed}
   * buy/sell are per share for options (the quoted premium), so a contract is x100. */
  function tradeResult(t) {
    var mult = t.kind === 'shares' ? 1 : 100;
    var qty = Number(t.qty) || 0, fees = Number(t.fees) || 0;
    var cost = (Number(t.buy) || 0) * mult * qty;
    var proceeds = (Number(t.sell) || 0) * mult * qty;
    var gain = proceeds - cost - fees;
    var disallowed = Math.max(0, Number(t.disallowed) || 0);
    if (gain < 0) gain = Math.min(0, gain + disallowed);   // wash-sale loss you can't take yet
    var term = 'short', days = null;
    if (t.opened && t.closed) {
      var o = new Date(t.opened + 'T00:00:00Z'), c = new Date(t.closed + 'T00:00:00Z');
      days = Math.round((c - o) / 864e5);
      var anniv = new Date(o); anniv.setUTCFullYear(anniv.getUTCFullYear() + 1);
      if (c > anniv) term = 'long';                          // held more than one year
    }
    return {cost: round2(cost), proceeds: round2(proceeds), gain: round2(gain), term: term, days: days};
  }

  /* Net short- and long-term results the way Schedule D does. */
  function netCapital(st, lt) {
    var netST = 0, netLT = 0, loss = 0;
    if (st >= 0 && lt >= 0) { netST = st; netLT = lt; }
    else if (st < 0 && lt < 0) { loss = -(st + lt); }
    else if (st < 0) { var a = lt + st; if (a >= 0) netLT = a; else loss = -a; }
    else { var b = st + lt; if (b >= 0) netST = b; else loss = -b; }
    return {netST: netST, netLT: netLT, netLoss: loss};
  }

  function federal(profile, st, lt, y) {
    var status = profile.status, other = Math.max(0, Number(profile.otherIncome) || 0);
    var ded = profile.itemized > 0 ? Number(profile.itemized) : y.standard[status];
    var n = netCapital(st, lt);
    var lossDed = Math.min(n.netLoss, y.lossLimit[status]);
    var agi = other + n.netST + n.netLT - lossDed;
    var taxable = Math.max(0, agi - ded);
    var pref = Math.min(n.netLT, taxable);
    var ord = taxable - pref;
    var oTax = ordinaryTax(ord, y, status);
    var z = y.ltcg[status][0], f = y.ltcg[status][1];
    var at0 = Math.max(0, Math.min(taxable, z) - ord);
    var at15 = Math.max(0, Math.min(taxable, f) - Math.max(ord, z));
    var at20 = Math.max(0, pref - at0 - at15);
    var lTax = at15 * 0.15 + at20 * 0.20;
    var nii = n.netST + n.netLT;
    var niit = Math.max(0, Math.min(nii, agi - y.niit[status])) * 0.038;
    return {
      net: n, lossDeduction: lossDed, carryover: n.netLoss - lossDed, agi: agi, deduction: ded,
      taxable: taxable, ordinaryTaxable: ord, preferential: pref,
      ordinaryTax: oTax, ltTax: lTax, ltAt0: at0, ltAt15: at15, ltAt20: at20, niit: niit,
      total: oTax + lTax + niit, marginal: marginalOrdinaryRate(ord, y, status)
    };
  }

  /* Year-to-date estimate. profile: {status, otherIncome, itemized, stateRate (%), carryST, carryLT}
   * trades: closed trades already filtered to the year. */
  function estimate(year, profile, trades) {
    var y = YEARS[year];
    if (!y) throw new Error('No tax figures for ' + year);
    var st = -(Math.max(0, Number(profile.carryST) || 0)), lt = -(Math.max(0, Number(profile.carryLT) || 0));
    var rows = trades.map(function (t) { var r = tradeResult(t); if (r.term === 'long') lt += r.gain; else st += r.gain; return r; });
    var tradingST = rows.filter(function (r) { return r.term === 'short'; }).reduce(function (s, r) { return s + r.gain; }, 0);
    var tradingLT = rows.filter(function (r) { return r.term === 'long'; }).reduce(function (s, r) { return s + r.gain; }, 0);
    var withT = federal(profile, st, lt, y);
    var without = federal(profile, 0, 0, y);
    var fedIncrement = withT.total - without.total;
    var stateRate = Math.max(0, Number(profile.stateRate) || 0) / 100;
    // States mostly tax capital gains as ordinary income; flat-rate approximation on the net gain or deductible loss.
    var stateBase = withT.net.netST + withT.net.netLT - withT.lossDeduction;
    var state = stateBase * stateRate;
    var netGain = tradingST + tradingLT;
    // What one more $1,000 of short-term gain would cost, all in.
    var plus = federal(profile, st + 1000, lt, y);
    var nextRate = (plus.total - withT.total) / 1000 + (st + 1000 > 0 || lt > 0 ? stateRate : 0);
    var setAside = Math.max(0, fedIncrement + state);
    return {
      year: Number(year), source: y.source,
      tradingST: round2(tradingST), tradingLT: round2(tradingLT), netGain: round2(netGain),
      federal: withT, federalWithout: without,
      federalFromTrading: round2(fedIncrement), niit: round2(withT.niit), state: round2(state),
      totalFromTrading: round2(fedIncrement + state), setAside: round2(setAside),
      keep: round2(netGain - (fedIncrement + state)),
      effectiveRate: netGain > 0 ? (fedIncrement + state) / netGain : null,
      nextRate: nextRate, estimatedDue: y.estimatedDue, trades: rows
    };
  }

  /* Same ticker reopened within 30 days of closing at a loss: a possible wash sale. */
  function washFlags(trades) {
    var flags = {};
    trades.forEach(function (t, i) {
      var r = tradeResult(t);
      if (r.gain >= 0 || !t.closed) return;
      var c = new Date(t.closed + 'T00:00:00Z');
      trades.forEach(function (u, j) {
        if (i === j || !u.opened || String(u.ticker).toUpperCase() !== String(t.ticker).toUpperCase()) return;
        var d = Math.abs((new Date(u.opened + 'T00:00:00Z') - c) / 864e5);
        if (d <= 30 && u.opened !== t.opened) flags[i] = true;
      });
    });
    return flags;
  }

  var api = {YEARS: YEARS, ordinaryTax: ordinaryTax, tradeResult: tradeResult, netCapital: netCapital,
             federal: federal, estimate: estimate, washFlags: washFlags};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Tax = api;
})(this);
