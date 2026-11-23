---
name: trader-agents
description: >-
  AI Trading Desk. A team of agents that watches five stocks every day for a
  roughly 3-month swing trade, and a paper-trading journal to test the process
  before any real money. Agent 1 (fundamentals) reads earnings, margins, EPS
  and filings; agent 2 (news and sentiment) reads headlines, 8-Ks, the bull and
  bear case, social sentiment and upcoming catalysts; agent 3 (technicals)
  checks one rule: above a rising 200-day average with the 3-month rate of
  change reset near zero after a run. Then a desk summary per name, and paper
  trades only when the user says so. Use when the user says "run my agents",
  "run the desk", "daily agent report", "check my five names", "AI trading
  agents", "paper trade my process", "200-day moving average", "rate of change
  reset", "swing trade setup", or "how's my paper account".
argument-hint: "[run | setup | paper | backtest] [TICKERS]"
---

# trader-agents: the AI Trading Desk

The idea, from a former fund manager on the Titans of Tomorrow podcast: pick
five names, set up agents that each do one job every day, paper trade for a
while, and develop a process. You decide; the agents do the legwork.

`${CLAUDE_SKILL_DIR}` is this skill's folder. Everything is written under
`trader/` in the current project. Agent 1 and 2 reuse the Day Trader pack's
tools (`trader-analyst`, `trader-news`); if they are not installed, the agents
fall back to web search.

```bash
python3 "${CLAUDE_SKILL_DIR}/trend.py"                         # agent 3 on the names in trader/agents.json
python3 "${CLAUDE_SKILL_DIR}/trend.py" AAPL MSFT --history     # + every past time the rule fired (10 years)
python3 "${CLAUDE_SKILL_DIR}/trend.py" --write                 # -> trader/agents/technical.json
python3 "${CLAUDE_SKILL_DIR}/paper.py" show                    # paper account, marked to the latest price
python3 "${CLAUDE_SKILL_DIR}/paper.py" buy MSFT --qty 5 --reason "..."
python3 "${CLAUDE_SKILL_DIR}/paper.py" sell MSFT
python3 "${CLAUDE_SKILL_DIR}/paper.py" journal
```

## Setup (once)

1. Ask for five names (fewer or more is fine). Never pick them for the user;
   if asked for ideas, offer to run `/trader-scanner` or `trend.py` on a list
   they name.
2. Copy `${CLAUDE_SKILL_DIR}/agents.example.json` to `trader/agents.json` and
   put the names in `names`. Rule settings live under `technical`.
3. Agent 1 and agent 2's SEC filings need the user's own "Name email" as the
   SEC User-Agent: `"sec_user_agent"` in `trader/config.json` or the
   `SEC_USER_AGENT` environment variable. Ask the user; never invent one and
   never use an address they did not give for this.

## Run (every day, after the close or before the open)

1. **Agent 3, technicals**, first, because it is quick and decides the focus:
   `trend.py --write`. Verdicts: SETUP, WATCH, NO.
2. **Agents 1 and 2 in parallel.** In Claude Code, launch one subagent per
   role at the same time (the definitions in `agents/` install them as
   `fundamental-analyst` and `news-sentiment-analyst`; otherwise use a
   general-purpose subagent with the brief from that file). Give each the
   names and today's date. Each writes `trader/agents/<role>.md` and returns
   a short per-name summary. Without subagents, do the two jobs one after the
   other yourself.
   - Agent 1: `python3 <trader-analyst>/facts.py TICKER` for the quarters,
     margins and EPS; the latest earnings date and the next one; analyst
     estimate changes from web search, with the source.
   - Agent 2: `python3 <trader-news>/news.py --tickers ... --hours 72` for
     headlines and 8-Ks; web search for the bull and bear case, sentiment on
     X / Reddit / StockTwits (say it is a read of public posts, not a
     measurement), and catalysts in the next 3 months (earnings, product
     events, investor days, FDA dates, lock-ups).
3. **The desk summary**, one block per name:

```
MSFT  SETUP  $497.76
  technical    above a rising 200-day average (+12.5%); 63-day ROC reset to +1.4% after peaking at +41.4%
  fundamental  revenue +x% y/y, op margin x% (was y%), EPS ..., next earnings Jan 27
  news/mood    3 headlines (2 analyst, 1 product); mood mixed; bull: ..., bear: ...
  catalysts    Jan 27 earnings (inside the 3-month horizon)
  history      rule fired 13 times since 2016: 10 up after 63 sessions, median +12.3%
  open question  what the agents disagree on, in one line
```

   Then the paper account (`paper.py show`) and how each open paper position
   stands against today's verdicts (for example a holding that is now NO:
   below its 200-day average).
4. Ask the user what they want to do on paper. Record only what they say:
   `paper.py buy|sell ... --reason "<their reason, in their words>"`. The
   journal keeps each trade with the agents' verdicts at that moment, so the
   process can be judged later.

To run it every day, offer a scheduled task (or `/loop 1d /trader-agents run`)
that runs steps 1 to 3 and reports. A scheduled run never records a paper
trade: nobody is there to decide.

## The technical rule

| check | default | `technical` key |
| --- | --- | --- |
| close above the 200-day simple average (adjusted closes) | 200 | `ma_days` |
| the average pointed up: higher than 20 sessions ago | 20 | `slope_days` |
| trade horizon, the rate-of-change period: ~3 months | 63 | `roc_days` |
| ROC ran: at least +15% at some point in the last 63 sessions | 15 | `roc_peak_min`, `peak_lookback` |
| ROC reset: now within ±5% of zero | 5 | `roc_reset_max` |

`--history` re-runs the rule on 10 years of daily closes, one position at a
time, and reports what the next 63 sessions did. It is in-sample, with no
costs or slippage, and the past is not the future. Say that every time.

## Rules

- Paper by default and always here: `paper.py` never talks to a broker. Real
  orders go through the Day Trader pack's `/trader-risk` and `/trader-broker`
  only, with the user's explicit yes.
- Say what each agent read and when; say "matched the rule", never "a buy".
- Report what the agents found, including where they disagree. Never pick the
  stock or the trade for the user.
- Not financial advice. The user decides. Most active traders lose money.
