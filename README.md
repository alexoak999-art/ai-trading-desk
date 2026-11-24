# AI Trading Desk

A team of AI agents that watches five stocks every day for ~3-month swing
trades, plus a paper-trading journal to test the process before risking money.
Built from an idea on the Titans of Tomorrow podcast.

| agent | job | how |
| --- | --- | --- |
| 1 fundamentals | earnings, margins, EPS, estimates, next report | `agents/fundamental-analyst.md` (uses trader-analyst) |
| 2 news & sentiment | headlines, 8-Ks, bull/bear case, social mood, catalysts | `agents/news-sentiment-analyst.md` (uses trader-news) |
| 3 technicals | above a rising 200-day average, 3-month ROC reset near zero after a run | `skills/trader-agents/trend.py` |

`/trader-agents` runs agent 3, launches agents 1 and 2 in parallel, writes a
desk summary per name, and records paper trades only when you say so
(`paper.py`). Works best with the Day Trader skill pack installed.

## Trading bot (`/trader-bot`)

An Alpaca **paper-trading** bot with thirteen strategies, three rebuilt from a
Claude course video: a trailing stop with ladder buys (the stop is a real order
at Alpaca), copying stock trades members of Congress disclose, the options
wheel, a trend swing that trades the technical agent's 200-day /
rate-of-change rule with a stop and a ~3-month exit, and an opening-range
breakout day trade (bracket order, stop at the range midpoint, 2R target, flat
by 3:55 PM), and an oversold bounce (RSI 14 under 30, then an up close; 5%
stop, out at the 20-day average), and dollar-cost averaging as the buy-and-hold
baseline, covered calls on shares you hold, rebalancing to target
weights, a gap-and-go day trade, and momentum rotation (hold the top names
by 6-month return, monthly), and the First Candle Rule (a 5-minute candle's
fair-value-gap break, long and short, 3R), and a protective collar (a put
under 100 shares paid for by a call over them). A bankroll cap runs a small
account inside any paper account, and `backtest` tests the day-trade rules and
candle setups on a year of 1-minute bars. Keys go in `trader/alpaca.env`, never in the chat; there is no live
trading address in the code. Step-by-step setup: `skills/trader-bot/SKILL.md`.

## Install (Claude Code)

```bash
cp -r skills/trader-agents skills/trader-bot ~/.claude/skills/
cp agents/*.md ~/.claude/agents/
```

Then: `/trader-agents setup`, and `/trader-agents run` each day.

```bash
python -m unittest discover -s tests
```

Standard-library Python 3.9+ (`python3` instead of `python` on macOS/Linux). Data: Yahoo Finance daily bars (free, unofficial,
can lag). Not financial advice; paper only - nothing here places an order.
