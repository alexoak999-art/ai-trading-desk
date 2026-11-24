"""Backtests of the bot's day-trade rules and of candle setups, on real 1-minute bars.

Data: Alpaca's historical 1-minute bars (the full SIP tape when the free plan allows it, else IEX), the
regular session only, one file per symbol cached under trader/backtest/cache. The exchange calendar
(holidays, 1:00 PM early closes) comes from Alpaca too.

Fills, on purpose a little worse than the paper engine:
  - entry at the open of the minute after the signal bar closes, plus --slip per share;
  - a stop fills at the stop, or at the bar's open if it opened through it, minus --slip;
  - a target (a limit) fills at the target, no slip;
  - a bar that touches both the stop and the target counts as the stop;
  - anything still open at the flat-by time is sold at that minute's open, minus --slip.
R = profit per share / risk per share at entry. Dollars use the bot's sizing: whole shares,
min(--dollars // price, --max-loss // risk). A trade that buys no whole share is counted as skipped.
"""
import datetime
import gzip
import json
import os

import alpaca as alp

DIR = "trader/backtest"
CACHE = os.path.join(DIR, "cache")
OPEN_M = 570  # 9:30 ET, minutes after midnight


# ---------------------------------------------------------------- time and data

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def et_offset(day):
    """Hours from UTC to New York on that date (US daylight time: 2nd Sunday of March to 1st Sunday of November)."""
    return -4 if _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1) else -5


def hm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def to_m(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def calendar(api, start, end):
    """[(date, open minute, close minute)] for the sessions from start to end."""
    rows = api._req("GET", alp.TRADING, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()}) or []
    return [(datetime.date.fromisoformat(r["date"]), to_m(r["open"]), to_m(r["close"])) for r in rows]


def fetch_minutes(api, sym, start, end, say=print):
    """1-minute bars [[utc iso, o, h, l, c, v], ...] from start up to end (exclusive), cached."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s-%s-%s.json.gz" % (sym, start, end))
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    for feed in ("sip", "iex"):
        out, token = [], None
        try:
            while True:
                d = api._req("GET", alp.DATA, "/v2/stocks/%s/bars" % sym, {
                    "timeframe": "1Min", "start": "%sT00:00:00Z" % start, "end": "%sT00:00:00Z" % end,
                    "feed": feed, "adjustment": "raw", "limit": 10000, "page_token": token}) or {}
                out += [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in d.get("bars") or []]
                token = d.get("next_page_token")
                if not token:
                    break
        except alp.AlpacaError as e:
            if feed == "sip":
                say("  %s: the full tape isn't on this data plan (%s); using IEX bars." % (sym, str(e)[:80]))
                continue
            raise
        data = {"feed": feed, "bars": out}
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        return data
    return {"feed": "none", "bars": []}


def sessions(raw, cal):
    """{date: [[minute, o, h, l, c, v], ...]} regular-session bars only, in New York minutes."""
    hours = {d: (o, c) for d, o, c in cal}
    out = {}
    for t, o, h, l, c, v in raw:
        ts = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
        day = ts.date()
        m = ts.hour * 60 + ts.minute + 60 * et_offset(day)
        if m < 0:
            m += 1440
            day -= datetime.timedelta(days=1)
        if day in hours and hours[day][0] <= m < hours[day][1]:
            out.setdefault(day, []).append([m, float(o), float(h), float(l), float(c), float(v)])
    return out


def five_min(bars, start_m, end_m):
    """Completed 5-minute bars from start_m whose close is at or before end_m: [[start minute, o, h, l, c, v]]."""
    groups = {}
    for b in bars:
        if start_m <= b[0] < end_m:
            groups.setdefault(start_m + (b[0] - start_m) // 5 * 5, []).append(b)
    out = []
    for k in sorted(groups):
        g = groups[k]
        if k + 5 > end_m:
            continue
        out.append([k, g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], sum(x[5] for x in g)])
    return out


def first_at(bars, m):
    """Index of the first 1-minute bar at or after minute m, or None."""
    return next((i for i, b in enumerate(bars) if b[0] >= m), None)


# ---------------------------------------------------------------- one trade

def walk(bars, i0, entry, stop, target, flat_m, slip, sign=1):
    """Run a filled trade minute by minute from bar i0. Returns (exit price, how, minute)."""
    for b in bars[i0:]:
        m, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        if m >= flat_m:
            return o - slip * sign, "FLAT", m
        if sign > 0:
            if l <= stop:
                return min(stop, o) - slip, "STOP", m
            if h >= target:
                return max(target, o), "TARGET", m
        else:
            if h >= stop:
                return max(stop, o) + slip, "STOP", m
            if l <= target:
                return min(target, o), "TARGET", m
    last = bars[-1]
    return last[4] - slip * sign, "FLAT", last[0]


def trade(sym, day, bars, i0, stop, target_r, flat_m, cfg, setup, sign=1, chase_ref=None, chase_risk=None):
    """Enter at bar i0's open (+ slip), check the bot's entry rules, walk it to the exit. A dict, or None."""
    if i0 is None or bars[i0][0] >= flat_m:
        return None
    entry = bars[i0][1] + cfg["slip"] * sign
    risk = (entry - stop) * sign
    if risk <= max(0.02, entry * 0.001):
        return None  # the stop is inside the spread: the bot skips these
    if chase_ref is not None and (entry - chase_ref) * sign > 0.5 * chase_risk:
        return None  # ran too far past the signal: the bot doesn't chase
    target = entry + target_r * risk * sign
    px, how, m = walk(bars, i0, entry, stop, target, flat_m, cfg["slip"], sign)
    r = (px - entry) * sign / risk
    qty = int(min(cfg["dollars"] // entry, cfg["max_loss"] // risk))
    return {"symbol": sym, "day": day.isoformat(), "setup": setup, "side": "long" if sign > 0 else "short",
            "in": hm(bars[i0][0]), "out": hm(m), "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "exit": round(px, 2), "how": how, "r": round(r, 3), "qty": qty,
            "pnl": round(qty * (px - entry) * sign, 2), "hold": m - bars[i0][0]}


# ---------------------------------------------------------------- the strategies

def orb_trades(sym, day, bars, close_m, cfg, prev_close=None, gap=False):
    """The bot's opening-range breakout (gap=True: gap-and-go), one day, one symbol."""
    p = cfg["gap"] if gap else cfg["orb"]
    end = OPEN_M + p["range"]
    inside = [b for b in bars if OPEN_M <= b[0] < end]
    if not inside:
        return []
    hi, lo, day_open = max(b[2] for b in inside), min(b[3] for b in inside), inside[0][1]
    if gap:
        if not prev_close:
            return []
        g = (day_open / prev_close - 1) * 100
        if g < p["min_gap"] or (p.get("max_gap") and g > p["max_gap"]):
            return []
    last_entry = min(to_m(p["last_entry"]), close_m - 5)
    flat_m = min(to_m(cfg["flat_by"]), close_m - 5)
    brk = next((b for b in five_min(bars, end, last_entry) if b[4] > hi), None)
    if not brk:
        return []
    stop = day_open - 0.01 if p["stop"] == "open" else (hi + lo) / 2 if p["stop"] == "mid" else lo
    i0 = first_at(bars, brk[0] + 5)
    if i0 is None or bars[i0][1] <= hi:
        return []  # back at or under the range high by the time the bot looks
    t = trade(sym, day, bars, i0, stop, p["target_r"], flat_m, cfg, "gap" if gap else "orb",
              chase_ref=hi, chase_risk=hi - stop)
    return [t] if t else []


def fcr_trades(sym, day, bars, close_m, cfg, scan):
    """The bot's First Candle Rule: a 1-minute fair value gap through the 9:30-9:35 candle, retest, engulfing."""
    p = cfg["fcr"]
    first = [b for b in bars if OPEN_M <= b[0] < OPEN_M + 5]
    if not first:
        return []
    last_entry = min(to_m(p["last_entry"]), close_m - 5)
    flat_m = min(to_m(cfg["flat_by"]), close_m - 5)
    after = [b for b in bars if OPEN_M + 5 <= b[0] and b[0] + 1 <= last_entry]
    dicts = [{"t": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in after]
    hi, lo = max(b[2] for b in first), min(b[3] for b in first)
    best = None
    for side, sign, level in (("long", 1, hi), ("short", -1, lo)):
        if side == "short" and p["sides"] != "both":
            continue
        state, info = scan(dicts, level, sign)
        if state == "trigger" and (best is None or info["engulf_at"] < best[1]["engulf_at"]):
            best = (sign, info)
    if not best:
        return []
    sign, info = best
    stop = info["extreme"] - 0.01 * sign
    t = trade(sym, day, bars, first_at(bars, info["engulf_at"] + 1), stop, p["target_r"], flat_m, cfg, "fcr",
              sign=sign, chase_ref=info["close"], chase_risk=(info["close"] - stop) * sign)
    return [t] if t else []


def vwap_series(bars):
    out, pv, vv = {}, 0.0, 0.0
    for b in bars:
        pv += (b[2] + b[3] + b[4]) / 3 * b[5]
        vv += b[5]
        out[b[0]] = pv / vv if vv else b[4]
    return out


def candle_trades(sym, day, bars, close_m, cfg):
    """Long candle setups on 5-minute bars from 9:45 to the last entry; the first signal of each kind a day."""
    p = cfg["candles"]
    last_entry = min(to_m(p["last_entry"]), close_m - 5)
    flat_m = min(to_m(cfg["flat_by"]), close_m - 5)
    fives = five_min(bars, OPEN_M, last_entry)
    vw = vwap_series(bars)
    out, done = [], set()
    for i in range(3, len(fives)):
        b, pb = fives[i], fives[i - 1]
        if b[0] < OPEN_M + 15:
            continue
        m, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        sig = []
        falling = c < fives[i - 3][4] or pb[4] < fives[i - 3][4]
        body, rng = abs(c - o), h - l
        # bullish engulfing after a pullback
        if pb[4] < pb[1] and c > o and c >= pb[1] and o <= pb[4] + 0.01 and falling:
            sig.append(("engulfing", min(l, pb[3]) - 0.01))
        # hammer: long lower wick, closes near the high, after a pullback
        if rng > 0 and (min(o, c) - l) >= 2 * max(body, rng * 0.05) and (h - max(o, c)) <= 0.25 * rng \
                and falling:
            sig.append(("hammer", l - 0.01))
        # inside bar, then a close above the mother bar's high
        mb, ib = fives[i - 2], pb
        if ib[2] < mb[2] and ib[3] > mb[3] and c > mb[2]:
            sig.append(("inside_break", mb[3] - 0.01))
        # VWAP reclaim: three closes under VWAP, then one above it
        end_m = m + 4
        v_now = vw.get(end_m)
        if v_now and c > v_now and all(fives[j][4] < vw.get(fives[j][0] + 4, 1e18) for j in (i - 3, i - 2, i - 1)):
            sig.append(("vwap_reclaim", min(x[3] for x in fives[i - 2:i + 1]) - 0.01))
        for name, stop in sig:
            if name in done:
                continue
            done.add(name)
            t = trade(sym, day, bars, first_at(bars, m + 5), stop, p["target_r"], flat_m, cfg, name,
                      chase_ref=c, chase_risk=c - stop)
            if t:
                out.append(t)
    return out


# ---------------------------------------------------------------- statistics

def stats(trades):
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    half = len(rs) // 2
    sized = [t for t in trades if t["qty"] >= 1]
    hows = {}
    for t in trades:
        hows[t["how"]] = hows.get(t["how"], 0) + 1
    days = len({t["day"] for t in trades})
    return {"n": len(rs), "win_pct": round(100 * len(wins) / len(rs), 1), "avg_r": round(sum(rs) / len(rs), 3),
            "total_r": round(sum(rs), 2),
            "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "max_dd_r": round(dd, 2), "worst_streak": worst,
            "avg_r_first_half": round(sum(rs[:half]) / half, 3) if half else None,
            "avg_r_second_half": round(sum(rs[half:]) / (len(rs) - half), 3) if len(rs) - half else None,
            "exits": hows, "avg_hold_min": round(sum(t["hold"] for t in trades) / len(trades)),
            "sized": len(sized), "skipped_no_share": len(trades) - len(sized),
            "pnl": round(sum(t["pnl"] for t in sized), 2), "days": days}


def run(api, a, scan, say=print):
    """The whole backtest: fetch, simulate, summarize. Returns the result dict (also saved under trader/backtest)."""
    today = datetime.date.today()
    cal = [c for c in calendar(api, today - datetime.timedelta(days=int(a.days * 1.5) + 10), today) if c[0] < today]
    cal = cal[-(a.days + 1):]
    start, end = cal[0][0], today
    want = a.strategies if "all" not in a.strategies else ["orb", "gap", "fcr", "candles"]
    cfg = {"slip": a.slip, "dollars": a.dollars, "max_loss": a.max_loss, "flat_by": a.flat_by,
           "orb": {"range": a.range, "stop": "mid", "target_r": a.orb_r, "last_entry": "11:30"},
           "gap": {"range": 5, "stop": "open", "target_r": a.orb_r, "min_gap": a.min_gap, "max_gap": None,
                   "last_entry": "10:30"},
           "fcr": {"sides": a.sides, "target_r": a.fcr_r, "last_entry": "11:30"},
           "candles": {"target_r": a.candle_r, "last_entry": a.candle_last}}
    say("Backtest %s on %s: %d sessions, %s to %s. Slip $%.2f a share; sizing $%g a trade, $%g at risk." % (
        ", ".join(want), ", ".join(a.symbols), len(cal) - 1, cal[1][0], cal[-1][0], a.slip, a.dollars, a.max_loss))
    trades, feeds = [], {}
    for sym in a.symbols:
        data = fetch_minutes(api, sym, start, end, say)
        feeds[sym] = data["feed"]
        days = sessions(data["bars"], cal)
        prev = None
        for d, _, close_m in cal:
            bars = days.get(d)
            if d == cal[0][0]:  # the extra first session only gives a previous close
                prev = bars[-1][4] if bars else None
                continue
            if not bars:
                prev = None
                continue
            if "orb" in want:
                trades += orb_trades(sym, d, bars, close_m, cfg)
            if "gap" in want:
                trades += orb_trades(sym, d, bars, close_m, cfg, prev_close=prev, gap=True)
            if "fcr" in want:
                trades += fcr_trades(sym, d, bars, close_m, cfg, scan)
            if "candles" in want:
                trades += candle_trades(sym, d, bars, close_m, cfg)
            prev = bars[-1][4]
        say("  %s: %d sessions of %s bars." % (sym, len(days), data["feed"].upper()))
    trades.sort(key=lambda t: (t["day"], t["in"]))
    setups = sorted({t["setup"] for t in trades})
    by = {s: stats([t for t in trades if t["setup"] == s]) for s in setups}
    by_sym = {s: {sym: stats([t for t in trades if t["setup"] == s and t["symbol"] == sym]) for sym in a.symbols}
              for s in setups}
    res = {"ran": datetime.datetime.now().isoformat(timespec="seconds"), "from": cal[1][0].isoformat(),
           "to": cal[-1][0].isoformat(), "sessions": len(cal) - 1, "symbols": a.symbols, "feeds": feeds,
           "cfg": cfg, "by_setup": by, "by_symbol": by_sym, "trades": trades}
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, "result-%s.json" % today.isoformat())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    say("")
    say("%-13s %5s %6s %7s %8s %5s %8s %6s %10s %10s %8s" % (
        "setup", "n", "win%", "avg R", "total R", "PF", "max DD R", "streak", "1st half R", "2nd half R", "$ P/L"))
    for s in setups:
        x = by[s]
        say("%-13s %5d %6.1f %+7.3f %+8.2f %5s %8.2f %6d %+10.3f %+10.3f %8.2f" % (
            s, x["n"], x["win_pct"], x["avg_r"], x["total_r"], x["pf"] if x["pf"] is not None else "-",
            x["max_dd_r"], x["worst_streak"], x["avg_r_first_half"] or 0, x["avg_r_second_half"] or 0, x["pnl"]))
    say("")
    say("Saved every trade to %s. Past results on past bars; not a prediction." % path)
    return res
