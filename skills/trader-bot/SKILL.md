---
name: trader-bot
description: >-
  Paper-trading bot on Alpaca. Sets up an Alpaca paper account step by step
  (keys in an environment file, never in the chat), tests the connection with
  a one-share buy, then runs twelve rule-based strategies on a schedule: a
  trailing stop with ladder buys (a real stop order kept at Alpaca and raised
  as the price climbs), copying the stock trades a member of Congress
  discloses (House PTR filings), and the options wheel (cash-secured puts,
  then covered calls never below cost, closed early at 50% profit), and a
  trend swing (buy when a stock is above a rising 200-day average and its
  3-month rate of change has reset near zero, stop at Alpaca, sell after ~3
  months or on a close below the average), and an opening-range breakout day
  trade (buy a fresh 5-minute close above the first 15 minutes' high, as a
  bracket order with the stop at the range midpoint and a 2R target, flat by
  3:55 PM), and an oversold bounce (RSI 14 under 30 yesterday and an up close
  today, 5% stop at Alpaca, sell back at the 20-day average or after ~2
  weeks), and dollar-cost averaging (a fixed dollar amount split across
  chosen names weekly, every other week or monthly, fractional shares, no
  selling: the buy-and-hold baseline), and covered calls only (calls on 100
  shares you hold, 10% above the price and never below cost, bought back at
  50% profit), and rebalancing (money put into target weights, sold back to
  target monthly or quarterly when a name drifts past a band), and a
  gap-and-go day trade (a stock that opens 3%+ above yesterday's close and
  breaks its first 5 minutes' high, stop just under the open, 2R target,
  flat by 3:55 PM), and momentum rotation (rank a list of names by their
  ~6-month return each month, hold the top few, sell what drops out, cash
  when nothing is up), and the First Candle Rule (the 9:30-9:35 candle's
  high and low, then a 1-minute break that leaves a fair value gap, a retest
  and an engulfing candle; long or short, 3R target, flat by 3:55 PM).
  Paper account only. Use when the user says "trading bot",
  "connect Alpaca", "paper trade automatically", "trailing stop", "ladder in",
  "copy Pelosi", "copy Congress trades", "wheel strategy", "sell puts",
  "covered calls", "trend swing", "swing trade the 200-day", "opening range
  breakout", "ORB", "day trade automatically", "oversold bounce", "buy the
  dip on RSI", "dollar-cost averaging", "DCA", "buy every week", "covered calls
  only", "sell calls on my shares", "rebalance",
  "target weights", "60/40", "gap and go",
  "trade the gappers", "momentum rotation", "sector rotation", "hold the leaders", "first candle rule",
  "FVG", "fair value gap", or "how is my bot doing".
argument-hint: "[setup | check | trail | copy | wheel | swing | orb | bounce | dca | cc | rebal | gap | rotate | fcr | status]"
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

## Step 5b: strategy 4, the trend swing

The `/trader-agents` technical rule, traded: `swing start SYMBOL ...` stores
the list and rules; `swing run` once a day near the close (about 3:45 PM ET)
does the work. It needs the trader-agents skill installed next to this one
(it imports its `trend.py`). The user picks the names; do not pick for them.

1. **Entry**: a name's verdict is SETUP: above a rising 200-day average, and
   its 63-day rate of change peaked at +15% or more, then reset to within 5%
   of zero. Buys whole shares for about `--dollars` (2000) at market.
2. **Stop**: a real GTC stop at Alpaca `--stop`% (10) below the fill.
3. **Exits**: the stop fills; or a close below the 200-day average; or
   `--hold-days` (91, about 3 months) have passed. Market sell, stop cancelled.
4. **Limits**: at most `--max-positions` (5) open; never a name an open
   trailing-stop or wheel plan trades, or one held outside the swing; no
   rebuy of a name it closed for `--cooldown` days (21).
5. `swing stop` ends new buys; open positions keep their stops and exits.

Show the plan and `--dry-run swing start ...` first: it previews today's
verdict for every name and the orders it would send. Before the market
closes, Yahoo's daily bar is the latest price, so the signal can change by
the close; that is why it runs near the close.

## Step 5c: strategy 5, the opening-range breakout (day trade)

The Day Trader pack's entry and exit rules, automated: `orb start SYMBOL ...`
stores the list and rules; `orb run` every 5 minutes in market hours does the
work. Long only. The user picks the names; do not pick for them.

1. **Range**: the high and low of the first `--range` minutes (15), from
   Alpaca's 5-minute bars (free IEX feed, so volume is partial).
2. **Entry**: the first 5-minute bar that *closes* above the range high, if
   that bar finished in the last 10 minutes and the price is still above the
   high and not more than half a risk unit past it. A breakout that was
   missed, or failed, is not bought later. No new entries from
   `--last-entry` (11:30 ET).
3. **The order**: one bracket at Alpaca: market buy, stop at the range
   midpoint (`--stop mid`, or `low`), take-profit at `--target-r` (2) times
   the risk. Both legs are held by the broker, so they work between runs.
4. **Size**: whole shares, the smaller of `--dollars` (2000) and
   `--max-loss` (100) divided by the risk per share. A stop too tight for the
   spread is a refusal, not a smaller order.
5. **Limits**: `--max-trades` (3) a day, one per name, never a name another
   strategy holds or trades.
6. **Flat by** `--flat-by` (15:55 ET): the run at that time cancels the legs
   and sells at market. Anything left over (the PC was off) is sold on the
   next run; the GTC legs protect it overnight.

`--dry-run orb start ...` previews what a run would do right now. Runs before
the range is built, or after the last entry time, say so and do nothing new.

## Step 5d: strategy 6, the oversold bounce

The Day Trader scanner's "oversold bounce", traded: `bounce start SYMBOL ...`
stores the list and rules; `bounce run` once a day near the close (about
3:45 PM ET) does the work. Shares its position handling with the trend swing.
The user picks the names; do not pick for them.

1. **Entry**: RSI 14 (Wilder) was under 30 yesterday and today closes up
   (the scanner's rule). With `--above-200`, only while the close is above
   the 200-day average: without it, the rule also buys bounces in stocks
   that are in a downtrend. Whole shares for about `--dollars` (2000).
2. **Stop**: a GTC stop at Alpaca `--stop`% (5) below the fill.
3. **Exits**: the stop fills; or a close back at or above the 20-day
   average; or `--hold-days` (14, about 10 sessions) have passed.
4. **Limits**: `--max-positions` (5); never a name another strategy holds or
   trades; no rebuy for `--cooldown` days (10).
5. `bounce stop` ends new buys; open positions keep their stops and exits.

## Step 5e: strategy 7, dollar-cost averaging

The baseline the others are measured against: `dca start SYM[:WEIGHT] ...`
stores the plan; `dca run` once a day (10:00 AM ET is fine) buys only on its
dates. The user picks the names and weights; do not pick for them.

1. **Buys**: `--dollars` (100) each buy day, split by weight (`SPY:60
   QQQ:40`; no weights = equal), at market. Fractional (notional) orders
   where Alpaca allows; otherwise whole shares, with the remainder carried
   to the next buy.
2. **Dates**: `--every weekly|biweekly|monthly`, `--day mon..fri` or `1-28`.
   The first buy is on the first such day from today. A date the market is
   closed is bought on the next run when it is open.
3. **No stops, no selling.** `--cap` ends it after that many dollars;
   `dca stop` ends it any time. What it bought stays.
4. If cash is short on a buy day it skips to the next date and says so.
5. It keeps its own record of buys, so `status` shows dollars in and what
   they are worth now, even if another strategy trades the same name.

## Step 5f: strategy 8, covered calls only

The second half of the wheel on shares you hold, never selling puts:
`cc start SYMBOL` stores the plan; `cc run` every 15 minutes in market hours
does one step at a time (the same engine as the wheel). The user picks the
name; do not pick for them.

1. **Shares**: 100 per contract (`--contracts`, 1). Without them it refuses
   and says what they would cost; `--buy-shares` buys the missing ones at
   market first. It refuses a name another strategy trades or holds (its
   shares could be called away from under that strategy), and the others
   keep out of a covered-call name.
2. **Strike**: at least `--otm`% (10) above today's price and `--over-cost`%
   (0) above your cost, and never below cost. Expiry `--dte` (14-28) days
   out, sold at the bid/ask midpoint.
3. **Take profit**: buy back at `--take-profit`% (50) profit; sell the next
   call on the following run.
4. **Called away**: the cycle is recorded; it sells new calls only once 100
   shares are held again.
5. `cc stop SYMBOL`: no new calls; an open one is still managed.

Tell the user what the numbers mean before starting: a call far above the
price on a calm stock pays little (the preview shows the actual bid/ask), and
a call that ends in the money sells the shares at the strike, capping the gain.

## Step 5g: strategy 9, rebalance to target weights

A long-term allocation kept on target: `rebal start SYM:W ...` stores the
plan; `rebal run` once a day (10:00 AM ET is fine) acts only on its dates.
The user picks the names and weights; do not pick for them.

1. **Start**: the first run puts `--dollars` (10000) in at the target
   weights, at market, fractional where Alpaca allows (whole shares
   otherwise). It needs that much cash, or it waits and says so.
2. **Checks**: day `--day` (1) of each month, or of Jan/Apr/Jul/Oct with
   `--every quarterly`. If every name is within `--band` (5) percentage
   points of its target, nothing is traded.
3. **Rebalance**: otherwise it sells what is overweight first, then buys
   what is underweight with that cash, back to the targets. Trades under
   `--min-trade` ($25) are skipped.
4. It keeps its own share count (a sleeve), so it never sells shares
   another strategy bought. It refuses a name another strategy trades or
   holds; the others keep out of its names. New weights for the same names
   are fine; new names need `rebal stop` first.
5. No stops: it is an allocation, not a trade. `rebal stop` ends trading;
   the holdings stay.

## Step 5h: strategy 10, gap-and-go (day trade)

The opening-range breakout's engine with a gap filter: `gap start SYMBOL ...`
stores the plan in its own file; `gap run` every 5 minutes in market hours
does the work. Long only. The user picks the names; do not pick for them.

1. **Gap**: today's open (the first 5-minute IEX bar) is `--min-gap`% (3) or
   more above yesterday's official close (Yahoo, not dividend-adjusted), and
   at most `--max-gap`% if set. Huge gaps are often news; a cap skips them.
2. **Entry**: the first 5-minute bar that closes above the high of the first
   `--range` minutes (5), fresh (finished in the last 10 minutes), price
   still above that high and not chasing. No new entries from
   `--last-entry` (10:30 ET).
3. **The order**: one bracket at Alpaca, stop just under the day's open
   (`--stop open`; or `mid` / `low` of the range), take-profit at
   `--target-r` (2) times the risk. Size: the smaller of `--dollars` (2000)
   and `--max-loss` (100) / risk per share, whole shares.
4. **Limits and exit**: as the breakout: `--max-trades` (3) a day, one per
   name, never a name another strategy holds; everything sold by
   `--flat-by` (15:55). Its trades are kept apart from the breakout's.

## Step 5i: strategy 11, momentum rotation

`rotate start SYMBOL ...` stores the plan; `rotate run` once a day trades only
on its dates (day `--day` of each month by default, or `--every weekly`). The
user picks the names (often sector or asset-class ETFs); do not pick for them.

1. **Rank**: every name by its return over the last `--lookback` sessions
   (126, about 6 months), from Yahoo's dividend-adjusted daily closes;
   `--skip 21` leaves out the latest month, as many momentum studies do.
2. **Pick**: the top `--top` (2) whose return is above `--min-mom`% (0). A
   slot with no name above that sits in cash: the "absolute momentum" filter
   that keeps it out of a falling market. A name another strategy trades is
   skipped and the next one takes its place.
3. **Trade**: the first run splits `--dollars` (10,000) evenly. On each date,
   names that dropped out are sold in full and the new leaders bought with
   the sleeve's value / `--top` each. Names that stay are not touched. Market
   orders, fractional where Alpaca allows. No stop orders: the exit is the
   next rotation, so a holding can fall a lot within a month.
4. `rotate stop` ends the trading; what it holds stays in the account.

## Step 5j: strategy 12, the First Candle Rule (day trade, long and short)

From a short video ("5 min FVG break"). `fcr start SYMBOL ...` stores the
plan; `fcr run` every minute in market hours does the work. The user picks
the names; do not pick for them. The video's "$100,000 in a month" is the
creator's claim, not something this bot or its tests show.

1. **Levels**: the high and low of the first 5-minute candle, 9:30-9:35 ET
   (built from 1-minute IEX bars). The only levels for the day.
2. **Gap**: on completed 1-minute bars, a break through a level that leaves a
   fair value gap: bar 1 starts at the level, bar 2 closes through it, and
   bar 3 leaves a gap between its wick and bar 1's (up: bar 3's low above
   bar 1's high). A wick or a close through the level alone is not enough.
   A later close back through the far side of the gap cancels it.
3. **Entry**: price retests the gap, then an engulfing candle in the break's
   direction (within 15 bars). Enter at market if that candle finished in the
   last 3 minutes and price has not run half a risk unit past its close;
   otherwise the name is done for the day. No new entries from
   `--last-entry` (11:30 ET).
4. **The order**: one bracket at Alpaca. Long: buy, stop a cent under the
   retest's low. Short (`--sides both`, the default): sell short, stop a cent
   over the retest's high; only names Alpaca marks easy to borrow. Target
   `--target-r` (3) times the risk. Size: the smaller of `--dollars` (2000)
   and `--max-loss` (100) / risk per share, whole shares.
5. **Limits and exit**: `--max-trades` (3) a day, one per name, never a name
   another strategy holds; everything closed by `--flat-by` (15:55).

The free IEX feed carries a few percent of the volume, so its 1-minute bars
can show gaps the full tape does not, and the other way round.

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
45 15 * * 1-5   cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py swing run >> trader/bot/cron.log 2>&1
*/5  * * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py orb run >> trader/bot/cron.log 2>&1
45 15 * * 1-5   cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py bounce run >> trader/bot/cron.log 2>&1
0 10 * * 1-5    cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py dca run >> trader/bot/cron.log 2>&1
*/15 * * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py cc run >> trader/bot/cron.log 2>&1
5 10 * * 1-5    cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py rebal run >> trader/bot/cron.log 2>&1
10 10 * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py rotate run >> trader/bot/cron.log 2>&1
* 9-15 * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py fcr run >> trader/bot/cron.log 2>&1
*/5  * * * 1-5  cd ~/trading && python3 ~/.claude/skills/trader-bot/bot.py gap run >> trader/bot/cron.log 2>&1
```

On Windows, Task Scheduler does the same: one task per line, Monday to
Friday, running `pythonw.exe` on a small launcher in the trading folder that
calls `bot.py` and appends to `trader/bot/cron.log` (no window opens).

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
