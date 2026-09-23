# Snagly Bot

Snagly - a Telegram bot for US shoppers (@SnaglyDealsBot): send an AliExpress product link, get back
product details in USD, an affiliate link, and a matching discount code
(when one is configured).

## Files
- `bot.py` - the bot (English UI, USD, ship_to_country=US)
- `aliexpress_client.py` - AliExpress Affiliate API client (defaults: USD / en / US)
- `alternative_finder.py` - "find a cheaper alternative" feature
- `coupons.py` - US discount codes in USD (empty until you add tested codes)
- `stats.py` - usage tracking for the admin-only `/stats` report
- `.env.example` - copy to `.env` and fill in the US account's keys

## Run locally
```bash
pip install -r requirements.txt
python bot.py
```

## Deploy to Railway
Deploy as a **separate** service/repo from the Israeli bot (repo: staketogrow-code/Snagly), with its own
Variables: `TELEGRAM_BOT_TOKEN`, `ALIEXPRESS_APP_KEY`,
`ALIEXPRESS_APP_SECRET`, `ALIEXPRESS_TRACKING_ID` (+ optional `ADMIN_CHAT_ID`,
`ADMIN_USER_IDS`).
Make sure the service region is a US region - AliExpress routes by server IP.
