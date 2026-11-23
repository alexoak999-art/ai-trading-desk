#!/usr/bin/env python3
"""Technical agent: trend + rate-of-change reset, on daily closes.

The rule (from the reel): only trade a name when
  1. the close is above its 200-day simple moving average,
  2. that 200-day average is pointed up (higher than N sessions ago), and
  3. the rate of change (ROC) over the trade horizon -- 3 months, 63 sessions --
     has RESET close to zero after being clearly higher: the stock ran, then
     went sideways, while still above a rising 200-day average.

Verdicts per name:
  SETUP  all three hold today
  WATCH  in an uptrend (1 and 2), but ROC has not reset (or never ran)
  NO     below the 200-day average, or the average is falling

--history re-runs the same rule on every past session in the data and reports
what happened over the following horizon (in-sample, no costs, no slippage:
a reality check, not a promise).

Standard library only. Data: Yahoo Finance v8 chart, daily bars (free,
unofficial, can lag). --offline DIR reads DIR/SYMBOL.json instead.

  python trend.py AAPL MSFT NVDA GOOGL AMZN
  python trend.py --config trader/agents.json --history --write
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; trader-agents/1.0)"
HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")

DEFAULTS = {
    "ma_days": 200,          # the long moving average
    "slope_days": 20,        # "pointed up": MA today > MA this many sessions ago
    "roc_days": 63,          # trade horizon, ~3 months
    "roc_peak_min": 15.0,    # ROC (%) must have been at least this ...
    "peak_lookback": 63,     # ... at some point in the last this many sessions
    "roc_reset_max": 5.0,    # and now |ROC| (%) is at most this: "reset near zero"
}


# ---------------------------------------------------------------- data

def http_json(path, tries=2):
    last = None
    for host in HOSTS:
        for attempt in range(tries):
            req = urllib.request.Request(host + path, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 and attempt + 1 < tries:
                    time.sleep(5)
                    continue
                if e.code == 404:
                    raise
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                break
    raise RuntimeError(str(last))


def load_chart(symbol, offline=None, rng="2y"):
    if offline:
        with open(os.path.join(offline, symbol + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = http_json("/v8/finance/chart/%s?range=%s&interval=1d"
                         % (symbol.replace(".", "-"), rng))
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("no chart data")
    r = res[0]
    gmtoff = (r.get("meta") or {}).get("gmtoffset") or 0
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    closes = adj if adj and len(adj) == len(ts) else q.get("close") or []
    rows = [(datetime.datetime.fromtimestamp(t + gmtoff, datetime.timezone.utc).date().isoformat(), c)
            for t, c in zip(ts, closes) if c is not None]
    if not rows:
        raise RuntimeError("no closes")
    return rows


# ---------------------------------------------------------------- math

def sma_series(closes, n):
    out, s = [None] * len(closes), 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def roc_series(closes, n):
    return [None if i < n else (closes[i] / closes[i - n] - 1) * 100
            for i in range(len(closes))]


def evaluate(closes, p=DEFAULTS):
    """Per-session state: list of dicts (None before there is enough data)."""
    ma = sma_series(closes, p["ma_days"])
    roc = roc_series(closes, p["roc_days"])
    out = []
    for i, c in enumerate(closes):
        j = i - p["slope_days"]
        if ma[i] is None or j < 0 or ma[j] is None or roc[i] is None:
            out.append(None)
            continue
        window = [r for r in roc[max(0, i - p["peak_lookback"]):i] if r is not None]
        peak = max(window) if window else None
        above = c > ma[i]
        rising = ma[i] > ma[j]
        ran = peak is not None and peak >= p["roc_peak_min"]
        reset = abs(roc[i]) <= p["roc_reset_max"]
        if not (above and rising):
            verdict = "NO"
        elif ran and reset:
            verdict = "SETUP"
        else:
            verdict = "WATCH"
        out.append({
            "close": round(c, 2), "ma": round(ma[i], 2),
            "pct_over_ma": round((c / ma[i] - 1) * 100, 2),
            "ma_slope_pct": round((ma[i] / ma[j] - 1) * 100, 2),
            "roc": round(roc[i], 2), "roc_peak": None if peak is None else round(peak, 2),
            "above_ma": above, "ma_rising": rising, "ran": ran, "reset": reset,
            "verdict": verdict,
        })
    return out


def why(s, p=DEFAULTS):
    if s["verdict"] == "NO":
        parts = []
        if not s["above_ma"]:
            parts.append("close %.2f is %.1f%% below the %d-day average %.2f"
                         % (s["close"], -s["pct_over_ma"], p["ma_days"], s["ma"]))
        if not s["ma_rising"]:
            parts.append("the %d-day average is falling (%.1f%% over %d sessions)"
                         % (p["ma_days"], s["ma_slope_pct"], p["slope_days"]))
        return "; ".join(parts)
    trend = "above a rising %d-day average (+%.1f%%)" % (p["ma_days"], s["pct_over_ma"])
    if s["verdict"] == "SETUP":
        return "%s; %d-day ROC reset to %+.1f%% after peaking at %+.1f%%" % (
            trend, p["roc_days"], s["roc"], s["roc_peak"])
    if not s["ran"]:
        return "%s; ROC never got to +%.0f%% in the last %d sessions (peak %+.1f%%)" % (
            trend, p["roc_peak_min"], p["peak_lookback"], s["roc_peak"] or 0)
    return "%s; ROC %+.1f%% has not reset to within %.0f%% of zero (peak %+.1f%%)" % (
        trend, s["roc"], p["roc_reset_max"], s["roc_peak"])


def history(dates, closes, states, p=DEFAULTS):
    """Every session the SETUP verdict newly fired, and what the next
    roc_days sessions did. Signals closer together than the horizon are skipped
    (one position at a time)."""
    h, trades, i = p["roc_days"], [], 0
    while i < len(states):
        s = states[i]
        fresh = s and s["verdict"] == "SETUP" and not (
            i > 0 and states[i - 1] and states[i - 1]["verdict"] == "SETUP")
        if fresh and i + h < len(closes):
            entry = closes[i]
            path = closes[i + 1:i + h + 1]
            trades.append({
                "date": dates[i], "entry": round(entry, 2),
                "ret_pct": round((path[-1] / entry - 1) * 100, 2),
                "worst_pct": round((min(path) / entry - 1) * 100, 2),
            })
            i += h
            continue
        i += 1
    return trades


def summarize(trades):
    if not trades:
        return None
    rets = sorted(t["ret_pct"] for t in trades)
    n = len(rets)
    med = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
    return {"signals": n, "up": sum(r > 0 for r in rets),
            "median_ret_pct": round(med, 2),
            "worst_ret_pct": rets[0], "best_ret_pct": rets[-1]}


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--config", default="trader/agents.json",
                    help="names + rule overrides (default trader/agents.json)")
    ap.add_argument("--history", action="store_true", help="re-run the rule on past sessions")
    ap.add_argument("--range", default=None, help="Yahoo range (default 2y, 10y with --history)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--write", action="store_true", help="write trader/agents/technical.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    p, cfg = dict(DEFAULTS), {}
    if os.path.exists(a.config):
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        p.update({k: v for k, v in (cfg.get("technical") or {}).items() if k in DEFAULTS})
    symbols = [s.upper() for s in (a.symbols or cfg.get("names") or [])]
    if not symbols:
        print("trend.py: no symbols. Give them on the command line or as \"names\" in %s." % a.config)
        return 1
    rng = a.range or ("10y" if a.history else "2y")

    report = {"as_of": None, "rule": p, "names": []}
    for sym in symbols:
        item = {"symbol": sym}
        try:
            rows = load_chart(sym, a.offline, rng)
            dates, closes = [d for d, _ in rows], [c for _, c in rows]
            states = evaluate(closes, p)
            if states[-1] is None:
                raise RuntimeError("only %d sessions, need %d"
                                   % (len(closes), p["ma_days"] + p["slope_days"]))
            item.update(states[-1], date=dates[-1], why=why(states[-1], p))
            report["as_of"] = max(report["as_of"] or "", dates[-1])
            if a.history:
                trades = history(dates, closes, states, p)
                item["history"] = {"from": dates[0], "summary": summarize(trades),
                                   "trades": trades}
        except Exception as e:  # one bad name never stops the rest
            item.update(verdict="ERROR", why=str(e))
        report["names"].append(item)
        if not a.offline:
            time.sleep(0.3)

    if a.write:
        os.makedirs("trader/agents", exist_ok=True)
        with open("trader/agents/technical.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)

    if a.json:
        print(json.dumps(report, indent=1))
    else:
        print("Technical agent, daily closes through %s (Yahoo, adjusted). Rule: above a rising %d-day "
              "average, %d-day ROC peaked >= +%.0f%% then reset to within %.0f%% of zero."
              % (report["as_of"], p["ma_days"], p["roc_days"], p["roc_peak_min"], p["roc_reset_max"]))
        for it in report["names"]:
            if it["verdict"] == "ERROR":
                print("  %-6s ERROR  %s" % (it["symbol"], it["why"]))
                continue
            print("  %-6s %-5s  $%-9.2f %s" % (it["symbol"], it["verdict"], it["close"], it["why"]))
            hs = (it.get("history") or {}).get("summary")
            if a.history:
                if hs:
                    print("         history since %s: fired %d times, %d of %d up after %d sessions, "
                          "median %+.1f%% (worst %+.1f%%, best %+.1f%%)"
                          % (it["history"]["from"], hs["signals"], hs["up"], hs["signals"],
                             p["roc_days"], hs["median_ret_pct"], hs["worst_ret_pct"], hs["best_ret_pct"]))
                else:
                    print("         history since %s: the rule never fired with a full horizon after it"
                          % it["history"]["from"])
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo("America/New_York"))
            if report["as_of"] == now.date().isoformat() and now.hour < 16:
                print("Note: today's bar is still forming until 4:00 PM ET; its close is the latest price.")
        except Exception:
            pass
        if a.history:
            print("History is in-sample: no costs, no slippage, one position at a time. A reality check, not a promise.")
        if a.write:
            print("Wrote trader/agents/technical.json.")
    return 0 if any(it["verdict"] != "ERROR" for it in report["names"]) else 1


if __name__ == "__main__":
    sys.exit(main())
