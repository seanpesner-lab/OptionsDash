#!/usr/bin/env python3
"""
notify.py - email the top 10 call candidates from the latest scan.

Ranking (rule-based, from your own setups, not advice):
  Tier 1  Ready       REVERSAL TRIGGERED or TURNED
  Tier 2  Building    MOMENTUM TRIGGERED with the MACD histogram rising, or SQUEEZE
  Tier 3  Setting up  REVERSAL WATCH (oversold, selling fading), then MOMENTUM with histogram falling
  Within a tier, higher scanner score first. EARLY and AVOID are never included.
  Earnings within 14 days are flagged, since implied volatility usually drops after the report.

Environment
  SMTP_USER, SMTP_PASSWORD   sending account (for Gmail, an app password)
  EMAIL_TO                   recipient(s), comma-separated
  SMTP_HOST, SMTP_PORT       default smtp.gmail.com, 465
  DASHBOARD_URL              link in the email

Usage
  python notify.py site/data/latest.json             # send
  python notify.py site/data/latest.json --dry-run   # write email.html instead of sending
"""
import argparse
import datetime as dt
import html
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

TIERS = {
    ("REVERSAL", "TRIGGERED"): 1, ("REVERSAL", "TURNED"): 1,
    ("MOMENTUM", "TRIGGERED", True): 2, ("SQUEEZE", "WATCH"): 2,
    ("REVERSAL", "WATCH"): 3, ("MOMENTUM", "TRIGGERED", False): 3,
}
TIER_LABEL = {1: "Ready", 2: "Building", 3: "Setting up"}


def tier(r):
    if r["setup"] == "MOMENTUM":
        return TIERS.get(("MOMENTUM", r["status"], bool(r.get("hist_rising"))))
    return TIERS.get((r["setup"], r["status"]))


def earnings_days(r):
    e, b = r.get("next_earnings"), r.get("bar_date")
    if not e or not b:
        return None
    try:
        return (dt.date.fromisoformat(str(e)[:10]) - dt.date.fromisoformat(b)).days
    except ValueError:
        return None


def reason(r):
    s, st = r["setup"], r["status"]
    if s == "REVERSAL" and st == "TRIGGERED":
        return f"MACD crossed up in the last 2 days after RSI hit {r['rsi_min7']:.0f}; still {abs(r['vs_sma50_pct']):.1f}% below the 50-day."
    if s == "REVERSAL" and st == "TURNED":
        return f"Turned up 3-7 days ago from RSI {r['rsi_min7']:.0f}; still {abs(r['vs_sma50_pct']):.1f}% below the 50-day."
    if s == "REVERSAL":
        return f"Oversold (RSI low {r['rsi_min7']:.0f}), selling fading, MACD not crossed yet."
    if s == "MOMENTUM":
        return f"Pullback in an uptrend: RSI {r['rsi']:.0f}, {r['vs_sma50_pct']:.1f}% above the 50-day, histogram {'rising' if r.get('hist_rising') else 'falling'}."
    if s == "SQUEEZE":
        return f"Bollinger bands in the tightest 20% of 6 months, close near the 20-day high."
    return ""


def top10(rows, n=10):
    cands = [r for r in rows if tier(r)]
    cands.sort(key=lambda r: (tier(r), -(r.get("score") or 0)))
    return cands[:n]


def contract_line(c):
    exp = dt.date.fromisoformat(c["expiration"])
    s = (f"{exp:%b} {exp.day} {c['strike']:g}C: mid {c['mid']:.2f} (${c['cost']:,}), spread {c['spread_pct']:.1f}%, "
         f"delta {c['delta']:.2f}, needs {(c['move_pct'] or 0.0):+.1f}% by expiration")
    return s + (", expires after earnings" if c.get("after_earnings") else "")


def exit_line(ticker, c, stop):
    """Exit plan: take profit at +50%, sell by the 10th trading day, stop if the stock closes 8% lower."""
    parts = []
    if c and c.get("take_profit"):
        parts.append(f"take profit at {c['take_profit']:.2f} (+50%)")
    if c and c.get("sell_by"):
        sb = dt.date.fromisoformat(c["sell_by"])
        parts.append(f"otherwise sell {sb:%a %b} {sb.day}")
    if stop:
        parts.append(f"stop if {ticker} closes below {stop:.2f} (-8%)")
    return "; ".join(parts)


def build(data, url, options=None):
    meta, picks = data.get("meta", {}), top10(data.get("rows", []))
    stocks = (options or {}).get("stocks", [])
    best = {s["ticker"]: (s.get("contracts") or [None])[0] for s in stocks}
    stops = {s["ticker"]: s.get("stop_price") for s in stocks}
    bar = meta.get("bar_date") or "?"
    prov = meta.get("provisional")
    d = dt.date.fromisoformat(bar) if bar != "?" else None
    day = f"{d:%a %b} {d.day}" if d else bar
    subject = f"Top {len(picks)} call setups - {day}" + (" (intraday, provisional)" if prov else " close")

    # Plain text
    lines = [f"Top call setups from your S&P 500 scan, {'prices so far on' if prov else 'close of'} {bar}.", ""]
    for i, r in enumerate(picks, 1):
        ed = earnings_days(r)
        warn = f"  Earnings in {ed} days." if ed is not None and 0 <= ed <= 14 else ""
        lines.append(f"{i:>2}. {r['ticker']:<6} {TIER_LABEL[tier(r)]:<10} {r['setup'].title()} {r['status'].lower()}  "
                     f"close {r['close']:.2f}  RSI {r['rsi']:.1f}  score {r['score']:.1f}")
        lines.append(f"    {reason(r)}{warn}")
        if best.get(r["ticker"]):
            lines.append(f"    Best-fit call: {contract_line(best[r['ticker']])}")
            lines.append(f"    Exit plan: {exit_line(r['ticker'], best[r['ticker']], stops.get(r['ticker']))}")
    if not picks:
        lines.append("No stock fits a call setup today.")
    lines += ["", f"Dashboard: {url}", "",
              "Ranked by your scanner's rules: Ready (reversal triggered or turned), then Building (momentum, squeeze), "
              "then Setting up (oversold watch). Exit plan per trade: take profit when the call is up 50%, "
              "otherwise sell after 10 trading days, and sell if the stock closes 8% below its price at entry. "
              "Rule matches, not advice. Check the option chain, spread and earnings date before trading."]
    text = "\n".join(lines)

    # HTML
    e = html.escape
    rows_html = []
    for i, r in enumerate(picks, 1):
        ed = earnings_days(r)
        warn = (f'<div style="color:#a96a00;font-weight:600;margin-top:2px">Earnings in {ed} day{"s" if ed != 1 else ""}: IV usually drops after the report</div>'
                if ed is not None and 0 <= ed <= 14 else "")
        c = best.get(r["ticker"])
        if c:
            warn += (f'<div style="margin-top:4px;font:12px monospace;color:#14201b;background:#f0f4f2;border-radius:4px;padding:4px 6px">'
                     f'Best-fit call: {e(contract_line(c))}</div>'
                     f'<div style="margin-top:3px;font-size:12px;color:#3d4a44">Exit plan: '
                     f'{e(exit_line(r["ticker"], c, stops.get(r["ticker"])))}</div>')
        t = tier(r)
        badge ={1: "#1a7f37", 2: "#3756c8", 3: "#a96a00"}[t]
        rows_html.append(f"""
<tr>
  <td style="padding:10px 8px;border-top:1px solid #e3e8e5;color:#5b6a63;font:13px monospace;vertical-align:top">{i}</td>
  <td style="padding:10px 8px;border-top:1px solid #e3e8e5;vertical-align:top">
    <div style="font:600 16px monospace">{e(r['ticker'])}</div>
    <div style="font-size:12px;color:{badge};font-weight:600">{TIER_LABEL[t]}</div>
  </td>
  <td style="padding:10px 8px;border-top:1px solid #e3e8e5;vertical-align:top;font-size:13px;line-height:1.45">
    <div><b>{e(r['setup'].title())}</b> &middot; {e(r['status'].lower())}</div>
    <div style="color:#3d4a44">{e(reason(r))}</div>{warn}
  </td>
  <td style="padding:10px 8px;border-top:1px solid #e3e8e5;vertical-align:top;text-align:right;font:13px monospace;white-space:nowrap">
    {r['close']:.2f}<br><span style="color:#5b6a63">RSI {r['rsi']:.1f}</span><br><span style="color:#5b6a63">score {r['score']:.1f}</span>
  </td>
</tr>""")
    banner = ('<p style="background:#fff6e0;border:1px solid #f0d58a;border-radius:6px;padding:8px 12px;font-size:13px">'
              "Intraday run: these use today's prices so far and can change before the close.</p>") if prov else ""
    body = "".join(rows_html) or '<tr><td style="padding:12px;color:#5b6a63">No stock fits a call setup today.</td></tr>'
    html_doc = f"""<!doctype html><html><body style="margin:0;background:#f5f7f6;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#14201b">
<div style="max-width:640px;margin:0 auto;padding:24px 16px">
  <h1 style="font-size:20px;margin:0 0 4px">Top {len(picks)} call setups</h1>
  <p style="margin:0 0 16px;color:#5b6a63;font-size:13px">S&amp;P 500 scan, {'prices so far on' if prov else 'close of'} {e(bar)} &middot; {meta.get('hits', 0)} stocks matched a setup</p>
  {banner}
  <table role="presentation" style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8e5;border-radius:8px">{body}</table>
  <p style="margin:18px 0"><a href="{e(url)}" style="background:#0f766e;color:#fff;text-decoration:none;padding:9px 16px;border-radius:6px;font-size:14px;display:inline-block">Open the dashboard</a></p>
  <p style="font-size:12px;color:#5b6a63;line-height:1.5">Ranked by your scanner's rules: <b>Ready</b> (reversal triggered or turned), then <b>Building</b> (momentum with rising MACD histogram, squeeze), then <b>Setting up</b> (oversold, selling fading). Higher score first within each group. Exit plan per trade: take profit when the call is up 50%, otherwise sell after 10 trading days, and sell if the stock closes 8% below its price at entry. These are rule matches, not advice. Check the option chain, bid-ask spread and earnings date before trading.</p>
</div></body></html>"""
    return subject, text, html_doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--options", help="options.json from options_scan.py, to add the best-fit contract per stock")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with open(args.json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    options = None
    if args.options and os.path.exists(args.options):
        with open(args.options, encoding="utf-8") as fh:
            options = json.load(fh)
    url = os.environ.get("DASHBOARD_URL", "https://seanpesner-lab.github.io/OptionsDash/")
    subject, text, html_doc = build(data, url, options)

    if args.dry_run:
        with open("email.html", "w", encoding="utf-8") as fh:
            fh.write(html_doc)
        print(subject)
        print(text)
        return

    user, pwd, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if not (user and pwd and to):
        sys.exit("Set SMTP_USER, SMTP_PASSWORD and EMAIL_TO to send email.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(html_doc, subtype="html")
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
