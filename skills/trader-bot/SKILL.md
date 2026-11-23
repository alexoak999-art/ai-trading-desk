---
name: trader-bot
description: >-
  Paper-trading bot on Alpaca. Sets up an Alpaca paper account step by step
  (keys in an environment file, never in the chat), tests the connection with
  a one-share buy, then runs three rule-based strategies on a schedule: a
  trailing stop with ladder buys (a real stop order kept at Alpaca and raised
  as the price climbs), copying the stock trades a member of Congress
  discloses (House PTR filings), and the options wheel (cash-secured puts,
  then covered calls never below cost, closed early at 50% profit). Paper
  account only. Use when the user says "trading bot", "connect Alpaca",
  "paper trade automatically", "trailing stop", "ladder in", "copy Pelosi",
  "copy Congress trades", "wheel strategy", "sell puts", "covered calls", or
  "how is my bot doing".
argument-hint: "[setup | check | trail | copy | wheel | status]"
---

# trader-bot: an Alpaca paper-trading bot

Three strategies from a Claude course video, rebuilt so they are safe to
leave running: rules decide, the broker holds the protective orders, and
nothing touches real money.

`${CLAUDE_SKILL_DIR}` is this skill's folder. Run everything from the user's
trading project folder; state and a log of every order go to `trader/bot/`.
Commands use `python` (Windows); on macOS or Linux, use `python3` if `python`
is not found. The cron lines below are for macOS/Linux; on Windows use Task Scheduler.

```bash
python "${CLAUDE_SKILL_DIR}/bot.py" check
python "${CLAUDE_SKILL_DIR}/bot.py" test-buy AAPL
python "${CLAUDE_SKILL_DIR}/bot.py" trail start TSLA --qty 10 --stop 10 --trigger 10 --trail 5 --ladder 5:5,8:10
python "${CLAUDE_SKILL_DIR}/bot.py" trail run
python "${CLAUDE_SKILL_DIR}/bot.py" copy run --member mccaul --dollars 1000
python "${CLAUDE_SKILL_DIR}/bot.py" wheel start TSLA --put-otm 10 --call-over 10 --dte 14-28 --take-profit 50
python "${CLAUDE_SKILL_DIR}/bot.py" wheel run
python "${CLAUDE_SKILL_DIR}/bot.py" status
python "${CLAUDE_SKILL_DIR}/bot.py" --dry-run trail start TSLA --qty 10    # print orders, send nothing
```

## Step 1: the Alpaca paper account (the user does this)

1. Sign up at alpaca.markets. In the dashboard, switch to **Paper Trading**
   (top left). Optionally make a new paper account with the amount to play
   with (the video uses $50,000). One paper account per strategy keeps the
   results apart.
2. On the paper account's home page, **API Keys > Generate**. Paper keys
   start with `PK`. The bot refuses a live key (`AK...`).
3. In the trading project folder, the user creates `trader/alpaca.env`
   themselves, with their editor:

   ```
   ALPACA_API_KEY=PK...
   ALPACA_SECRET_KEY=...
   ```

   then `chmod 600 trader/alpaca.env`. Environment variables of the same
   names work too. **Never ask for the keys, never accept them in the chat,
   never read that file back.** If the user pastes a key, tell them to delete
   the message and regenerate the keys in Alpaca. (The video pastes keys into
   the chat; do not copy that.)
4. For the wheel: the paper account needs options enabled (Account >
   Configure, options level 2 or higher).

## Step 2: test the connection

`bot.py check` shows the paper account number, cash, options level and
whether the market is open. Then `bot.py test-buy AAPL` buys one share (the
video's test) during market hours; the user sees it in the dashboard.

## Step 3: strategy 1, trailing stop with ladder buys

Ask for the symbol, shares, and the four numbers; show the plan in plain words
and get the user's go-ahead before `trail start`.

- **Stop**: a real GTC stop order at Alpaca at entry − `--stop`% (default 10).
  It works even when the computer is off.
- **Trail**: once the price is `--trigger`% (default 10) above entry, the stop
  moves to `--trail`% (default 5) below the highest price seen. It only moves up.
- **Ladder**: `--ladder DROP:QTY,...` buys more on the way down; the stop is
  resized to cover every share. Ladder levels must sit **above** the stop
  (`--ladder 5:5,8:10` with a 10% stop). The video used −20%/−30% with a
  −10% stop; those buys can never happen, and the bot refuses that setup.
- `trail run` every 5 minutes in market hours checks the high, raises the
  stop, fires ladder buys, and notices when the stop has filled.

## Step 4: strategy 2, copy a member of Congress

Uses the Day Trader pack's `trader-pelosi` (official House Clerk filings; it
needs `pdftotext`: `brew install poppler`). The user picks the member; if
asked, show recent filers with `ptr.py index` and say what you see, but do not
pick for them.

- Copies **stock purchases** as a fixed dollar amount (`--dollars`, fractional
  shares) and **sales** only of what the bot holds (a partial sale sells half).
- Skips options (the video copied them; option prices and expirations a month
  later are a different trade), funds and assets without a ticker, and trades
  older than `--max-age` days (default 60).
- Each filing row is copied once, at most `--max-orders` per run.
- Say this every time: filings arrive up to 45 days after the trade, amounts
  are ranges, and the video's "+34.8% vs +15% for the S&P 500" was its own
  backtest, not something this tool verified.

## Step 5: strategy 3, the wheel (options)

`wheel start SYMBOL` stores the rules; `wheel run` every 15 minutes does one
step at a time:

1. No shares: sell 1 cash-secured put about `--put-otm`% (10) below the price,
   expiring in the `--dte` window (14-28 days), at the bid/ask midpoint.
   Never without the cash to buy 100 shares at the strike.
2. If assigned: cost basis = strike − premium received.
3. With 100 shares: sell 1 covered call at or above cost basis + `--call-over`%
   (10). **Never below cost basis.**
4. Any open contract worth `--take-profit`% (50) less than it was sold for is
   bought back; the next one is sold on the following run.
5. Shares called away: record the cycle and go back to step 1.

Tell the user what one contract needs: a $250 stock means $25,000 of cash
held for the put.

## Step 6: run it on a schedule

The checks are plain Python, so a system scheduler runs them without using
Claude at all. Every `run` exits quietly when the market is closed (it asks
Alpaca's clock), so the schedule does not need to know about holidays.
Suggest these crontab lines (`crontab -e`, on a computer that stays on, such
as a Mac mini), with the user's own folder:

```
*/5  * * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py trail run >> trader/bot/cron.log 2>&1
*/15 * * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py wheel run >> trader/bot/cron.log 2>&1
0 */2 * * 1-5   cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py copy run --member mccaul >> trader/bot/cron.log 2>&1
```

Or a Claude Desktop scheduled task with the same command, which uses tokens on
every run. For a daily report, a scheduled task after 4:00 PM ET that runs
`bot.py status`, reads `trader/bot/log.jsonl` for today, and summarizes.

## Rules

- Paper only. The code has no live-trading address. Never help switch it to
  live; that is the user's own decision, outside this skill.
- Get the user's go-ahead before `trail start`, `wheel start`, or the first
  `copy run`. Scheduled runs then follow the rules the user set.
- No `bypass permissions` needed: the scheduler runs Python, not Claude.
- A rule-based bot is not a prediction. Stops can fill below their price in a
  gap. Options can be assigned early. Not financial advice.
