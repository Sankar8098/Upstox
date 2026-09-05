# Upstox NSE Stock Telegram Bot (GitHub / Render / Railway)

Stocks only. No futures/options and no automatic order placement.

## What it does

- Downloads Upstox's daily NSE instrument master and filters `NSE_EQ` + `EQ`.
- Gets live quotes in batches.
- Deep-scans daily candles with EMA20/50/200, RSI, MACD, ATR, volume and 20-day levels.
- Adds recent Google News RSS sentiment.
- Ranks setups and sends BUY WATCH / WAIT / AVOID.
- Reads long-term holdings and sends SELL/EXIT WATCH when a holding deteriorates.
- Sends entry, stop-loss and target levels for BUY WATCH.
- Runs on a schedule.
- Includes Render and Railway deployment files.
- Does NOT place orders.

## Important API-load note

A full NSE deep scan can make many historical-data requests. Upstox documents standard API limits of 50 requests/second, 500/minute and 2000/30 minutes for standard APIs. The bot intentionally sleeps between historical calls. Do not schedule many full scans back-to-back.

`DEEP_SCAN_LIMIT=2000` means the bot attempts the whole live NSE equity universe. For a faster scan, use 200-500.

## Environment variables

Copy `.env.example` to `.env` for local use.

Required:
- `UPSTOX_ACCESS_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Schedule:
- `SCAN_TIMES=09:20,12:30,15:15`

## Local

```bash
pip install -r requirements.txt
# set environment variables
python bot.py
```

For a one-off test:
```bash
DRY_RUN=1 python bot.py
```

## Render

Create a Web Service from this GitHub repo. Render will use `render.yaml` if selected, or set:
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

Add secrets as environment variables in Render. Never commit `.env`.

## Railway

Create a project from the GitHub repo. Railway can use `railway.json`. Add the same environment variables.

## Security

Never put Upstox access tokens or Telegram bot tokens in GitHub. If a token is exposed, revoke/regenerate it immediately.

## Disclaimer

Signals are algorithmic research/alerts, not guaranteed returns or investment advice. Verify the setup yourself before trading.
