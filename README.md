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

## Install (Claude Code)

```bash
cp -r skills/trader-agents ~/.claude/skills/
cp agents/*.md ~/.claude/agents/
```

Then: `/trader-agents setup`, and `/trader-agents run` each day.

```bash
python3 -m unittest discover -s tests
```

Standard-library Python 3.9+. Data: Yahoo Finance daily bars (free, unofficial,
can lag). Not financial advice; paper only - nothing here places an order.
