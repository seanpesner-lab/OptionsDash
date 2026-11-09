# S&P 500 Setup Scanner

Scans the S&P 500 on daily bars for four setups (Reversal, Momentum, Squeeze, Avoid) and publishes a dashboard to GitHub Pages. During market hours it rescans every 15 minutes and marks results as provisional. The scan after the 4:00 PM close is final, and a copy is saved to `history/`.

## How it runs

`.github/workflows/scan.yml` runs on GitHub's servers every 15 minutes on weekdays from 13:00 to 21:45 UTC. That covers 9:30 AM to 4:00 PM Eastern year-round. It:

1. downloads about two years of daily bars for every S&P 500 stock from Alpaca,
2. runs `breakout_scanner.py` and writes `site/data/latest.json`,
3. after the close, commits `history/YYYY-MM-DD.csv` (once per trading day),
4. publishes `site/` to GitHub Pages.

The page reloads its data every 5 minutes while it's open.

## One-time setup

1. **Alpaca key (free).** Sign up at alpaca.markets. In the dashboard, generate API keys. Paper-trading keys work for market data.
2. **Repository.** Create a new **public** GitHub repository and push this folder to it. GitHub Pages on a private repository needs a paid GitHub plan.
3. **Pages.** In the repository, go to Settings → Pages → Build and deployment and set Source to **GitHub Actions**.
4. **Secrets.** Go to Settings → Secrets and variables → Actions → New repository secret and add:
   - `ALPACA_API_KEY_ID`
   - `ALPACA_API_SECRET_KEY`
   - `ALPHAVANTAGE_API_KEY` (optional, for earnings dates; the free key is enough because the calendar is fetched once a day)
5. **First run.** Go to Actions → Scan and publish → Run workflow. When it finishes, the page is at `https://<your-username>.github.io/<repo-name>/`.

## Daily email

Each weekday morning (around 7:30 AM Eastern in winter, 8:30 AM in summer), `notify.py` emails the top 10 call setups from the previous day's final close, with the best-fit call contract for each. `history/emailed.txt` records the days already sent, so it goes out once a day. They are ranked Ready (reversal triggered or turned), then Building (momentum with rising MACD histogram, squeeze), then Setting up (oversold watch), with the higher score first in each group. Earnings within 14 days are flagged.

To turn it on, add these repository secrets:

- `SMTP_USER`: the Gmail address that sends the email
- `SMTP_PASSWORD`: a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords), not your normal password
- `EMAIL_TO`: where to send it; separate several addresses with commas

To send a test now, go to Actions → Scan and publish → Run workflow and tick **send_email**.

## Track record

The morning run also appends that day's top 10, with the best-fit call and its quote, to `history/picks.csv`. Every run then scores all recorded picks (`track.py update`) into the **Track record** tab:

- **Stock:** return from the signal day's close to the latest price, and after 5, 10 and 20 trading days, each compared with SPY over the same days.
- **Call:** return from the mid price when picked to the current mid. Once expired, the call is valued at the stock's close on expiration minus the strike, or zero.

Picks made since the last close are listed but left out of the averages until they've had a trading day to move.

## Running locally

```bash
pip install -r requirements.txt
python breakout_scanner.py --earnings --site site
python -m http.server 8765 --directory site
```

Then open http://localhost:8765. Without Alpaca keys in the environment, the scanner uses Yahoo Finance.

## Notes

- The free Alpaca plan gives consolidated (SIP) prices delayed 15 minutes, so the page lags the market by at least that much.
- Intraday signals use a daily bar that is still forming and can disappear by the close.
- GitHub may start scheduled runs a few minutes late, and it pauses schedules in repositories with no activity for 60 days. The daily history commit counts as activity.
- The site is public. Market data plans are for personal use, so check your provider's terms before sharing the link widely.
- These are rule matches, not trading advice.
