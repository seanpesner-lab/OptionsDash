/* positions.js - math for the My positions tab. Pure functions, no storage, no network.
 *
 * exitStatus mirrors track.py's exit rules:
 *   1. once RSI(14) has closed at 70+, sell at the first close where momentum fades
 *      (MACD histogram falls two days in a row from above zero, or MACD crosses below its signal)
 *   2. sell if the stock closes 8% or more below the entry price
 *   3. sell by the 20th trading day (the open day is day 1)
 * It runs on completed daily closes from data/indicators.json.
 */
(function (root) {
  var RSI_EXIT = 70, STOP_PCT = 8, MAX_DAYS = 20;

  /* series: {d: dates, c: closes, r: rsi, h: hist, m: macd, s: signal} (oldest first).
   * dropDate: a bar date to ignore because it isn't final yet (today's bar during market hours). */
  function exitStatus(series, opened, entryPrice, dropDate) {
    var stop = entryPrice * (1 - STOP_PCT / 100);
    var out = {state: 'open', reason: '', date: null, close: null, day: 0, rsi70: false,
               rsi: null, stopPrice: Math.round(stop * 100) / 100, lastClose: null, known: true};
    if (!series || !series.d || !series.d.length) { out.known = false; return out; }
    var n = series.d.length;
    if (dropDate && series.d[n - 1] === dropDate) n -= 1;
    if (n < 1) { out.known = false; return out; }
    out.lastClose = series.c[n - 1];
    if (opened < series.d[0]) {
      // Opened before the data window: more than 20 trading days ago, so the time limit has passed.
      out.state = 'closed'; out.reason = 'time'; out.known = false; return out;
    }
    var hit70 = false, day = 0;
    for (var i = 0; i < n; i++) {
      if (series.d[i] < opened) continue;
      day += 1;
      out.day = day; out.rsi = series.r[i];
      var c = series.c[i], h = series.h, m = series.m, s = series.s, reason = '';
      if (c < stop) reason = 'stop';
      else {
        if (series.r[i] >= RSI_EXIT) hit70 = true;
        var fade = (i >= 2 && h[i] < h[i - 1] && h[i - 1] < h[i - 2] && h[i - 2] > 0) ||
                   (i >= 1 && m[i] < s[i] && m[i - 1] >= s[i - 1]);
        if (hit70 && fade) reason = 'momentum';
        else if (day >= MAX_DAYS) reason = 'time';
      }
      out.rsi70 = hit70;
      if (reason) {
        out.state = i === n - 1 ? 'sell' : 'closed';
        out.reason = reason; out.date = series.d[i]; out.close = c;
        return out;
      }
    }
    return out;
  }

  function normCdf(x) {
    // Abramowitz-Stegun 7.1.26, accurate to about 1e-7
    var t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
    var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x / 2);
    return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
  }

  /* Black-Scholes value per share. kind 'call' or 'put'; vol as a decimal; years to expiration. */
  function optionValue(kind, S, K, years, vol, r) {
    r = r == null ? 0.04 : r;
    if (!(S > 0) || !(K > 0)) return null;
    if (years <= 0 || !(vol > 0)) return Math.max(0, kind === 'put' ? K - S : S - K);
    var d1 = (Math.log(S / K) + (r + vol * vol / 2) * years) / (vol * Math.sqrt(years));
    var d2 = d1 - vol * Math.sqrt(years);
    if (kind === 'put') return K * Math.exp(-r * years) * normCdf(-d2) - S * normCdf(-d1);
    return S * normCdf(d1) - K * Math.exp(-r * years) * normCdf(d2);
  }

  /* Implied volatility from a price, by bisection (used to calibrate the estimate at entry). */
  function impliedVol(kind, price, S, K, years, r) {
    var lo = 0.01, hi = 4;
    if (!(price > 0) || years <= 0) return null;
    for (var k = 0; k < 80; k++) {
      var mid = (lo + hi) / 2, v = optionValue(kind, S, K, years, mid, r);
      if (v > price) hi = mid; else lo = mid;
    }
    return (lo + hi) / 2;
  }

  function yearsBetween(fromISO, toISO) {
    return (new Date(toISO + 'T21:00:00Z') - new Date(fromISO + 'T21:00:00Z')) / (365 * 864e5);
  }

  /* P&L for one position given a current price per share. */
  function pnl(pos, price) {
    var mult = 100, qty = Number(pos.qty) || 0;
    var cost = (Number(pos.paid) || 0) * mult * qty + (Number(pos.fees) || 0);
    if (price == null) return {cost: cost, value: null, dollars: null, pct: null};
    var value = price * mult * qty;
    return {cost: cost, value: value, dollars: value - cost, pct: cost > 0 ? (value - cost) / cost * 100 : null};
  }

  /* OCC symbol like NFLX261218C00063000 -> {ticker, expiration, kind, strike}. */
  function parseOcc(sym) {
    var m = /^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(String(sym || '').toUpperCase());
    if (!m) return null;
    return {ticker: m[1], expiration: '20' + m[2] + '-' + m[3] + '-' + m[4], kind: m[5] === 'P' ? 'put' : 'call', strike: Number(m[6]) / 1000};
  }

  var NYSE_HOLIDAYS = ['2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-11-26', '2026-12-25', '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
    '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24'];

  /* The 20th trading day counting the open day as day 1 (the latest sell date). */
  function sellBy(opened, days) {
    days = days || MAX_DAYS;
    var d = new Date(opened + 'T12:00:00Z'), count = 0;
    while (true) {
      var iso = d.toISOString().slice(0, 10), wd = d.getUTCDay();
      if (wd !== 0 && wd !== 6 && NYSE_HOLIDAYS.indexOf(iso) < 0) { count += 1; if (count === days) return iso; }
      d.setUTCDate(d.getUTCDate() + 1);
    }
  }

  var api = {sellBy: sellBy, exitStatus: exitStatus, optionValue: optionValue, impliedVol: impliedVol, yearsBetween: yearsBetween,
             pnl: pnl, parseOcc: parseOcc, RSI_EXIT: RSI_EXIT, STOP_PCT: STOP_PCT, MAX_DAYS: MAX_DAYS};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Positions = api;
})(this);
