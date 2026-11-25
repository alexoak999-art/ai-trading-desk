#!/usr/bin/env python3
"""Paper-trading bot on Alpaca: trailing stop with ladder buys, copying Congress
trades, the options wheel, a 200-day trend swing, an opening-range breakout
day trade, an oversold bounce, dollar-cost averaging, covered calls,
rebalancing to target weights, a gap-and-go day trade, momentum rotation, and
the First Candle Rule (a 5-minute fair-value-gap break, long and short), and a
protective collar (a put under 100 shares paid for by a call over them). PAPER ACCOUNT ONLY.

  python bot.py check                                   # keys work? paper? market open?
  python bot.py test-buy AAPL                           # the connection test: buy 1 share
  python bot.py trail start TSLA --qty 10 --stop 10 --trigger 10 --trail 5 --ladder 5:5,8:10
  python bot.py trail run                               # every 5 min in market hours
  python bot.py copy run --member mccaul --dollars 1000 # each morning / hourly
  python bot.py wheel start TSLA --put-otm 10 --call-over 10 --dte 14-28 --take-profit 50
  python bot.py wheel run                               # every 15 min in market hours
  python bot.py swing start AAPL MSFT NVDA --dollars 2000 --stop 10 --hold-days 91
  python bot.py swing run                               # once a day, ~3:45 PM ET
  python bot.py swing stop                              # no new buys; open ones still managed
  python bot.py orb start AAPL NVDA --range 15 --stop mid --target-r 2 --max-loss 100
  python bot.py orb run                                 # every 5 min in market hours
  python bot.py orb stop                                # no new trades; open ones still flat by close
  python bot.py bounce start AAPL MSFT --dollars 2000 --stop 5 --hold-days 14 [--above-200]
  python bot.py bounce run                              # once a day, ~3:45 PM ET
  python bot.py bounce stop                             # no new buys; open ones still managed
  python bot.py dca start SPY:60 QQQ:40 --dollars 100 --every weekly --day mon [--cap 5000]
  python bot.py dca run                                 # once a day; buys only on its dates
  python bot.py dca stop                                # no more buys; holdings stay
  python bot.py cc start AAPL --otm 10 --dte 14-28 --take-profit 50 [--buy-shares]
  python bot.py cc run                                  # every 15 min in market hours
  python bot.py cc stop AAPL                            # no new calls; the open one is still managed
  python bot.py rebal start VTI:60 BND:40 --dollars 10000 --every monthly --day 1 --band 5
  python bot.py rebal run                               # once a day; trades only on its dates
  python bot.py rebal stop                              # no more trades; holdings stay
  python bot.py gap start AMZN TSLA --min-gap 3 [--max-gap 10] --stop open --target-r 2
  python bot.py gap run                                 # every 5 min in market hours
  python bot.py gap stop                                # no new trades; open ones still flat by close
  python bot.py rotate start XLK XLF XLE XLV --top 2 --lookback 126 --dollars 10000 --every monthly --day 1
  python bot.py rotate run                              # once a day; trades only on its dates
  python bot.py rotate stop                             # no more trades; holdings stay
  python bot.py fcr start SPY QQQ --sides both --target-r 3 --max-loss 100 --last-entry 11:30
  python bot.py fcr run                                 # every minute in market hours
  python bot.py fcr stop                                # no new trades; open ones still closed by 15:55
  python bot.py collar start AAPL --put-otm 10 --call-otm 10 --dte 30-60 --max-debit 0.5 [--buy-shares]
  python bot.py collar run                              # every 15 min in market hours
  python bot.py collar stop AAPL                        # no new collars; the open one runs to expiration
  python bot.py notify setup [--phone]                  # a pop-up (and phone push) for every order
  python bot.py bankroll set 500                        # this folder's bots: at most $500 + profits, long stock only
  python bot.py backtest all --symbols AMD PYPL --days 250   # day-trade rules + candle setups on past bars
  python bot.py bankroll show | off
  python bot.py notify test | off
  python bot.py status                                 # account + every strategy
  add --dry-run to any command to print what it would send without sending it.

State and a log of every order live in trader/bot/. Each `run` does nothing
when the market is closed. Nothing here trades real money.
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alpaca as alp  # noqa: E402

DIR = "trader/bot"
LOG = os.path.join(DIR, "log.jsonl")


# ---------------------------------------------------------------- plumbing

def load(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def log(event, **kw):
    os.makedirs(DIR, exist_ok=True)
    kw.update(event=event, at=datetime.datetime.now().replace(microsecond=0).isoformat())
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(kw) + "\n")
    if not kw.get("dry_run") and event not in QUIET:
        alert(event, kw)


# ---------------------------------------------------------------- trade alerts (Windows pop-up, ntfy push)

NOTIFY = "trader/notify.json"
QUIET = ("trail_raise", "error")  # not trades: a stop moved up, a failed run
ALERT_KEYS = ("symbol", "ticker", "side", "kind", "qty", "dollars", "value", "price", "entry", "exit", "limit",
              "strike", "stop", "target", "how", "reason", "r", "pnl")
TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:TB_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:TB_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def alert_text(event, kw):
    """('Trade bot: ORB ENTRY', 'AMZN | qty 7 | entry 253.90 | stop 251.97 | target 258.06')."""
    strat, _, action = event.partition("_")
    title = "Trade bot: %s %s" % (strat.upper(), action.replace("_", " ").upper())
    parts = []
    for k in ALERT_KEYS:
        v = kw.get(k)
        if v is None or v == "":
            continue
        if k in ("symbol", "ticker"):
            parts.insert(0, str(v))
        elif isinstance(v, float):
            parts.append("%s %s" % (k, ("%.4f" % v).rstrip("0").rstrip(".") if k == "qty" else "%.2f" % v))
        else:
            parts.append("%s %s" % (k, v))
    return title, " | ".join(parts) or event


def send_toast(title, body):
    if sys.platform != "win32":
        return
    env = dict(os.environ, TB_TITLE=title, TB_BODY=body)
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", TOAST], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)


def send_ntfy(server, topic, title, body):
    import urllib.request
    req = urllib.request.Request("%s/%s" % (server.rstrip("/"), topic), data=body.encode("utf-8"), method="POST",
                                 headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "chart_with_upwards_trend"})
    urllib.request.urlopen(req, timeout=5).read()


def alert(event, kw):
    """A pop-up and/or a phone push for one trade, if trader/notify.json turns them on. Never stops the bot."""
    cfg = load(NOTIFY) if os.path.exists(NOTIFY) else None
    if not cfg:
        return
    title, body = alert_text(event, kw)
    try:
        if cfg.get("windows"):
            send_toast(title, body)
    except Exception:
        pass
    try:
        if cfg.get("ntfy_topic"):
            send_ntfy(cfg.get("ntfy_server") or "https://ntfy.sh", cfg["ntfy_topic"], title, body)
    except Exception:
        pass


def notify_setup(api, a):
    import secrets
    cfg = load(NOTIFY) or {}
    cfg["windows"] = not a.no_windows
    if a.phone:
        cfg.setdefault("ntfy_topic", "tradebot-" + secrets.token_hex(8))
        cfg.setdefault("ntfy_server", "https://ntfy.sh")
    else:
        cfg.pop("ntfy_topic", None)
    save(NOTIFY, cfg)
    say("Trade alerts on: %s." % ", ".join(
        (["Windows pop-ups"] if cfg["windows"] else []) + (["phone push via ntfy"] if cfg.get("ntfy_topic") else [])
    ) if cfg["windows"] or cfg.get("ntfy_topic") else "Trade alerts off.")
    if cfg.get("ntfy_topic"):
        say("On your phone: install the ntfy app, tap +, and subscribe to the topic %s (server %s). Anyone who knows "
            "the topic name can read the alerts, so keep it to yourself. It is kept in %s." % (
                cfg["ntfy_topic"], cfg["ntfy_server"], NOTIFY))
    return 0


def notify_test(api, a):
    cfg = load(NOTIFY)
    if not cfg:
        say("Trade alerts are off. Turn them on with `notify setup` (add --phone for a phone push).")
        return 1
    title, body = "Trade bot: TEST", "Alerts work. You'll get one like this for every order the bots place."
    if cfg.get("windows"):
        send_toast(title, body)
    if cfg.get("ntfy_topic"):
        try:
            send_ntfy(cfg.get("ntfy_server") or "https://ntfy.sh", cfg["ntfy_topic"], title, body)
        except Exception as e:
            say("The phone push failed: %s" % e)
            return 1
    say("Test alert sent%s." % (" (pop-up and phone)" if cfg.get("windows") and cfg.get("ntfy_topic") else ""))
    return 0


def notify_off(api, a):
    if os.path.exists(NOTIFY):
        os.remove(NOTIFY)
    say("Trade alerts off.")
    return 0


def say(msg):
    print(msg, flush=True)


class DryRun:
    """Reads go to Alpaca; every write is printed and not sent."""

    def __init__(self, inner):
        self._inner, self._n = inner, 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def submit_order(self, **o):
        self._n += 1
        say("  DRY RUN, not sent: %s" % json.dumps(o))
        return {"id": "dry-%d" % self._n, "status": "dry_run", "filled_avg_price": None}

    def replace_order(self, order_id, **c):
        say("  DRY RUN, not sent: replace %s with %s" % (order_id, json.dumps(c)))
        return {"id": order_id, "status": "dry_run"}

    def cancel_order(self, order_id):
        say("  DRY RUN, not sent: cancel %s" % order_id)


# ---------------------------------------------------------------- bankroll: a small account inside any account

BANKROLL = "trader/bankroll.json"


def bankroll_room(api, cfg):
    """(play money, in positions and pending buys, room left). Play money = the stake plus whatever
    the account has made or lost since the stake was set."""
    equity = float(api.account()["equity"])
    play = cfg["stake"] + equity - cfg["start_equity"]
    used = 0.0
    for p in api.positions():
        q = float(p["qty"])
        if q > 0:
            used += float(p.get("market_value") or q * api.last_price(p["symbol"]))
    lister = getattr(api, "orders", None)
    for o in (lister(status="open") if callable(lister) else []):
        if o.get("side") == "buy" and occ_underlying(o["symbol"]) == o["symbol"]:
            px = o.get("limit_price") or o.get("stop_price") or api.last_price(o["symbol"])
            used += float(o["notional"]) if o.get("notional") else float(o.get("qty") or 0) * float(px)
    return play, used, play - used


class Capped:
    """On when trader/bankroll.json exists in this folder. Every order goes through here first:
    stock buys only while the bot's positions fit inside the stake plus profits, no options, no short sales."""

    def __init__(self, inner, cfg):
        self._inner, self._cfg = inner, cfg

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def submit_order(self, **o):
        sym, side, stake = o.get("symbol", ""), o.get("side"), self._cfg["stake"]
        if occ_underlying(sym) != sym:
            raise alp.AlpacaError("bankroll: %s is an option; options are off in this $%g account." % (sym, stake))
        pos = self._inner.position(sym)
        held = float(pos["qty"]) if pos else 0.0
        if side == "sell":
            want = float(o["qty"]) if o.get("qty") else float(o["notional"]) / self._inner.last_price(sym)
            if want > held + 1e-6:
                raise alp.AlpacaError("bankroll: selling %g %s but only %g held; short selling is off in this "
                                      "$%g account." % (want, sym, held, stake))
        elif side == "buy" and held >= 0:
            px = float(o.get("limit_price") or self._inner.last_price(sym))
            cost = float(o["notional"]) if o.get("notional") else float(o["qty"]) * px
            play, used, room = bankroll_room(self._inner, self._cfg)
            if cost > room + 0.01:
                raise alp.AlpacaError(
                    "bankroll: buying %s for about $%.2f is more than the $%.2f left (play money $%.2f = stake "
                    "$%g %+.2f since %s; $%.2f already in positions and pending buys). Nothing sent." % (
                        sym, cost, max(room, 0), play, stake, play - stake, self._cfg["since"], used))
        return self._inner.submit_order(**o)


def backtest(api, a):
    import backtest as bt
    a.symbols = [x.upper() for x in a.symbols]
    if a.days < 5 or a.slip < 0 or a.dollars <= 0 or a.max_loss <= 0:
        say("--days must be 5 or more; --slip, --dollars and --max-loss can't be negative or zero.")
        return 1
    bt.run(api, a, fcr_scan, say)
    return 0


def bankroll_set(api, a):
    if a.stake <= 0:
        say("The stake must be more than $0.")
        return 1
    acct = api.account()
    cfg = {"stake": a.stake, "start_equity": float(acct["equity"]), "since": datetime.date.today().isoformat()}
    play, used, room = bankroll_room(api, cfg)
    save(BANKROLL, cfg)
    say("Bankroll on: the bots in this folder trade at most $%g plus what they make (account equity now $%.2f). "
        "Stock buys only; options and short sales are refused. $%.2f is already in positions here, so $%.2f is "
        "free to use." % (a.stake, cfg["start_equity"], used, max(room, 0)))
    if used > a.stake:
        say("Note: the positions already here are worth more than the stake, so no new buys until they are sold.")
    return 0


def bankroll_show(api, a):
    cfg = load(BANKROLL)
    if not cfg:
        say("No bankroll: the bots here may use the whole account. Set one with `bankroll set 500`.")
        return 0
    play, used, room = bankroll_room(api, cfg)
    say("bankroll: stake $%g since %s; play money $%.2f (%+.2f, %+.1f%%); in positions $%.2f; free $%.2f" % (
        cfg["stake"], cfg["since"], play, play - cfg["stake"], 100 * (play - cfg["stake"]) / cfg["stake"], used,
        max(room, 0)))
    return 0


def bankroll_off(api, a):
    if os.path.exists(BANKROLL):
        os.remove(BANKROLL)
    say("Bankroll off: the bots here may use the whole account again.")
    return 0


def wait_fill(api, order, seconds=15):
    oid = order.get("id")
    for _ in range(seconds):
        if order.get("status") in ("filled", "dry_run") or not oid:
            return order
        time.sleep(1)
        order = api.order(oid) or order
    return order


def market_open(api):
    c = api.clock() or {}
    if not c.get("is_open"):
        say("Market is closed (next open %s). Nothing to do; stop orders already at Alpaca still protect you."
            % c.get("next_open", "?"))
        return False
    return True


def money(x):
    return round(float(x) + 1e-9, 2)


# ---------------------------------------------------------------- strategy 1: trailing stop + ladder

def floor_for(st, hwm):
    """The stop price: never below entry - stop%, and once the stock has run
    trigger% above entry, trail% below the highest price seen. Only goes up."""
    f = st["entry"] * (1 - st["stop_pct"] / 100)
    if hwm >= st["entry"] * (1 + st["trigger_pct"] / 100) - 1e-9:
        f = max(f, hwm * (1 - st["trail_pct"] / 100))
    return money(max(f, st.get("floor") or 0))


def parse_ladder(text):
    out = []
    for part in filter(None, (text or "").split(",")):
        drop, qty = part.split(":")
        out.append({"drop_pct": abs(float(drop)), "qty": int(qty), "done": False})
    return sorted(out, key=lambda x: x["drop_pct"])


def validate_trail(stop, trigger, trail, ladder):
    errs = []
    if not 0 < stop < 100:
        errs.append("--stop must be between 0 and 100 (percent below your entry)")
    if trigger <= 0 or trail <= 0:
        errs.append("--trigger and --trail must be positive percents")
    for lv in ladder:
        if lv["drop_pct"] >= stop:
            errs.append("ladder buy at -%g%% can never happen: the -%g%% stop sells everything first. "
                        "Put ladder levels above the stop (e.g. --ladder 5:5,8:10 with --stop 10)."
                        % (lv["drop_pct"], stop))
    return errs


def trail_path(sym):
    return os.path.join(DIR, "trail-%s.json" % sym)


def trail_start(api, a):
    sym = a.symbol.upper()
    ladder = parse_ladder(a.ladder)
    errs = validate_trail(a.stop, a.trigger, a.trail, ladder)
    if errs:
        for e in errs:
            say("trail: " + e)
        return 1
    old = load(trail_path(sym))
    if old and old.get("status") == "open":
        say("trail: %s already has an open trailing-stop plan. `trail run` manages it." % sym)
        return 1
    if not market_open(api):
        return 1
    o = wait_fill(api, api.submit_order(symbol=sym, qty=a.qty, side="buy", type="market",
                                        time_in_force="day"))
    entry = float(o.get("filled_avg_price") or api.last_price(sym))
    st = {"symbol": sym, "qty": a.qty, "entry": money(entry), "stop_pct": a.stop,
          "trigger_pct": a.trigger, "trail_pct": a.trail, "ladder": ladder,
          "hwm": money(entry), "floor": None, "stop_order_id": None, "status": "open",
          "opened": datetime.date.today().isoformat()}
    st["floor"] = floor_for(st, entry)
    so = api.submit_order(symbol=sym, qty=a.qty, side="sell", type="stop",
                          stop_price=st["floor"], time_in_force="gtc")
    st["stop_order_id"] = so.get("id")
    if not a.dry_run:
        save(trail_path(sym), st)
    log("trail_start", symbol=sym, qty=a.qty, entry=st["entry"], floor=st["floor"], dry_run=a.dry_run)
    if a.dry_run:
        say("DRY RUN, nothing sent: would buy %d %s at about %.2f and place a stop-loss at %.2f (-%g%%)."
            % (a.qty, sym, entry, st["floor"], a.stop))
    else:
        say("BOUGHT %d %s at about %.2f (paper). Stop-loss order at Alpaca: %.2f (-%g%%)."
            % (a.qty, sym, entry, st["floor"], a.stop))
    say("Once it is up %g%% the stop trails %g%% below the highest price. Ladder: %s."
        % (a.trigger, a.trail, ", ".join("buy %d at -%g%%" % (l["qty"], l["drop_pct"]) for l in ladder) or "none"))
    return 0


def trail_run(api, a):
    plans = [load(os.path.join(DIR, f)) for f in sorted(os.listdir(DIR))
             if f.startswith("trail-")] if os.path.isdir(DIR) else []
    plans = [p for p in plans if p and p.get("status") == "open"]
    if not plans:
        say("No open trailing-stop plans.")
        return 0
    if not market_open(api):
        return 0
    for st in plans:
        sym = st["symbol"]
        pos = api.position(sym)
        held = int(float(pos["qty"])) if pos else 0
        if held <= 0:
            st["status"] = "closed"
            st["closed"] = datetime.date.today().isoformat()
            log("trail_closed", symbol=sym, floor=st["floor"])
            say("%s: position is gone (the stop filled or you sold). Plan closed." % sym)
            if not a.dry_run:
                save(trail_path(sym), st)
            continue
        price = api.last_price(sym)
        st["hwm"] = money(max(st["hwm"], price))
        due = [lv for lv in st["ladder"] if not lv["done"] and price <= st["entry"] * (1 - lv["drop_pct"] / 100)]
        if due and st.get("stop_order_id"):
            # Alpaca refuses a buy while our own sell stop on the name is open ("potential wash trade"):
            # take the stop down first; a new one for every share goes back on below.
            api.cancel_order(st["stop_order_id"])
            wait_canceled(api, st["stop_order_id"], a.dry_run)
            if (api.order(st["stop_order_id"]) or {}).get("status") == "filled":
                say("%s: the stop filled while making room for a ladder buy; no buy. The plan closes next run." % sym)
                if not a.dry_run:
                    save(trail_path(sym), st)
                continue
            st["stop_order_id"] = None
        for lv in due:
            api.submit_order(symbol=sym, qty=lv["qty"], side="buy", type="market", time_in_force="day")
            lv["done"] = True
            held += lv["qty"]
            log("trail_ladder", symbol=sym, qty=lv["qty"], price=price, drop_pct=lv["drop_pct"])
            say("%s: LADDER BUY %d at about %.2f (-%g%% from entry)." % (sym, lv["qty"], price, lv["drop_pct"]))
        new_floor = floor_for(st, st["hwm"])
        order = api.order(st["stop_order_id"]) if st.get("stop_order_id") else None
        live = order and order.get("status") in ("new", "accepted", "held", "pending_new",
                                                  "partially_filled", "pending_replace", "dry_run")
        if not live:
            so = api.submit_order(symbol=sym, qty=held, side="sell", type="stop",
                                  stop_price=new_floor, time_in_force="gtc")
            st["stop_order_id"] = so.get("id")
            say("%s: placed a stop order for %d at %.2f." % (sym, held, new_floor))
        elif new_floor > float(order.get("stop_price") or 0) + 0.004 or int(float(order.get("qty") or 0)) != held:
            ro = api.replace_order(st["stop_order_id"], qty=held, stop_price=new_floor)
            st["stop_order_id"] = (ro or {}).get("id", st["stop_order_id"])
            say("%s: price %.2f, high %.2f. Stop raised %.2f -> %.2f for %d shares."
                % (sym, price, st["hwm"], float(order.get("stop_price") or 0), new_floor, held))
            log("trail_raise", symbol=sym, price=price, hwm=st["hwm"], floor=new_floor, qty=held)
        else:
            say("%s: price %.2f, high %.2f, stop %.2f. No change." % (sym, price, st["hwm"], new_floor))
        st["floor"], st["qty"] = new_floor, held
        if not a.dry_run:
            save(trail_path(sym), st)
    return 0


# ---------------------------------------------------------------- strategy 2: copy Congress trades

def default_ptr():
    """The Pelosi tracker: $TRADER_PELOSI_PTR, the user skills folder, or the newest copy
    installed as a plugin (Claude Code plugins, or the Claude desktop app's plugin folder)."""
    if os.environ.get("TRADER_PELOSI_PTR"):
        return os.environ["TRADER_PELOSI_PTR"]
    home = os.path.expanduser("~/.claude/skills/trader-pelosi/ptr.py")
    if os.path.exists(home):
        return home
    found = glob.glob(os.path.expanduser("~/.claude/plugins/**/trader-pelosi/ptr.py"), recursive=True)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += glob.glob(os.path.join(local, "Claude-Data", "*", "rpm", "*", "*", "*",
                                        "skills", "trader-pelosi", "ptr.py"))
    return os.path.normpath(max(found, key=os.path.getmtime) if found else home)


def copy_rows(data):
    """(key, row) pairs from `ptr.py new --read --json` output or a single `parse --json` report."""
    reports = data.get("reports") if "reports" in data else [{"filing": {}, "report": data}]
    out = []
    for rep in reports:
        r = rep.get("report") or {}
        doc = (rep.get("filing") or {}).get("doc_id") or r.get("doc_id") or "?"
        for i, row in enumerate(r.get("rows") or []):
            out.append(("%s:%d" % (doc, i), dict(row, member=r.get("name"))))
    return out


def copy_decide(row, held_qty, dollars, today, max_age):
    """('buy', notional) | ('sell', qty) | ('skip', reason)."""
    t, tick = row.get("type"), row.get("ticker")
    if not tick:
        return "skip", "no ticker on the filing"
    if row.get("option") or row.get("asset_type") not in (None, "ST"):
        return "skip", "not a plain stock (%s); options and funds are not copied" % (row.get("asset_type") or "option")
    if row.get("date"):
        age = (today - datetime.date.fromisoformat(row["date"])).days
        if age > max_age:
            return "skip", "trade was %d days ago, older than --max-age %d" % (age, max_age)
    if t == "P":
        return "buy", dollars
    if t in ("S", "S (partial)"):
        if held_qty <= 0:
            return "skip", "a sale, and you do not hold %s" % tick
        return "sell", held_qty if t == "S" else round(held_qty / 2, 6)
    return "skip", "transaction type %s is not copied" % t


def copy_run(api, a):
    state_path = os.path.join(DIR, "copy.json")
    state = load(state_path, {"done": [], "trades": []})
    if a.from_json:
        data = load(a.from_json)
    else:
        ptr = a.ptr or default_ptr()
        if not os.path.exists(ptr):
            say("copy: the Pelosi tracker is not installed (%s). Install the Day Trader pack, "
                "or point to its ptr.py with --ptr or TRADER_PELOSI_PTR." % ptr)
            return 1
        cmd = [sys.executable, ptr, "new", "--read", "--json", "--state",
               os.path.join(DIR, "copy-ptr-state.json")] + sum([["--member", m] for m in a.member], [])
        if a.dry_run:
            cmd.append("--dry-run")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode not in (0, 1) or not res.stdout.strip():
            say("copy: ptr.py failed: %s" % (res.stderr.strip() or res.stdout.strip())[:300])
            return 1
        data = json.loads(res.stdout)
        unread = [r for r in data.get("reports", []) if r.get("report") is None]
        if unread:
            say("copy: %d filing(s) could not be read (pdftotext is not installed: brew install poppler)."
                % len(unread))
    rows = [(k, r) for k, r in copy_rows(data) if k not in state["done"]]
    if not rows:
        say("No new trades to copy for %s." % ", ".join(a.member or ["the filings given"]))
        return 0
    if not market_open(api):
        say("(%d new filing rows wait for the next run during market hours.)" % len(rows))
        return 0
    today, sent = datetime.date.today(), 0
    for key, row in rows:
        pos = api.position(row["ticker"]) if row.get("ticker") else None
        held = float(pos["qty"]) if pos else 0.0
        action, val = copy_decide(row, held, a.dollars, today, a.max_age)
        what = "%s %s %s (traded %s, %s)" % (row.get("member") or "?", row.get("type_name") or row.get("type"),
                                            row.get("ticker") or row.get("asset"), row.get("date"), row.get("amount"))
        if action == "skip":
            say("SKIP  %s: %s" % (what, val))
        elif sent >= a.max_orders:
            say("WAIT  %s: hit --max-orders %d this run" % (what, a.max_orders))
            continue
        else:
            if action == "buy":
                o = api.submit_order(symbol=row["ticker"], notional=money(val), side="buy",
                                     type="market", time_in_force="day")
                say("COPY  %s -> BUY $%.2f of %s (paper)" % (what, val, row["ticker"]))
            else:
                o = api.submit_order(symbol=row["ticker"], qty=val, side="sell", type="market",
                                     time_in_force="day")
                say("COPY  %s -> SELL %s %s (paper)" % (what, val, row["ticker"]))
            sent += 1
            state["trades"].append({"key": key, "ticker": row["ticker"], "action": action, "value": val,
                                    "order_id": o.get("id"), "member": row.get("member"),
                                    "trade_date": row.get("date"), "copied": today.isoformat()})
            log("copy_" + action, ticker=row["ticker"], value=val, member=row.get("member"), key=key)
        state["done"].append(key)
    if not a.dry_run:
        save(state_path, state)
    return 0


# ---------------------------------------------------------------- strategy 3: the wheel

def occ_underlying(sym):
    i = 0
    while i < len(sym) and not sym[i].isdigit():
        i += 1
    return sym[:i]


def pick_contract(contracts, quotes, kind, target, today, dte_min, dte_max):
    """Puts: the highest strike at or below target. Calls: the lowest strike at
    or above target. Among those, the expiration nearest the middle of the
    window, with a real bid."""
    mid_dte = (dte_min + dte_max) / 2
    ok = []
    for c in contracts:
        k = float(c["strike_price"])
        dte = (datetime.date.fromisoformat(c["expiration_date"]) - today).days
        bid, ask = quotes.get(c["symbol"], (0, 0))
        if not (dte_min <= dte <= dte_max) or bid <= 0 or ask <= 0:
            continue
        if (kind == "put" and k <= target) or (kind == "call" and k >= target):
            ok.append((k, dte, c, bid, ask))
    if not ok:
        return None
    best_k = max(o[0] for o in ok) if kind == "put" else min(o[0] for o in ok)
    k, dte, c, bid, ask = min((o for o in ok if o[0] == best_k), key=lambda o: abs(o[1] - mid_dte))
    return {"symbol": c["symbol"], "strike": k, "expiration": c["expiration_date"], "dte": dte,
            "bid": bid, "ask": ask, "limit": money(max(bid, (bid + ask) / 2))}


def wheel_path(sym):
    return os.path.join(DIR, "wheel-%s.json" % sym)


def wheel_start(api, a):
    sym = a.symbol.upper()
    lo, hi = (int(x) for x in a.dte.split("-"))
    st = load(wheel_path(sym)) or {"symbol": sym, "premium_total": 0.0, "cost_basis": None,
                                   "open": None, "cycles": [], "history": []}
    st.update(put_otm=a.put_otm, call_over=a.call_over, dte_min=lo, dte_max=hi,
              take_profit=a.take_profit, contracts=a.contracts, status="on")
    if not a.dry_run:
        save(wheel_path(sym), st)
    say("Wheel on %s set: sell puts ~%g%% below the price, calls >= %g%% above your cost, %d-%d days out, "
        "close at %g%% profit, %d contract(s). `wheel run` does the work."
        % (sym, a.put_otm, a.call_over, lo, hi, a.take_profit, a.contracts))
    if a.dry_run:
        say("Preview of the first `wheel run` (nothing is saved or sent):")
        say(wheel_step(api, st, datetime.date.today(), True))
    return 0


def wheel_step(api, st, today, dry):
    sym, n = st["symbol"], st["contracts"]
    pos = api.position(sym)
    shares = int(float(pos["qty"])) if pos else 0
    opts = {p["symbol"]: p for p in api.positions()
            if p.get("asset_class") == "us_option" and occ_underlying(p["symbol"]) == sym}
    op = st.get("open")

    # 1. reconcile what we opened earlier
    if op:
        p = opts.get(op["symbol"])
        if op["status"] == "opening":
            if p:
                op["credit"] = abs(float(p["avg_entry_price"]))
                op["status"] = "open"
                st["premium_total"] = money(st["premium_total"] + op["credit"] * 100 * n)
                st["history"].append({"date": today.isoformat(), "event": "sold " + op["kind"],
                                      "symbol": op["symbol"], "credit": op["credit"]})
            else:
                o = api.order(op["order_id"]) if op.get("order_id") else None
                if not o or o.get("status") in ("canceled", "expired", "rejected", "done_for_day"):
                    st["open"] = op = None
                else:
                    return "%s: waiting for the %s order to fill." % (sym, op["kind"])
        elif op["status"] == "closing" and not p:
            o = api.order(op["close_order_id"]) or {}
            paid = float(o.get("filled_avg_price") or op.get("close_limit") or 0)
            st["premium_total"] = money(st["premium_total"] - paid * 100 * n)
            st["history"].append({"date": today.isoformat(), "event": "bought back " + op["kind"],
                                  "symbol": op["symbol"], "paid": paid})
            st["open"] = None
            return "%s: bought back %s at %.2f (sold at %.2f). The next contract is sold on the next run." % (
                sym, op["symbol"], paid, op["credit"])
        elif op["status"] in ("open", "closing") and not p:
            # expired or assigned
            if op["kind"] == "put" and shares >= op["shares_before"] + 100 * n:
                st["cost_basis"] = money(op["strike"] - op["credit"])
                st["history"].append({"date": today.isoformat(), "event": "put assigned",
                                      "strike": op["strike"], "cost_basis": st["cost_basis"]})
                msg = "ASSIGNED: bought %d %s at %.2f; cost after premiums %.2f." % (100 * n, sym, op["strike"], st["cost_basis"])
                log("wheel_assigned", symbol=sym, qty=100 * n, price=op["strike"], dry_run=dry)
            elif op["kind"] == "call" and shares <= op["shares_before"] - 100 * n:
                gain = (op["strike"] - (st["cost_basis"] or op["strike"])) * 100 * n
                st["cycles"].append({"ended": today.isoformat(), "stock_gain": money(gain),
                                     "premium_total": st["premium_total"]})
                st["history"].append({"date": today.isoformat(), "event": "called away",
                                      "strike": op["strike"], "stock_gain": money(gain)})
                st["cost_basis"] = None
                log(("cc" if st.get("mode") == "cc" else "wheel") + "_called_away", symbol=sym, qty=100 * n,
                    price=op["strike"], dry_run=dry)
                msg = "CALLED AWAY at %.2f: stock gain %.2f. %s" % (
                    op["strike"], gain, "New calls once you hold 100 shares again." if st.get("mode") == "cc"
                    else "Back to selling puts.")
            else:
                st["history"].append({"date": today.isoformat(), "event": op["kind"] + " expired",
                                      "symbol": op["symbol"]})
                msg = "%s expired worthless; kept %.2f a share." % (op["symbol"], op["credit"])
            st["open"] = op = None
            return "%s: %s" % (sym, msg)

    # 2. manage an open short option: take profit early
    if op and op["status"] == "open":
        bid, ask = api.option_quotes([op["symbol"]]).get(op["symbol"], (0, 0))
        target = op["credit"] * (1 - st["take_profit"] / 100)
        if 0 < ask <= target:
            o = api.submit_order(symbol=op["symbol"], qty=n, side="buy", type="limit",
                                 limit_price=money(ask), time_in_force="day")
            op.update(status="closing", close_order_id=o.get("id"), close_limit=money(ask))
            log("wheel_close", symbol=op["symbol"], credit=op["credit"], ask=ask)
            return "%s: %s now %.2f vs %.2f sold: >= %g%% profit, BUYING TO CLOSE." % (
                sym, op["symbol"], ask, op["credit"], st["take_profit"])
        return "%s: holding %s (sold %.2f, now %.2f bid / %.2f ask; close at <= %.2f)." % (
            sym, op["symbol"], op["credit"], bid, ask, target)
    if op:
        return "%s: waiting on the %s order." % (sym, op["status"])

    # 3. nothing open: sell the next contract
    if st.get("status") != "on":
        return "%s: stopped; no new contracts." % sym
    lo = (today + datetime.timedelta(days=st["dte_min"])).isoformat()
    hi = (today + datetime.timedelta(days=st["dte_max"])).isoformat()
    if shares >= 100 * n:
        if st.get("cost_basis") is None:
            st["cost_basis"] = money(float(pos["avg_entry_price"]))
        target = st["cost_basis"] * (1 + st["call_over"] / 100)
        if st.get("mode") == "cc":  # covered calls only: also --otm above today's price
            target = max(target, api.last_price(sym) * (1 + st["call_otm"] / 100))
        kind, strike_lo, strike_hi = "call", max(target, st["cost_basis"]), None
    elif st.get("mode") == "cc":
        st["cost_basis"] = None
        return "%s: you hold %d shares; %d covered call(s) need %d. Nothing to sell." % (sym, shares, n, 100 * n)
    else:
        price = api.last_price(sym)
        target = price * (1 - st["put_otm"] / 100)
        kind, strike_lo, strike_hi = "put", None, target
    cons = api.option_contracts(sym, kind, lo, hi, strike_lo, strike_hi)
    quotes = api.option_quotes([c["symbol"] for c in cons]) if cons else {}
    pick = pick_contract(cons, quotes, kind, target if kind == "put" else max(target, st["cost_basis"]),
                         today, st["dte_min"], st["dte_max"])
    if not pick:
        return "%s: no %s with a bid between %s and %s at the strike rule (target %.2f). Try a wider --dte." % (
            sym, kind, lo, hi, target)
    if kind == "call" and pick["strike"] < st["cost_basis"]:
        return "%s: refusing a call below your cost basis %.2f." % (sym, st["cost_basis"])
    if kind == "put":
        cash = float(api.account().get("cash") or 0)
        need = pick["strike"] * 100 * n
        if cash < need:
            return "%s: not selling the %.2f put: it needs $%.0f cash to buy the shares if assigned, you have $%.0f." % (
                sym, pick["strike"], need, cash)
    o = api.submit_order(symbol=pick["symbol"], qty=n, side="sell", type="limit",
                         limit_price=pick["limit"], time_in_force="day")
    st["open"] = {"symbol": pick["symbol"], "kind": kind, "strike": pick["strike"],
                  "expiration": pick["expiration"], "credit": pick["limit"], "order_id": o.get("id"),
                  "status": "opening", "shares_before": shares}
    log("wheel_sell", kind=kind, dry_run=dry, **pick)
    return "%s: SELL %d %s %s %.2f strike exp %s (%d days) at %.2f limit (bid %.2f / ask %.2f)." % (
        sym, n, kind.upper(), pick["symbol"], pick["strike"], pick["expiration"], pick["dte"],
        pick["limit"], pick["bid"], pick["ask"])


def wheel_run(api, a):
    files = [f for f in sorted(os.listdir(DIR)) if f.startswith("wheel-")] if os.path.isdir(DIR) else []
    plans = [p for p in (load(os.path.join(DIR, f)) for f in files) if p and p.get("status") == "on"]
    if not plans:
        say("No wheel plans. Start one with `wheel start SYMBOL`.")
        return 0
    if not market_open(api):
        return 0
    for st in plans:
        say(wheel_step(api, st, datetime.date.today(), a.dry_run))
        if not a.dry_run:
            save(wheel_path(st["symbol"]), st)
    return 0


# ---------------------------------------------------------------- strategy 8: covered calls only

def cc_path(sym):
    return os.path.join(DIR, "cc-%s.json" % sym)


def strategy_symbols():
    """{symbol: strategy} for everything another bot strategy trades or holds."""
    out = {}
    for f in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
        st = load(os.path.join(DIR, f)) if f.endswith(".json") else None
        if not st:
            continue
        if f.startswith(("trail-", "wheel-")) and st.get("status") in ("open", "on"):
            out[st["symbol"]] = f.split("-")[0]
        elif f.startswith("collar-") and (st.get("status") == "on" or st.get("open")):
            out[st["symbol"]] = "collar"
        elif f in ("swing.json", "bounce.json"):
            out.update({s: f[:-5] for s in st.get("positions", {})})
        elif f in ("orb.json", "gap.json", "fcr.json"):
            out.update({s: f[:-5] for s, t in (st.get("today") or {}).items()
                        if t["status"] in ("open", "buying", "entering")})
        elif f == "dca.json" and st.get("status") == "on":
            out.update({s: "dca" for s in st.get("weights", {})})
        elif f == "rebal.json" and (st.get("status") == "on" or any(st.get("shares", {}).values())):
            out.update({s: "rebal" for s in st.get("weights", {})})
        elif f == "rotate.json":
            out.update({s: "rotate" for s, q in st.get("shares", {}).items() if q > 0})
    return out


class PretendShares:
    """For a dry-run preview: Alpaca as it is, plus the shares --buy-shares would buy."""

    def __init__(self, api, sym, qty, price):
        self._api, self._sym, self._qty, self._price = api, sym, qty, price

    def __getattr__(self, name):
        return getattr(self._api, name)

    def position(self, sym):
        if sym == self._sym:
            return {"symbol": sym, "qty": str(self._qty), "avg_entry_price": str(self._price)}
        return self._api.position(sym)


def cc_start(api, a):
    sym, n = a.symbol.upper(), a.contracts
    other = strategy_symbols().get(sym)
    if other:
        say("cc: %s is traded by the %s strategy; covered calls could get its shares called away. "
            "Pick another name." % (sym, other))
        return 1
    try:
        lo, hi = (int(x) for x in a.dte.split("-"))
    except ValueError:
        say("cc: --dte is a range of days, e.g. 14-28")
        return 1
    if n < 1 or a.otm < 0 or a.over_cost < 0 or not 0 < a.take_profit < 100 or not 0 < lo <= hi:
        say("cc: --contracts >= 1, --otm and --over-cost >= 0, --take-profit 1-99, --dte like 14-28")
        return 1
    st = load(cc_path(sym)) or {"symbol": sym, "premium_total": 0.0, "cost_basis": None,
                                "open": None, "cycles": [], "history": []}
    st.update(mode="cc", call_otm=a.otm, call_over=a.over_cost, put_otm=0, dte_min=lo, dte_max=hi,
              take_profit=a.take_profit, contracts=n, status="on")
    pos = api.position(sym)
    shares, need = (int(float(pos["qty"])) if pos else 0), 100 * n
    price = api.last_price(sym)
    preview_api = api
    if shares < need:
        missing = need - shares
        if not a.buy_shares:
            say("cc: you hold %d %s; %d covered call(s) need %d. Buy the other %d first (about $%.0f), "
                "or add --buy-shares." % (shares, sym, n, need, missing, missing * price))
            return 1
        cash = float(api.account().get("cash") or 0)
        if missing * price > cash:
            say("cc: %d %s cost about $%.0f and cash is $%.0f." % (missing, sym, missing * price, cash))
            return 1
        if not market_open(api):
            return 1
        o = wait_fill(api, api.submit_order(symbol=sym, qty=missing, side="buy", type="market",
                                            time_in_force="day"))
        log("cc_buy_shares", symbol=sym, qty=missing, price=o.get("filled_avg_price"), dry_run=a.dry_run)
        if a.dry_run or o.get("status") == "filled":
            say("%s %d %s at about %.2f (to have %d to cover)." % (
                "WOULD BUY" if a.dry_run else "BOUGHT", missing, sym, float(o.get("filled_avg_price") or price), need))
        else:
            say("ORDERED %d %s at market; %s filled so far (%s). `cc run` sells the call once all %d are in." % (
                missing, sym, o.get("filled_qty") or 0, o.get("status"), need))
        if a.dry_run:
            preview_api = PretendShares(api, sym, need, price)
    if not a.dry_run:
        save(cc_path(sym), st)
    say("Covered calls on %s: sell %d call(s) %d-%d days out, strike at least %g%% above today's price and %g%% "
        "above your cost, never below cost; buy back at %g%% profit, then sell the next. If the stock closes "
        "above the strike at expiration, the shares are sold at the strike (called away). Puts are never sold. "
        "`cc run` does the work."
        % (sym, n, lo, hi, a.otm, a.over_cost, a.take_profit))
    if a.dry_run:
        say("Preview of the first `cc run` (nothing is saved or sent):")
        say(wheel_step(preview_api, st, datetime.date.today(), True))
    return 0


def cc_run(api, a):
    files = [f for f in sorted(os.listdir(DIR)) if f.startswith("cc-") and f.endswith(".json")] \
        if os.path.isdir(DIR) else []
    plans = [p for p in (load(os.path.join(DIR, f)) for f in files)
             if p and (p.get("status") == "on" or p.get("open"))]
    if not plans:
        say("No covered-call plans. Start one with `cc start SYMBOL`.")
        return 0
    if not market_open(api):
        return 0
    for st in plans:
        say(wheel_step(api, st, datetime.date.today(), a.dry_run))
        if not a.dry_run:
            save(cc_path(st["symbol"]), st)
    return 0


def cc_stop(api, a):
    sym = a.symbol.upper()
    st = load(cc_path(sym))
    if not st:
        say("No covered-call plan on %s." % sym)
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(cc_path(sym), st)
    say("Covered calls on %s: no new calls.%s" % (sym, " The open call %s is still managed (bought back at %g%% "
        "profit, or it expires or is assigned)." % (st["open"]["symbol"], st["take_profit"]) if st.get("open") else ""))
    return 0


# ---------------------------------------------------------------- strategy 13: protective collar

def collar_path(sym):
    return os.path.join(DIR, "collar-%s.json" % sym)


def collar_pick(puts, calls, quotes, put_target, call_target, today, dte_min, dte_max):
    """One expiration with both legs: the highest put strike at or under put_target and the lowest call strike
    at or over call_target, each with a real bid and ask. Expirations nearest the middle of the window first."""
    mid_dte = (dte_min + dte_max) / 2
    exps = sorted({c["expiration_date"] for c in puts + calls},
                  key=lambda e: abs((datetime.date.fromisoformat(e) - today).days - mid_dte))
    for exp in exps:
        dte = (datetime.date.fromisoformat(exp) - today).days
        if not dte_min <= dte <= dte_max:
            continue
        legs = {}
        for kind, cons, ok in (("put", puts, lambda k: k <= put_target), ("call", calls, lambda k: k >= call_target)):
            cands = []
            for c in cons:
                bid, ask = quotes.get(c["symbol"], (0, 0))
                if c["expiration_date"] == exp and bid > 0 and ask > 0 and ok(float(c["strike_price"])):
                    cands.append((float(c["strike_price"]), c["symbol"], bid, ask))
            if cands:
                k, s, bid, ask = (max if kind == "put" else min)(cands)
                legs[kind] = {"symbol": s, "strike": k, "bid": bid, "ask": ask, "limit": money((bid + ask) / 2)}
        if len(legs) == 2:
            return dict(legs, expiration=exp, dte=dte)
    return None


def collar_leg_fill(api, leg, opts, n):
    """Update one leg from Alpaca: 'open' once the position is there, 'dead' if the order ended unfilled."""
    p = opts.get(leg["symbol"])
    if p and abs(float(p["qty"])) >= n:
        o = (api.order(leg["order_id"]) if leg.get("order_id") else None) or {}
        leg.update(status="open", price=money(abs(float(o.get("filled_avg_price") or p["avg_entry_price"]))))
    else:
        o = api.order(leg["order_id"]) if leg.get("order_id") else None
        if not o or o.get("status") in ("canceled", "expired", "rejected", "done_for_day"):
            leg["status"] = "dead"


def collar_send(api, sym, leg, kind, n, dry):
    side = "buy" if kind == "put" else "sell"
    bid, ask = api.option_quotes([leg["symbol"]]).get(leg["symbol"], (leg.get("bid", 0), leg.get("ask", 0)))
    if bid > 0 and ask > 0:
        leg.update(bid=bid, ask=ask, limit=money((bid + ask) / 2))
    o = api.submit_order(symbol=leg["symbol"], qty=n, side=side, type="limit", limit_price=leg["limit"],
                         time_in_force="day")
    leg.update(order_id=o.get("id"), status="open" if dry else "opening")
    log("collar_" + side, symbol=leg["symbol"], kind=kind, strike=leg["strike"], limit=leg["limit"], dry_run=dry)
    return "%s %d %s %s %.2f strike at %.2f limit (bid %.2f / ask %.2f)" % (
        "BUY" if kind == "put" else "SELL", n, kind.upper(), leg["symbol"], leg["strike"], leg["limit"],
        leg.get("bid", 0), leg.get("ask", 0))


def collar_step(api, st, today, dry):
    sym, n = st["symbol"], st["contracts"]
    pos = api.position(sym)
    shares = int(float(pos["qty"])) if pos else 0
    opts = {p["symbol"]: p for p in api.positions()
            if p.get("asset_class") == "us_option" and occ_underlying(p["symbol"]) == sym}
    op = st.get("open")
    out = []

    # 1. legs still being filled: record fills; resend a leg whose day order ended unfilled
    if op and any(op[k]["status"] in ("opening", "dead") for k in ("put", "call")):
        for kind in ("put", "call"):
            if op[kind]["status"] == "opening":
                collar_leg_fill(api, op[kind], opts, n)
        if all(op[k]["status"] == "dead" for k in ("put", "call")):
            st["open"] = None
            return "%s: neither collar order filled; a fresh collar is priced on the next run." % sym
        for kind in ("put", "call"):
            if op[kind]["status"] == "dead" and st.get("status") == "on":
                out.append("%s: the %s order ended unfilled; sending it again: %s." % (
                    sym, kind, collar_send(api, sym, op[kind], kind, n, dry)))
        if any(op[k]["status"] != "open" for k in ("put", "call")):
            return "\n".join(out) or "%s: waiting for the collar orders to fill (put %s, call %s)." % (
                sym, op["put"]["status"], op["call"]["status"])
        op["net"] = money(op["put"]["price"] - op["call"]["price"])
        st["net_total"] = money(st["net_total"] - op["net"] * 100 * n)
        st["history"].append({"date": today.isoformat(), "event": "collar on", "expiration": op["expiration"],
                              "put": op["put"]["strike"], "call": op["call"]["strike"], "net": op["net"]})
        return "%s: COLLAR ON, exp %s: put %.2f bought at %.2f, call %.2f sold at %.2f; net %s %.2f a share." % (
            sym, op["expiration"], op["put"]["strike"], op["put"]["price"], op["call"]["strike"],
            op["call"]["price"], "cost" if op["net"] > 0 else "credit", abs(op["net"]))

    # 2. a collar on: report, or settle it once both legs are gone (expired, exercised or assigned)
    if op:
        have = [k for k in ("put", "call") if op[k]["symbol"] in opts]
        if have:
            price = api.last_price(sym)
            dte = (datetime.date.fromisoformat(op["expiration"]) - today).days
            if len(have) == 1 and shares < op["shares_before"]:
                return "%s: the %s was assigned or exercised early (shares %d -> %d); the %s %s is still open. " \
                       "Close it in the dashboard or let it expire." % (
                           sym, "call" if "put" in have else "put", op["shares_before"], shares, have[0],
                           op[have[0]]["symbol"])
            return "%s: collared at %.2f, floor %.2f (put), cap %.2f (call), exp %s (%d days); net %s %.2f a share." % (
                sym, price, op["put"]["strike"], op["call"]["strike"], op["expiration"], dte,
                "cost" if op["net"] > 0 else "credit", abs(op["net"]))
        sold = shares <= op["shares_before"] - 100 * n
        if sold:
            price = api.last_price(sym)
            how, at_k = ("PUT EXERCISED", op["put"]["strike"]) if price <= op["put"]["strike"] else \
                ("CALLED AWAY", op["call"]["strike"])
            msg = "%s: %s, %d shares sold at %.2f. %s" % (
                sym, how, 100 * n, at_k, "New collars once you hold %d shares again." % (100 * n))
        else:
            how, at_k = "EXPIRED", None
            msg = "%s: the collar expiring %s is over; both options expired and the shares stay." % (
                sym, op["expiration"])
        log("collar_" + how.lower().replace(" ", "_"), symbol=sym, qty=100 * n if sold else None, price=at_k,
            dry_run=dry)
        st["cycles"].append({"ended": today.isoformat(), "expiration": op["expiration"], "how": how,
                             "sold_at": at_k, "net": op["net"]})
        st["history"].append({"date": today.isoformat(), "event": how.lower(), "expiration": op["expiration"]})
        st["open"] = op = None
        out.append(msg)
        if sold:
            return "\n".join(out)

    # 3. nothing on: price the next collar
    if st.get("status") != "on":
        return "\n".join(out + ["%s: stopped; no new collars." % sym])
    if shares < 100 * n:
        return "\n".join(out + ["%s: you hold %d shares; %d collar(s) need %d. Nothing to do." % (
            sym, shares, n, 100 * n)])
    if st.get("cost_basis") is None:
        st["cost_basis"] = money(float(pos["avg_entry_price"]))
    price = api.last_price(sym)
    put_t = money(price * (1 - st["put_otm"] / 100))
    call_t = money(max(price * (1 + st["call_otm"] / 100), st["cost_basis"] * (1 + st["over_cost"] / 100)))
    lo = (today + datetime.timedelta(days=st["dte_min"])).isoformat()
    hi = (today + datetime.timedelta(days=st["dte_max"])).isoformat()
    puts = api.option_contracts(sym, "put", lo, hi, put_t * 0.85, put_t)
    calls = api.option_contracts(sym, "call", lo, hi, call_t, call_t * 1.15)
    syms = [c["symbol"] for c in puts + calls]
    quotes = api.option_quotes(syms) if syms else {}
    pick = collar_pick(puts, calls, quotes, put_t, call_t, today, st["dte_min"], st["dte_max"])
    if not pick:
        return "\n".join(out + ["%s: no expiration %s to %s with both a put at or under %.2f and a call at or over "
                                "%.2f with real quotes. Try a wider --dte." % (sym, lo, hi, put_t, call_t)])
    net = money(pick["put"]["limit"] - pick["call"]["limit"])
    if net > st["max_debit"]:
        return "\n".join(out + ["%s: the %s collar (put %.2f at %.2f, call %.2f at %.2f) costs %.2f a share, more "
                                "than --max-debit %.2f; not opened. Wider strikes or a later expiration cost less." % (
                                    sym, pick["expiration"], pick["put"]["strike"], pick["put"]["limit"],
                                    pick["call"]["strike"], pick["call"]["limit"], net, st["max_debit"])])
    op = st["open"] = {"expiration": pick["expiration"], "put": pick["put"], "call": pick["call"],
                       "shares_before": shares, "net": net}
    lines = [collar_send(api, sym, op["put"], "put", n, dry), collar_send(api, sym, op["call"], "call", n, dry)]
    if dry:
        op["put"]["price"], op["call"]["price"] = op["put"]["limit"], op["call"]["limit"]
    out.append("%s at %.2f: COLLAR exp %s (%d days): %s; %s. Net %s %.2f a share ($%.0f). Floor %.2f (-%.1f%%), "
               "cap %.2f (+%.1f%%)." % (
                   sym, price, pick["expiration"], pick["dte"], lines[0], lines[1],
                   "cost" if net > 0 else "credit", abs(net), abs(net) * 100 * n,
                   pick["put"]["strike"], (1 - pick["put"]["strike"] / price) * 100,
                   pick["call"]["strike"], (pick["call"]["strike"] / price - 1) * 100))
    return "\n".join(out)


def collar_start(api, a):
    sym, n = a.symbol.upper(), a.contracts
    other = strategy_symbols().get(sym)
    if other and other != "collar":
        say("collar: %s is traded by the %s strategy; a collar would sell calls or protect shares it manages. "
            "Pick another name." % (sym, other))
        return 1
    try:
        lo, hi = (int(x) for x in a.dte.split("-"))
    except ValueError:
        say("collar: --dte is a range of days, e.g. 30-60")
        return 1
    if n < 1 or a.put_otm < 0 or a.call_otm < 0 or a.over_cost < 0 or not 0 < lo <= hi:
        say("collar: --contracts >= 1, --put-otm, --call-otm and --over-cost >= 0, --dte like 30-60")
        return 1
    st = load(collar_path(sym)) or {"symbol": sym, "net_total": 0.0, "cost_basis": None, "open": None,
                                    "cycles": [], "history": []}
    st.update(put_otm=a.put_otm, call_otm=a.call_otm, over_cost=a.over_cost, dte_min=lo, dte_max=hi,
              max_debit=a.max_debit, contracts=n, status="on")
    pos = api.position(sym)
    shares, need = (int(float(pos["qty"])) if pos else 0), 100 * n
    price = api.last_price(sym)
    preview_api = api
    if shares < need:
        missing = need - shares
        if not a.buy_shares:
            say("collar: you hold %d %s; %d collar(s) need %d. Buy the other %d first (about $%.0f), "
                "or add --buy-shares." % (shares, sym, n, need, missing, missing * price))
            return 1
        cash = float(api.account().get("cash") or 0)
        if missing * price > cash:
            say("collar: %d %s cost about $%.0f and cash is $%.0f." % (missing, sym, missing * price, cash))
            return 1
        if not market_open(api):
            return 1
        o = wait_fill(api, api.submit_order(symbol=sym, qty=missing, side="buy", type="market",
                                            time_in_force="day"))
        log("collar_buy_shares", symbol=sym, qty=missing, price=o.get("filled_avg_price"), dry_run=a.dry_run)
        if a.dry_run or o.get("status") == "filled":
            say("%s %d %s at about %.2f (to have %d to collar)." % (
                "WOULD BUY" if a.dry_run else "BOUGHT", missing, sym, float(o.get("filled_avg_price") or price), need))
        else:
            say("ORDERED %d %s at market; %s filled so far (%s). `collar run` opens the collar once all %d are in."
                % (missing, sym, o.get("filled_qty") or 0, o.get("status"), need))
        if a.dry_run:
            preview_api = PretendShares(api, sym, need, price)
    if not a.dry_run:
        save(collar_path(sym), st)
    say("Collar on %s: for each 100 shares, buy a put about %g%% below the price (the floor) and sell a call about "
        "%g%% above it (the cap; never under %g%% over your cost), same expiration %d-%d days out, at the bid/ask "
        "midpoints, only if the pair costs at most %.2f a share. Below the put strike at expiration the shares are "
        "sold at the strike; above the call strike they are called away; in between both expire and the next "
        "collar is put on. `collar run` does the work." % (
            sym, a.put_otm, a.call_otm, a.over_cost, lo, hi, a.max_debit))
    if a.dry_run:
        say("Preview of the first `collar run` (nothing is saved or sent):")
        say(collar_step(preview_api, st, datetime.date.today(), True))
    return 0


def collar_run(api, a):
    files = [f for f in sorted(os.listdir(DIR)) if f.startswith("collar-") and f.endswith(".json")] \
        if os.path.isdir(DIR) else []
    plans = [p for p in (load(os.path.join(DIR, f)) for f in files)
             if p and (p.get("status") == "on" or p.get("open"))]
    if not plans:
        say("No collars. Start one with `collar start SYMBOL`.")
        return 0
    if not market_open(api):
        return 0
    for st in plans:
        say(collar_step(api, st, datetime.date.today(), a.dry_run))
        if not a.dry_run:
            save(collar_path(st["symbol"]), st)
    return 0


def collar_stop(api, a):
    sym = a.symbol.upper()
    st = load(collar_path(sym))
    if not st:
        say("No collar on %s." % sym)
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(collar_path(sym), st)
    say("Collar on %s: no new collars.%s" % (sym, " The open one (exp %s) stays until it expires." % st["open"][
        "expiration"] if st.get("open") else ""))
    return 0


# ---------------------------------------------------------------- strategy 4: trend swing

SWING = os.path.join(DIR, "swing.json")
LIVE = ("new", "accepted", "held", "pending_new", "partially_filled", "pending_replace", "dry_run")


def trend_module():
    """trend.py from the trader-agents skill, which sits next to this one."""
    path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-agents"))
    if not os.path.exists(os.path.join(path, "trend.py")):
        raise alp.AlpacaError("swing needs the trader-agents skill next to trader-bot (no trend.py in %s)" % path)
    if path not in sys.path:
        sys.path.insert(0, path)
    import trend
    return trend


def swing_signal(sym):
    """Today's trend-rule state for sym (verdict SETUP / WATCH / NO), or None if
    there is not enough history. Daily closes from Yahoo; today's bar is the
    latest price until the close."""
    tr = trend_module()
    closes = [c for _, c in tr.load_chart(sym, None, "2y")]
    return tr.evaluate(closes)[-1]


def other_bot_symbols():
    """Symbols an open trailing-stop, wheel or covered-call plan trades, so the others keep out of them."""
    out = set()
    for f in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
        if f.startswith(("trail-", "wheel-", "cc-", "collar-")) and f.endswith(".json"):
            st = load(os.path.join(DIR, f)) or {}
            if st.get("status") in ("open", "on") or (f.startswith("collar-") and st.get("open")):
                out.add(st.get("symbol"))
    rot = load(ROT) if os.path.isdir(DIR) else None
    out.update(s for s, q in ((rot or {}).get("shares") or {}).items() if q > 0)
    return out


def wait_canceled(api, oid, dry, seconds=10):
    if dry or not oid:
        return
    for _ in range(seconds * 2):
        if (api.order(oid) or {}).get("status") in ("canceled", "filled", "expired", "rejected"):
            return
        time.sleep(0.5)


def daily_close(st, sym, today, px, reason, dry, name):
    p = st["positions"].pop(sym)
    ret = (px / p["entry"] - 1) * 100
    st["closed"].append({"symbol": sym, "qty": p["qty"], "entry": p["entry"], "exit": money(px),
                         "opened": p["opened"], "closed": today.isoformat(), "ret_pct": round(ret, 2),
                         "reason": reason})
    log(name + "_sell", symbol=sym, qty=p["qty"], entry=p["entry"], exit=money(px), reason=reason, dry_run=dry)
    return ret


def place_stop(api, st, sym, p, qty):
    p["stop"] = money(p["entry"] * (1 - st["stop_pct"] / 100))
    so = api.submit_order(symbol=sym, qty=qty, side="sell", type="stop",
                          stop_price=p["stop"], time_in_force="gtc")
    p["stop_order_id"], p["qty"] = so.get("id"), qty


class Daily:
    """What differs between the strategies that hold for days: their name, the
    word for a buy signal, the entry rule and the exit rule.
      entry(sym) -> (True, info dict) or (False, why-not text); may raise
      exit(sym, p) -> reason text or None; may raise (then it holds)"""

    def __init__(self, name, label, entry, exit):
        self.name, self.label, self.entry, self.exit = name, label, entry, exit


def daily_step(api, st, today, dry, strat):
    """One pass: manage what the strategy holds, then buy fresh signals. Returns messages."""
    out, held_map, name, label = [], st["positions"], strat.name, strat.label

    # 1. what it holds
    for sym in sorted(held_map):
        p = held_map[sym]
        if p.get("status") == "buying":
            o = api.order(p["buy_order_id"]) or {}
            if o.get("status") == "filled":
                p.update(entry=money(float(o["filled_avg_price"])), status="open")
                place_stop(api, st, sym, p, int(float(o.get("filled_qty") or p["qty"])))
                out.append("%s: buy filled at %.2f; stop %.2f placed at Alpaca." % (sym, p["entry"], p["stop"]))
            elif o.get("status") in LIVE:
                out.append("%s: waiting for the buy order to fill." % sym)
            else:
                held_map.pop(sym)
                out.append("%s: the buy order ended %s; nothing bought." % (sym, o.get("status", "unknown")))
            continue
        pos = api.position(sym)
        qty = int(float(pos["qty"])) if pos else 0
        if qty <= 0:
            o = api.order(p["stop_order_id"]) if p.get("stop_order_id") else None
            stopped = bool(o) and o.get("status") == "filled"
            px = float((o or {}).get("filled_avg_price") or p["stop"])
            ret = daily_close(st, sym, today, px, "stop filled" if stopped else "position gone", dry, name)
            out.append("%s: %s at about %.2f (%+.1f%% vs entry %.2f). Closed." % (
                sym, "STOPPED OUT" if stopped else "position is gone", px, ret, p["entry"]))
            continue
        age = (today - datetime.date.fromisoformat(p["opened"])).days
        if age >= st["hold_days"]:
            reason = "held %d days (limit %d)" % (age, st["hold_days"])
        else:
            try:
                reason = strat.exit(sym, p)
            except Exception as e:  # no data today: keep holding, the stop still protects
                out.append("%s: no price data (%s); holding, stop still at Alpaca." % (sym, e))
                continue
        if reason:
            if p.get("stop_order_id"):
                api.cancel_order(p["stop_order_id"])
                wait_canceled(api, p["stop_order_id"], dry)
                if not dry:
                    pos = api.position(sym)
                    qty = int(float(pos["qty"])) if pos else 0
                    if qty <= 0:
                        continue  # the stop filled first; the next run records it
            o = wait_fill(api, api.submit_order(symbol=sym, qty=qty, side="sell", type="market",
                                                time_in_force="day"))
            px = float(o.get("filled_avg_price") or api.last_price(sym))
            ret = daily_close(st, sym, today, px, reason, dry, name)
            out.append("%s: %s %d at about %.2f: %s. %+.1f%% vs entry %.2f." % (
                sym, "WOULD SELL" if dry else "SELL", qty, px, reason, ret, p["entry"]))
            continue
        o = api.order(p["stop_order_id"]) if p.get("stop_order_id") else None
        if not (o and o.get("status") in LIVE):
            place_stop(api, st, sym, p, qty)
            out.append("%s: stop order was missing; placed one for %d at %.2f." % (sym, qty, p["stop"]))
        elif int(float(o.get("qty") or 0)) != qty:
            ro = api.replace_order(p["stop_order_id"], qty=qty)
            p["stop_order_id"], p["qty"] = (ro or {}).get("id", p["stop_order_id"]), qty
        price = api.last_price(sym)
        out.append("%s: holding %d, entry %.2f, now %.2f (%+.1f%%), day %d of %d, stop %.2f." % (
            sym, qty, p["entry"], price, (price / p["entry"] - 1) * 100, age, st["hold_days"], p["stop"]))

    # 2. new entries
    if st.get("status") != "on":
        return out
    slots = st["max_positions"] - len(held_map)
    busy = other_bot_symbols()
    cash = float(api.account().get("cash") or 0)
    last_closed = {c["symbol"]: c["closed"] for c in st["closed"]}
    for sym in st["symbols"]:
        if sym in held_map:
            continue
        if sym in last_closed:
            again = datetime.date.fromisoformat(last_closed[sym]) + datetime.timedelta(days=st.get("cooldown_days", 21))
            if today < again:
                out.append("%s: closed on %s; no new buy before %s." % (sym, last_closed[sym], again.isoformat()))
                continue
        if sym in busy:
            out.append("%s: skipped, a trailing-stop or wheel plan already trades it." % sym)
            continue
        try:
            ok, info = strat.entry(sym)
        except Exception as e:
            out.append("%s: no price data (%s)." % (sym, e))
            continue
        if not ok:
            out.append("%s: %s, no entry." % (sym, info))
            continue
        if slots <= 0:
            out.append("%s: %s, but %d %s positions are open (the max)." % (sym, label, st["max_positions"], name))
            continue
        if api.position(sym):
            out.append("%s: %s, but you already hold it outside the %s; skipped." % (sym, label, name))
            continue
        price = api.last_price(sym)
        qty = int(st["dollars"] // price)
        if qty < 1:
            out.append("%s: %s, but $%g buys no whole share at %.2f." % (sym, label, st["dollars"], price))
            continue
        if qty * price > cash:
            out.append("%s: %s, but %d shares need $%.0f and cash is $%.0f." % (
                sym, label, qty, qty * price, cash))
            continue
        o = wait_fill(api, api.submit_order(symbol=sym, qty=qty, side="buy", type="market",
                                            time_in_force="day"))
        p = {"qty": qty, "entry": money(float(o.get("filled_avg_price") or price)),
             "opened": today.isoformat(), "stop": None, "stop_order_id": None, "status": "open"}
        if o.get("status") in ("filled", "dry_run"):
            place_stop(api, st, sym, p, qty)
        else:
            p.update(status="buying", buy_order_id=o.get("id"))
        held_map[sym], cash, slots = p, cash - qty * p["entry"], slots - 1
        log(name + "_buy", symbol=sym, qty=qty, entry=p["entry"], stop=p["stop"], dry_run=dry, **info)
        sell_by = (today + datetime.timedelta(days=st["hold_days"])).isoformat()
        out.append("%s: %s -> %s %d at about %.2f; stop %s at Alpaca (-%g%%); sell by %s." % (
            sym, label, "WOULD BUY" if dry else "BUY", qty, p["entry"],
            "%.2f" % p["stop"] if p["stop"] else "placed once it fills", st["stop_pct"], sell_by))
    return out


def swing_entry(sym):
    s = swing_signal(sym)
    if not s:
        return False, "not enough history for the 200-day rule"
    if s["verdict"] != "SETUP":
        return False, "%s (%s)" % (s["verdict"], trend_module().why(s))
    return True, {"roc": s["roc"], "pct_over_ma": s["pct_over_ma"]}


def swing_exit(sym, p):
    s = swing_signal(sym)
    if s and not s["above_ma"]:
        return "closed below the 200-day average (%.2f < %.2f)" % (s["close"], s["ma"])
    return None


SWING_RULES = Daily("swing", "SETUP", lambda s: swing_entry(s), lambda s, p: swing_exit(s, p))


def swing_step(api, st, today, dry):
    return daily_step(api, st, today, dry, SWING_RULES)


def swing_start(api, a):
    syms = sorted({s.upper() for s in a.symbols})
    errs = []
    if not 0 < a.stop < 100:
        errs.append("--stop must be between 0 and 100 (percent below entry)")
    if a.dollars <= 0 or a.hold_days < 1 or a.max_positions < 1 or a.cooldown < 0:
        errs.append("--dollars, --hold-days and --max-positions must be positive, --cooldown 0 or more")
    for e in errs:
        say("swing: " + e)
    if errs:
        return 1
    trend_module()  # fail now, not on the first scheduled run
    st = load(SWING) or {"positions": {}, "closed": []}
    st.update(symbols=syms, dollars=a.dollars, stop_pct=a.stop, hold_days=a.hold_days,
              max_positions=a.max_positions, cooldown_days=a.cooldown, status="on")
    if not a.dry_run:
        save(SWING, st)
    say("Trend swing set on %s: when a name is above a rising 200-day average and its 3-month rate of change "
        "has reset near zero after a run, buy about $%g of it with a stop %g%% below at Alpaca. Sell after %d days "
        "or when it closes below the 200-day average. At most %d at a time. `swing run` does the work."
        % (", ".join(syms), a.dollars, a.stop, a.hold_days, a.max_positions))
    if a.dry_run:
        say("Preview of the first `swing run` (nothing is saved or sent):")
        for m in swing_step(api, st, datetime.date.today(), True):
            say(m)
    return 0


def swing_run(api, a):
    st = load(SWING)
    if not st or (st.get("status") != "on" and not st.get("positions")):
        say("No trend swing plan. Start one with `swing start SYMBOL ...`.")
        return 0
    if not market_open(api):
        return 0
    for m in swing_step(api, st, datetime.date.today(), a.dry_run):
        say(m)
    if not a.dry_run:
        save(SWING, st)
    return 0


def swing_stop(api, a):
    st = load(SWING)
    if not st:
        say("No trend swing plan.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(SWING, st)
    say("Trend swing: no new buys. %d open position(s) keep their stops and exit rules; "
        "`swing run` still manages them." % len(st["positions"]))
    return 0


# ---------------------------------------------------------------- strategy 6: oversold bounce

BOUNCE = os.path.join(DIR, "bounce.json")


def rsi_series(closes, n=14):
    """Wilder's RSI over n sessions, same as the Day Trader scanner. None until
    there are n price changes."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(c, 0) for c in ch[:n]) / n
    al = sum(max(-c, 0) for c in ch[:n]) / n

    def rsi(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[n] = rsi(ag, al)
    for i in range(n + 1, len(closes)):
        c = ch[i - 1]
        ag = (ag * (n - 1) + max(c, 0)) / n
        al = (al * (n - 1) + max(-c, 0)) / n
        out[i] = rsi(ag, al)
    return out


def bounce_signal(sym):
    """Daily closes from Yahoo (today's bar is the latest price until the close):
    RSI 14 today and yesterday, the 20- and 200-day averages."""
    closes = [c for _, c in trend_module().load_chart(sym, None, "2y")]
    if len(closes) < 30:
        return None
    r = rsi_series(closes)
    return {"close": round(closes[-1], 2), "prev": round(closes[-2], 2),
            "rsi": None if r[-1] is None else round(r[-1], 1),
            "rsi_prev": None if r[-2] is None else round(r[-2], 1),
            "ma20": round(sum(closes[-20:]) / 20, 2),
            "ma200": round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else None}


def bounce_rules(st):
    def entry(sym):
        s = bounce_signal(sym)
        if not s or s["rsi_prev"] is None:
            return False, "not enough history for RSI 14"
        if not (s["rsi_prev"] < 30 and s["close"] > s["prev"]):
            return False, "RSI %.1f (yesterday %.1f); needs under 30 yesterday and an up close today (%.2f vs %.2f)" % (
                s["rsi"], s["rsi_prev"], s["close"], s["prev"])
        if st.get("above_200") and not (s["ma200"] and s["close"] > s["ma200"]):
            return False, "oversold bounce, but %.2f is under the 200-day average %s (--above-200)" % (
                s["close"], s["ma200"])
        return True, {"rsi": s["rsi"], "rsi_prev": s["rsi_prev"], "ma20": s["ma20"]}

    def exit(sym, p):
        s = bounce_signal(sym)
        if s and s["close"] >= s["ma20"]:
            return "back at the 20-day average (%.2f >= %.2f)" % (s["close"], s["ma20"])
        return None
    return Daily("bounce", "BOUNCE", entry, exit)


def bounce_start(api, a):
    syms = sorted({s.upper() for s in a.symbols})
    errs = []
    if not 0 < a.stop < 100:
        errs.append("--stop must be between 0 and 100 (percent below entry)")
    if a.dollars <= 0 or a.hold_days < 1 or a.max_positions < 1 or a.cooldown < 0:
        errs.append("--dollars, --hold-days and --max-positions must be positive, --cooldown 0 or more")
    for e in errs:
        say("bounce: " + e)
    if errs:
        return 1
    trend_module()  # the price history comes through trader-agents' trend.py
    st = load(BOUNCE) or {"positions": {}, "closed": []}
    st.update(symbols=syms, dollars=a.dollars, stop_pct=a.stop, hold_days=a.hold_days,
              max_positions=a.max_positions, cooldown_days=a.cooldown, above_200=a.above_200, status="on")
    if not a.dry_run:
        save(BOUNCE, st)
    say("Oversold bounce on %s: when RSI 14 was under 30 yesterday and today closes up%s, buy about $%g with a "
        "stop %g%% below at Alpaca. Sell when it closes back at its 20-day average, or after %d days. At most %d "
        "at a time; no rebuy for %d days. `bounce run` does the work, once a day near the close."
        % (", ".join(syms), " while above its 200-day average" if a.above_200 else "", a.dollars, a.stop,
           a.hold_days, a.max_positions, a.cooldown))
    if a.dry_run:
        say("Preview of the first `bounce run` (nothing is saved or sent):")
        for m in daily_step(api, st, datetime.date.today(), True, bounce_rules(st)):
            say(m)
    return 0


def bounce_run(api, a):
    st = load(BOUNCE)
    if not st or (st.get("status") != "on" and not st.get("positions")):
        say("No oversold-bounce plan. Start one with `bounce start SYMBOL ...`.")
        return 0
    if not market_open(api):
        return 0
    for m in daily_step(api, st, datetime.date.today(), a.dry_run, bounce_rules(st)):
        say(m)
    if not a.dry_run:
        save(BOUNCE, st)
    return 0


def bounce_stop(api, a):
    st = load(BOUNCE)
    if not st:
        say("No oversold-bounce plan.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(BOUNCE, st)
    say("Oversold bounce: no new buys. %d open position(s) keep their stops and exit rules; "
        "`bounce run` still manages them." % len(st["positions"]))
    return 0


# ---------------------------------------------------------------- strategy 7: dollar-cost averaging

DCA = os.path.join(DIR, "dca.json")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def parse_weights(items):
    """['SPY:60', 'QQQ:40'] or ['SPY', 'QQQ'] -> {'SPY': 0.6, 'QQQ': 0.4}."""
    raw = {}
    for it in items:
        sym, _, w = it.partition(":")
        raw[sym.upper()] = float(w) if w else 1.0
    if any(w <= 0 for w in raw.values()):
        raise ValueError("weights must be positive")
    total = sum(raw.values())
    return {s: w / total for s, w in raw.items()}


def dca_next(st, after, inclusive):
    """The next buy date on or after (inclusive) / strictly after `after`."""
    d = after if inclusive else after + datetime.timedelta(days=1)
    if st["every"] in ("weekly", "biweekly"):
        want = WEEKDAYS.index(st["day"])
        d += datetime.timedelta(days=(want - d.weekday()) % 7)
        if st["every"] == "biweekly" and not inclusive:
            d += datetime.timedelta(days=7)
        return d
    day = int(st["day"])
    if d.day > day:
        d = (d.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    return d.replace(day=day)


def dca_value(api, st):
    """(dollars invested, value now, {sym: [shares, cost]}) from the strategy's own buys."""
    held = {}
    for b in st["buys"]:
        if b["status"] == "filled":
            h = held.setdefault(b["symbol"], [0.0, 0.0])
            h[0] += b["qty"]
            h[1] += b["qty"] * b["price"]
    cost = sum(h[1] for h in held.values())
    value = sum(h[0] * api.last_price(s) for s, h in held.items())
    return cost, value, held


def dca_step(api, st, today, dry):
    out = []
    # 1. settle buys that had not filled yet
    for b in st["buys"]:
        if b["status"] == "pending":
            o = api.order(b["order_id"]) or {}
            if o.get("status") == "filled":
                b.update(status="filled", qty=float(o.get("filled_qty") or b["qty"]),
                         price=float(o["filled_avg_price"]))
            elif o.get("status") not in LIVE:
                b["status"] = o.get("status", "unknown")
                st["carry"][b["symbol"]] = st["carry"].get(b["symbol"], 0) + b["dollars"]
                out.append("%s: the %s buy ended %s; its $%.2f goes into the next one." % (
                    b["symbol"], b["date"], b["status"], b["dollars"]))
    if st.get("status") != "on":
        return out
    nxt = datetime.date.fromisoformat(st["next"])
    if today < nxt:
        out.append("Next DCA buy on %s ($%g across %s)." % (st["next"], st["dollars"], ", ".join(st["weights"])))
        return out
    spent = sum(b["dollars"] for b in st["buys"] if b["status"] in ("filled", "pending"))
    budget = st["dollars"]
    if st.get("cap"):
        budget = min(budget, st["cap"] - spent)
        if budget < 1:
            st["status"] = "done"
            out.append("DCA reached its $%g cap; no more buys. What it bought stays put." % st["cap"])
            return out
    cash = float(api.account().get("cash") or 0)
    if cash < budget:
        out.append("DCA: $%.0f is due but cash is $%.0f; skipped this one." % (budget, cash))
        st["next"] = dca_next(st, today, False).isoformat()
        return out
    for sym, w in st["weights"].items():
        amt = round(budget * w + st["carry"].get(sym, 0), 2)
        price = api.last_price(sym)
        asset = api.asset(sym) or {}
        if asset.get("fractionable") and amt >= 1:
            order, qty = dict(notional=amt), amt / price
        else:
            qty = int(amt // price)
            if qty < 1:
                st["carry"][sym] = amt
                out.append("%s: $%.2f buys no whole share at %.2f (not fractionable); carried to the next buy."
                           % (sym, amt, price))
                continue
            order = dict(qty=qty)
        o = wait_fill(api, api.submit_order(symbol=sym, side="buy", type="market", time_in_force="day", **order))
        filled = o.get("status") in ("filled", "dry_run")
        px = float(o.get("filled_avg_price") or price)
        qty = float(o.get("filled_qty") or qty)
        used = round(qty * px, 2) if "qty" in order else amt
        st["carry"][sym] = round(amt - used, 2) if "qty" in order else 0
        st["buys"].append({"date": today.isoformat(), "symbol": sym, "dollars": used, "qty": round(qty, 6),
                           "price": money(px), "order_id": o.get("id"), "status": "filled" if filled else "pending"})
        log("dca_buy", symbol=sym, dollars=used, qty=round(qty, 6), price=money(px), dry_run=dry)
        out.append("%s: %s $%.2f = %s shares at about %.2f." % (
            sym, "WOULD BUY" if dry else "BUY", used, ("%.4f" % qty).rstrip("0").rstrip("."), px))
    st["next"] = dca_next(st, today, False).isoformat()
    out.append("Next DCA buy on %s." % st["next"])
    return out


def dca_start(api, a):
    try:
        weights = parse_weights(a.symbols)
    except ValueError as e:
        say("dca: %s (use SYMBOL or SYMBOL:WEIGHT, e.g. SPY:60 QQQ:40)" % e)
        return 1
    errs = []
    if a.dollars < 1:
        errs.append("--dollars must be at least 1")
    if a.every == "monthly":
        if not (a.day.isdigit() and 1 <= int(a.day) <= 28):
            errs.append("--day for monthly is a day of the month, 1-28")
    elif a.day not in WEEKDAYS:
        errs.append("--day for weekly/biweekly is one of %s" % ", ".join(WEEKDAYS))
    if a.cap is not None and a.cap < a.dollars:
        errs.append("--cap must be at least --dollars")
    for e in errs:
        say("dca: " + e)
    if errs:
        return 1
    st = load(DCA) or {"buys": [], "carry": {}}
    st.update(weights=weights, dollars=a.dollars, every=a.every, day=a.day, cap=a.cap, status="on")
    st["next"] = dca_next(st, datetime.date.today(), True).isoformat()
    if not a.dry_run:
        save(DCA, st)
    when = {"weekly": "every %s" % a.day.capitalize(), "biweekly": "every other %s" % a.day.capitalize(),
            "monthly": "on day %s of each month" % a.day}[a.every]
    say("Dollar-cost averaging: $%g %s into %s, at market, fractional shares where Alpaca allows. No stops and "
        "no selling: it is the buy-and-hold baseline.%s First buy on %s." % (
            a.dollars, when, ", ".join("%s %.0f%%" % (s, w * 100) for s, w in weights.items()),
            " Stops after $%g in total." % a.cap if a.cap else "", st["next"]))
    if a.dry_run:
        say("Preview of a buy day (nothing is saved or sent):")
        st["next"] = datetime.date.today().isoformat()
        for m in dca_step(api, st, datetime.date.today(), True):
            say(m)
    return 0


def dca_run(api, a):
    st = load(DCA)
    if not st or st.get("status") not in ("on",) and not any(b["status"] == "pending" for b in st.get("buys", [])):
        say("No dollar-cost averaging plan%s." % (" running" if st else ". Start one with `dca start SYMBOL ...`"))
        return 0
    if not market_open(api):
        return 0
    for m in dca_step(api, st, datetime.date.today(), a.dry_run):
        say(m)
    if not a.dry_run:
        save(DCA, st)
    return 0


def dca_stop(api, a):
    st = load(DCA)
    if not st:
        say("No dollar-cost averaging plan.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(DCA, st)
    say("Dollar-cost averaging stopped: no more buys. What it bought stays in the account.")
    return 0


# ---------------------------------------------------------------- strategy 9: rebalance to target weights

REBAL = os.path.join(DIR, "rebal.json")


def rebal_next(st, after, inclusive):
    """The next rebalance date: day N of each month, or of Jan/Apr/Jul/Oct for quarterly."""
    d = dca_next({"every": "monthly", "day": st["day"]}, after, inclusive)
    while st["every"] == "quarterly" and d.month not in (1, 4, 7, 10):
        d = dca_next({"every": "monthly", "day": st["day"]}, d, False)
    return d


def rebal_book(api, st):
    """(total value, {sym: (shares, price, value, weight now)}) of what the sleeve holds."""
    rows = {}
    for sym in st["weights"]:
        q = st["shares"].get(sym, 0.0)
        p = api.last_price(sym)
        rows[sym] = [q, p, q * p]
    total = sum(r[2] for r in rows.values())
    for r in rows.values():
        r.append(r[2] / total if total else 0.0)
    return total, rows


def rebal_trade(api, st, sym, side, dollars, price, dry, out):
    """One market order for about `dollars` of sym. Fractional (notional) where Alpaca allows,
    whole shares otherwise. Updates the sleeve's share count once filled."""
    if (api.asset(sym) or {}).get("fractionable"):
        order, qty = dict(notional=round(dollars, 2)), dollars / price
    else:
        qty = int(dollars // price)
        if side == "sell":
            qty = min(qty, int(st["shares"].get(sym, 0)))
        if qty < 1:
            out.append("%s: $%.2f is less than one whole share at %.2f (not fractionable); skipped." % (
                sym, dollars, price))
            return 0.0
        order = dict(qty=qty)
    o = wait_fill(api, api.submit_order(symbol=sym, side=side, type="market", time_in_force="day", **order))
    px = float(o.get("filled_avg_price") or price)
    qty = float(o.get("filled_qty") or qty)
    if o.get("status") in ("filled", "dry_run"):
        st["shares"][sym] = round(st["shares"].get(sym, 0.0) + (qty if side == "buy" else -qty), 6)
    else:
        st["pending"].append({"order_id": o.get("id"), "symbol": sym, "side": side})
    log("rebal_" + side, symbol=sym, qty=round(qty, 6), price=money(px), dry_run=dry)
    out.append("%s: %s %s shares (~$%.2f) at about %.2f." % (
        sym, ("WOULD " + side.upper()) if dry else side.upper(), ("%.4f" % qty).rstrip("0").rstrip("."), qty * px, px))
    return qty * px


def rebal_step(api, st, today, dry):
    out = []
    # 1. settle orders that had not filled
    for p in list(st["pending"]):
        o = api.order(p["order_id"]) or {}
        if o.get("status") == "filled":
            q = float(o.get("filled_qty") or 0)
            st["shares"][p["symbol"]] = round(st["shares"].get(p["symbol"], 0.0) + (q if p["side"] == "buy" else -q), 6)
            st["pending"].remove(p)
        elif o.get("status") not in LIVE:
            st["pending"].remove(p)
            out.append("%s: a %s order ended %s." % (p["symbol"], p["side"], o.get("status", "unknown")))
    if st["pending"]:
        out.append("Waiting for %d order(s) to fill before trading again." % len(st["pending"]))
        return out
    if st.get("status") != "on":
        return out
    if today < datetime.date.fromisoformat(st["next"]):
        total, rows = rebal_book(api, st)
        drift = max(abs(r[3] - st["weights"][s]) for s, r in rows.items()) * 100 if total else 0
        out.append("Rebalance: worth $%.2f, largest drift %.1f points; next check %s." % (total, drift, st["next"]))
        return out
    cash = float(api.account().get("cash") or 0)

    # 2. first time: put the money in at the target weights
    if not st.get("funded"):
        if cash < st["dollars"]:
            out.append("Rebalance: needs $%.0f to start, cash is $%.0f; nothing bought." % (st["dollars"], cash))
            return out
        for sym, w in st["weights"].items():
            rebal_trade(api, st, sym, "buy", st["dollars"] * w, api.last_price(sym), dry, out)
        st["funded"] = True
        st["history"].append({"date": today.isoformat(), "event": "funded", "dollars": st["dollars"]})
        st["next"] = rebal_next(st, today, False).isoformat()
        out.append("Funded with $%g at the target weights. Next rebalance check %s." % (st["dollars"], st["next"]))
        return out

    # 3. a rebalance date: trade only if something drifted past the band
    total, rows = rebal_book(api, st)
    st["next"] = rebal_next(st, today, False).isoformat()
    if not total:
        out.append("Rebalance: the sleeve holds nothing (sold outside the bot?). Next check %s." % st["next"])
        return out
    drifts = {s: (r[3] - st["weights"][s]) * 100 for s, r in rows.items()}
    worst = max(drifts, key=lambda s: abs(drifts[s]))
    summary = ", ".join("%s %.1f%% (target %.0f%%)" % (s, rows[s][3] * 100, st["weights"][s] * 100) for s in rows)
    if abs(drifts[worst]) < st["band"]:
        out.append("Rebalance: %s. Largest drift %.1f points, inside the %g-point band; no trades. Next check %s."
                   % (summary, abs(drifts[worst]), st["band"], st["next"]))
        st["history"].append({"date": today.isoformat(), "event": "checked", "value": money(total),
                              "max_drift": round(abs(drifts[worst]), 2)})
        return out
    out.append("Rebalance: %s. %s is %.1f points off; back to target:" % (summary, worst, abs(drifts[worst])))
    diffs = {s: total * st["weights"][s] - rows[s][2] for s in rows}
    for s, d in sorted(diffs.items(), key=lambda kv: kv[1]):  # sells first, to free the cash
        if d <= -st["min_trade"]:
            cash += rebal_trade(api, st, s, "sell", min(-d, rows[s][2]), rows[s][1], dry, out)
    for s, d in sorted(diffs.items(), key=lambda kv: -kv[1]):
        if d >= st["min_trade"]:
            amt = min(d, cash)
            if amt < st["min_trade"]:
                out.append("%s: needs $%.2f more but cash is short; skipped." % (s, d))
                continue
            cash -= rebal_trade(api, st, s, "buy", amt, rows[s][1], dry, out)
    st["history"].append({"date": today.isoformat(), "event": "rebalanced", "value": money(total),
                          "max_drift": round(abs(drifts[worst]), 2)})
    out.append("Next rebalance check %s." % st["next"])
    return out


def rebal_start(api, a):
    try:
        weights = parse_weights(a.symbols)
    except ValueError as e:
        say("rebal: %s (use SYMBOL:WEIGHT, e.g. VTI:60 BND:40)" % e)
        return 1
    errs = []
    if len(weights) < 2:
        errs.append("give at least two names to balance")
    if a.dollars < 100:
        errs.append("--dollars must be at least 100")
    if not (a.day.isdigit() and 1 <= int(a.day) <= 28):
        errs.append("--day is a day of the month, 1-28")
    if a.band < 0 or a.min_trade < 1:
        errs.append("--band must be 0 or more and --min-trade at least 1")
    others = strategy_symbols()
    for s, other in ((s, others.get(s)) for s in weights):
        if not other or other == "rebal":
            continue
        errs.append("%s is traded by the %s strategy; pick another name" % (s, other))
    for e in errs:
        say("rebal: " + e)
    if errs:
        return 1
    st = load(REBAL)
    if st and st.get("funded") and set(st["weights"]) != set(weights):
        say("rebal: a funded plan holds %s; changing the names would orphan shares. `rebal stop` first, "
            "or keep the same names with new weights." % ", ".join(st["weights"]))
        return 1
    st = st or {"shares": {}, "pending": [], "history": [], "funded": False}
    st.update(weights=weights, dollars=a.dollars, every=a.every, day=a.day, band=a.band,
              min_trade=a.min_trade, status="on")
    st["next"] = datetime.date.today().isoformat() if not st["funded"] else rebal_next(
        st, datetime.date.today(), True).isoformat()
    if not a.dry_run:
        save(REBAL, st)
    say("Rebalance: put $%g into %s, then on day %s of each %s sell what is overweight and buy what is "
        "underweight, if any name is %g or more points off target (trades under $%g are skipped). "
        "No stops: it is a long-term allocation. `rebal run` does the work." % (
            a.dollars, ", ".join("%s %.0f%%" % (s, w * 100) for s, w in weights.items()), a.day,
            "month" if a.every == "monthly" else "quarter (Jan, Apr, Jul, Oct)", a.band, a.min_trade))
    if a.dry_run:
        say("Preview of the first `rebal run` (nothing is saved or sent):")
        for m in rebal_step(api, st, datetime.date.today(), True):
            say(m)
    return 0


def rebal_run(api, a):
    st = load(REBAL)
    if not st or (st.get("status") != "on" and not st.get("pending")):
        say("No rebalance plan%s." % (" running" if st else ". Start one with `rebal start SYM:W ...`"))
        return 0
    if not market_open(api):
        return 0
    for m in rebal_step(api, st, datetime.date.today(), a.dry_run):
        say(m)
    if not a.dry_run:
        save(REBAL, st)
    return 0


def rebal_stop(api, a):
    st = load(REBAL)
    if not st:
        say("No rebalance plan.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(REBAL, st)
    say("Rebalance stopped: no more trades. What it holds stays in the account.")
    return 0


# ---------------------------------------------------------------- strategy 11: momentum rotation

ROT = os.path.join(DIR, "rotate.json")


def rot_closes(sym):
    """Daily closes (dividend-adjusted), oldest first, from Yahoo; the last one is today's price until the close."""
    return [c for _, c in trend_module().load_chart(sym, None, "2y")]


def momentum(closes, lookback, skip):
    """Return over `lookback` sessions, ending `skip` sessions ago, in percent; None without enough history."""
    if len(closes) < lookback + skip + 1:
        return None
    return (closes[-1 - skip] / closes[-1 - skip - lookback] - 1) * 100


def rot_value(api, st):
    """(holdings value, {sym: (shares, price, value)}) of what the rotation holds."""
    rows = {}
    for sym, q in st["shares"].items():
        if q > 0:
            p = api.last_price(sym)
            rows[sym] = (q, p, q * p)
    return sum(r[2] for r in rows.values()), rows


def rot_book(st, sym, side, qty, px):
    q = st["shares"].get(sym, 0.0) + (qty if side == "buy" else -qty)
    st["shares"][sym] = round(q, 6) if q > 1e-6 else 0.0
    st["cash"] = round(st["cash"] + (-qty * px if side == "buy" else qty * px), 2)
    if not st["shares"][sym]:
        del st["shares"][sym]


def rot_trade(api, st, sym, side, price, dry, out, dollars=None, qty=None):
    """A market order: buy about `dollars` (fractional where Alpaca allows, else whole shares), or sell `qty` shares."""
    if side == "buy":
        if (api.asset(sym) or {}).get("fractionable"):
            order, qty = dict(notional=round(dollars, 2)), dollars / price
        else:
            qty = int(dollars // price)
            if qty < 1:
                out.append("%s: $%.2f buys no whole share at %.2f (not fractionable); skipped." % (sym, dollars, price))
                return
            order = dict(qty=qty)
    else:
        order = dict(qty=qty)
    o = wait_fill(api, api.submit_order(symbol=sym, side=side, type="market", time_in_force="day", **order))
    px = float(o.get("filled_avg_price") or price)
    if o.get("status") in ("filled", "dry_run"):
        rot_book(st, sym, side, float(o.get("filled_qty") or qty), px)
    else:
        st["pending"].append({"order_id": o.get("id"), "symbol": sym, "side": side})
    log("rotate_" + side, symbol=sym, qty=round(qty, 6), price=money(px), dry_run=dry)
    out.append("%s: %s %s shares (~$%.2f) at about %.2f." % (
        sym, ("WOULD " + side.upper()) if dry else side.upper(), ("%.4f" % qty).rstrip("0").rstrip("."), qty * px, px))


def rot_rank(st, out):
    """[(momentum %, sym)] best first, for the names with enough history."""
    ranked = []
    for sym in st["symbols"]:
        try:
            m = momentum(rot_closes(sym), st["lookback"], st["skip"])
        except Exception as e:
            out.append("%s: no daily closes from Yahoo (%s); left out this time." % (sym, e))
            continue
        if m is None:
            out.append("%s: not enough history for a %d-session lookback; left out." % (sym, st["lookback"]))
            continue
        ranked.append((m, sym))
    return sorted(ranked, reverse=True)


def rot_step(api, st, today, dry):
    out = []
    # 1. settle orders that had not filled
    for p in list(st["pending"]):
        o = api.order(p["order_id"]) or {}
        if o.get("status") == "filled":
            rot_book(st, p["symbol"], p["side"], float(o.get("filled_qty") or 0),
                     float(o.get("filled_avg_price") or 0))
            st["pending"].remove(p)
        elif o.get("status") not in LIVE:
            st["pending"].remove(p)
            out.append("%s: a %s order ended %s." % (p["symbol"], p["side"], o.get("status", "unknown")))
    if st["pending"]:
        out.append("Waiting for %d order(s) to fill before trading again." % len(st["pending"]))
        return out
    if st.get("status") != "on":
        return out
    held, rows = rot_value(api, st)
    if today < datetime.date.fromisoformat(st["next"]):
        out.append("Rotation: holds %s; sleeve cash $%.2f; next rotation %s." % (
            ", ".join(sorted(rows)) or "nothing", st["cash"], st["next"]))
        return out

    # 2. rank, and pick the top names above the momentum floor that no other strategy trades
    ranked = rot_rank(st, out)
    out.append("Rotation, %d-session return%s: %s." % (
        st["lookback"], " skipping the last %d" % st["skip"] if st["skip"] else "",
        ", ".join("%s %+.1f%%" % (s, m) for m, s in ranked) or "no names ranked"))
    if not ranked:
        out.append("Nothing ranked, so nothing traded. Next try on the next run.")
        return out
    busy = {s: o for s, o in strategy_symbols().items() if o != "rotate"}
    picks = []
    for m, sym in ranked:
        if len(picks) == st["top"] or m <= st["min_mom"]:
            break
        if sym in busy and sym not in rows:
            out.append("%s: ranks in, but the %s strategy trades it; the next name takes its place." % (sym, busy[sym]))
            continue
        picks.append(sym)
    if len(picks) < st["top"]:
        out.append("Only %d name(s) usable above %+g%%: %d slot(s) stay in cash." % (
            len(picks), st["min_mom"], st["top"] - len(picks)))

    # 3. sell what dropped out, then buy what came in
    for sym, (q, px, v) in sorted(rows.items()):
        if sym in picks:
            continue
        pos = api.position(sym)
        have = min(q, float(pos["qty"])) if pos else 0.0
        if have <= 0:
            out.append("%s: the rotation's shares are gone from the account (sold by hand?); written off." % sym)
            st["shares"].pop(sym, None)
            continue
        out.append("%s dropped out of the top %d:" % (sym, st["top"]))
        rot_trade(api, st, sym, "sell", px, dry, out, qty=have)
    held, rows = rot_value(api, st)
    slot = (held + st["cash"]) / st["top"]
    cash = float(api.account().get("cash") or 0)
    for sym in picks:
        if sym in rows:
            continue
        amt = min(slot, st["cash"], cash)
        if amt < st["min_trade"]:
            out.append("%s: only $%.2f to spend (sleeve cash $%.2f, account cash $%.0f); skipped." % (
                sym, amt, st["cash"], cash))
            continue
        rot_trade(api, st, sym, "buy", api.last_price(sym), dry, out, dollars=amt)
        cash -= amt
    st["history"].append({"date": today.isoformat(), "ranked": [[s, round(m, 2)] for m, s in ranked],
                          "picks": picks})
    st["next"] = dca_next(st, today, False).isoformat()
    out.append("Holding %s. Next rotation %s." % (", ".join(picks) or "cash", st["next"]))
    return out


def rot_start(api, a):
    syms = list(dict.fromkeys(s.upper() for s in a.symbols))
    errs = []
    if a.top < 1 or len(syms) <= a.top:
        errs.append("give more names than --top (%d), so there is something to rotate between" % a.top)
    if a.lookback < 20 or a.skip < 0 or a.lookback + a.skip > 400:
        errs.append("--lookback must be 20 or more sessions, --skip 0 or more, and the two under 400")
    if a.dollars < 100 or a.min_trade < 1:
        errs.append("--dollars must be at least 100 and --min-trade at least 1")
    if a.every == "monthly":
        if not (a.day.isdigit() and 1 <= int(a.day) <= 28):
            errs.append("--day for monthly is a day of the month, 1-28")
    elif a.day not in WEEKDAYS:
        errs.append("--day for weekly is one of %s" % ", ".join(WEEKDAYS))
    for e in errs:
        say("rotate: " + e)
    if errs:
        return 1
    trend_module()  # daily closes come from Yahoo through trader-agents' trend.py
    st = load(ROT) or {"shares": {}, "pending": [], "history": []}
    if not st["shares"] and not st["history"]:
        st.update(cash=a.dollars, dollars=a.dollars)
    elif a.dollars != st["dollars"]:
        say("rotate: already running with $%g; --dollars only applies to a new plan." % st["dollars"])
    st.update(symbols=syms, top=a.top, lookback=a.lookback, skip=a.skip, min_mom=a.min_mom,
              every=a.every, day=a.day, min_trade=a.min_trade, status="on")
    st["next"] = datetime.date.today().isoformat() if not st["history"] else dca_next(
        st, datetime.date.today(), True).isoformat()
    busy = {s: o for s, o in strategy_symbols().items() if o != "rotate" and s in syms}
    if not a.dry_run:
        save(ROT, st)
    say("Momentum rotation over %s: %s, rank them by their %d-session return%s and hold the top %d with "
        "$%g split evenly, at market, fractional where Alpaca allows. A name needs a return above %+g%% to be "
        "held; otherwise its slot sits in cash. Names that drop out are sold, new leaders bought. No stop "
        "orders: the exit is the next rotation. `rotate run` does the work.%s" % (
            ", ".join(syms), "every %s" % a.day.capitalize() if a.every == "weekly"
            else "on day %s of each month" % a.day, a.lookback,
            " (skipping the last %d)" % a.skip if a.skip else "", a.top, st["dollars"], a.min_mom,
            " Traded by other strategies now, so skipped while they are: %s." % ", ".join(
                "%s (%s)" % kv for kv in sorted(busy.items())) if busy else ""))
    if a.dry_run:
        say("Preview of the first `rotate run` (nothing is saved or sent):")
        st["next"] = datetime.date.today().isoformat()
        for m in rot_step(api, st, datetime.date.today(), True):
            say(m)
    return 0


def rot_run(api, a):
    st = load(ROT)
    if not st or (st.get("status") != "on" and not st.get("pending")):
        say("No momentum rotation%s." % (" running" if st else ". Start one with `rotate start SYMBOL ...`"))
        return 0
    if not market_open(api):
        return 0
    for m in rot_step(api, st, datetime.date.today(), a.dry_run):
        say(m)
    if not a.dry_run:
        save(ROT, st)
    return 0


def rot_stop(api, a):
    st = load(ROT)
    if not st:
        say("No momentum rotation.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(ROT, st)
    say("Momentum rotation stopped: no more trades. What it holds (%s) stays in the account." % (
        ", ".join(sorted(st["shares"])) or "nothing"))
    return 0


# ---------------------------------------------------------------- strategy 5: opening-range breakout

ORB = os.path.join(DIR, "orb.json")
BAR = datetime.timedelta(minutes=5)


def parse_ts(s):
    """An ISO time from Alpaca (any fraction digits, Z or an offset) -> aware datetime."""
    return datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", s).replace("Z", "+00:00"))


def et_now(api):
    """New York time, from Alpaca's clock (no time-zone database needed on Windows)."""
    ts = (api.clock() or {}).get("timestamp")
    if not ts:
        raise alp.AlpacaError("Alpaca's clock gave no timestamp")
    return parse_ts(ts)


def at(now, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def orb_range(bars, open_dt, range_min, now):
    """Bars inside the opening range, and completed 5-minute bars after it."""
    end = open_dt + datetime.timedelta(minutes=range_min)
    inside, after = [], []
    for b in bars:
        t = parse_ts(b["t"])
        if open_dt <= t < end:
            inside.append(b)
        elif t >= end and t + BAR <= now:
            after.append(b)
    return inside, after


def orb_close(st, sym, t, px, how, dry):
    risk = t["entry"] - t["stop"]
    t.update(status="closed", exit=money(px), how=how, r=round((px - t["entry"]) / risk, 2) if risk > 0 else 0,
             pnl=money((px - t["entry"]) * t["qty"]))
    st["history"].append(dict(t, symbol=sym))
    log(st.get("kind", "orb") + "_exit", symbol=sym, qty=t["qty"], entry=t["entry"], exit=money(px), how=how, r=t["r"],
        pnl=t["pnl"], dry_run=dry)
    return "%s: %s at about %.2f: %+.2fR, $%+.2f." % (sym, how, px, t["r"], t["pnl"])


def orb_step(api, st, now, dry):
    out, today = [], now.date().isoformat()
    if st.get("day") != today:  # a new session: keep anything still open, drop the rest
        st["today"] = {s: t for s, t in (st.get("today") or {}).items() if t["status"] in ("open", "buying")}
        st["day"] = today
    trades = st["today"]
    open_dt = at(now, "09:30")
    flat_by, last_entry = at(now, st["flat_by"]), at(now, st["last_entry"])

    # 1. today's trades: closed by the bracket? time to be flat?
    for sym in sorted(trades):
        t = trades[sym]
        if t["status"] not in ("open", "buying"):
            continue
        parent = api.order(t["order_id"]) or {}
        pos = api.position(sym)
        qty = int(float(pos["qty"])) if pos else 0
        if t["status"] == "buying":
            if parent.get("status") == "filled":
                t.update(status="open", entry=money(float(parent["filled_avg_price"])))
            elif parent.get("status") in LIVE and now < flat_by:
                out.append("%s: waiting for the entry to fill." % sym)
                continue
            elif parent.get("status") in LIVE:
                api.cancel_order(t["order_id"])
                t["status"] = "canceled"
                out.append("%s: entry never filled; canceled at the flat-by time." % sym)
                continue
            else:
                t["status"] = "canceled"
                out.append("%s: the entry order ended %s; no trade." % (sym, parent.get("status", "unknown")))
                continue
        if qty <= 0:
            px, how = t["stop"], "closed"
            for leg in parent.get("legs") or []:
                if leg.get("status") == "filled":
                    px = float(leg.get("filled_avg_price") or px)
                    how = "TARGET HIT" if leg.get("type") == "limit" else "STOPPED OUT"
            out.append(orb_close(st, sym, t, px, how, dry))
            continue
        if now >= flat_by or t["day"] != today:
            for leg in parent.get("legs") or []:
                if leg.get("status") in LIVE:
                    api.cancel_order(leg["id"])
                    wait_canceled(api, leg["id"], dry)
            if not dry:
                pos = api.position(sym)
                qty = int(float(pos["qty"])) if pos else 0
                if qty <= 0:
                    continue  # a leg filled first; the next run records it
            o = wait_fill(api, api.submit_order(symbol=sym, qty=qty, side="sell", type="market",
                                                time_in_force="day"))
            px = float(o.get("filled_avg_price") or api.last_price(sym))
            out.append(orb_close(st, sym, t, px, "FLAT BY %s" % st["flat_by"] if t["day"] == today
                                 else "LEFT OVER FROM %s, SOLD" % t["day"], dry))
            continue
        price = api.last_price(sym)
        out.append("%s: in the trade, %d @ %.2f, now %.2f (%+.2fR); stop %.2f, target %.2f at Alpaca." % (
            sym, qty, t["entry"], price, (price - t["entry"]) / (t["entry"] - t["stop"]), t["stop"], t["target"]))

    # 2. new entries
    if st.get("status") != "on":
        return out
    waiting = [s for s in st["symbols"] if s not in trades]
    if not waiting:
        return out
    range_end = open_dt + datetime.timedelta(minutes=st["range_min"])
    if now < range_end + BAR:
        out.append("Building the %d-minute opening range; the first breakout check is at %s."
                   % (st["range_min"], (range_end + BAR).strftime("%H:%M")))
        return out
    if now >= last_entry:
        out.append("Past the last entry time (%s); no new trades today." % st["last_entry"])
        return out
    taken = len(trades)
    busy = other_bot_symbols()
    cash = float(api.account().get("cash") or 0)
    for sym in waiting:
        if taken >= st["max_trades"]:
            out.append("%d trades taken today (the max); no more." % st["max_trades"])
            break
        if sym in busy or api.position(sym):
            out.append("%s: held or traded by another strategy; skipped." % sym)
            continue
        inside, after = orb_range(api.bars(sym, open_dt.isoformat(), now.isoformat()), open_dt,
                                  st["range_min"], now)
        if not inside:
            out.append("%s: no bars in the opening range (IEX feed); skipped." % sym)
            continue
        hi, lo = max(float(b["h"]) for b in inside), min(float(b["l"]) for b in inside)
        day_open, label = float(inside[0]["o"]), "BREAKOUT"
        if st.get("min_gap") is not None:
            try:
                prev = prev_close(sym, now.date())
            except Exception as e:
                out.append("%s: no previous close from Yahoo (%s); skipped." % (sym, e))
                continue
            gap = (day_open / prev - 1) * 100
            if gap < st["min_gap"] or (st.get("max_gap") and gap > st["max_gap"]):
                out.append("%s: gap %+.1f%% (open %.2f vs %.2f yesterday); needs %+g%%%s. No trade today." % (
                    sym, gap, day_open, prev, st["min_gap"],
                    " to %+g%%" % st["max_gap"] if st.get("max_gap") else " or more"))
                continue
            label = "GAP-AND-GO, gap %+.1f%%" % gap
        brk = next((b for b in after if float(b["c"]) > hi), None)
        if not brk:
            last = float(after[-1]["c"]) if after else float(inside[-1]["c"])
            out.append("%s: waiting. Range %.2f-%.2f; last 5-min close %.2f (%.2f below the high)."
                       % (sym, lo, hi, last, hi - last))
            continue
        stop = money(day_open - 0.01 if st["stop_at"] == "open"
                     else (hi + lo) / 2 if st["stop_at"] == "mid" else lo)
        brk_t = parse_ts(brk["t"]).astimezone(now.tzinfo)
        brk_at = brk_t.strftime("%H:%M")
        if brk_t + BAR < now - 2 * BAR:
            out.append("%s: the first breakout was the %s bar, more than 10 minutes ago; missed, no entry today."
                       % (sym, brk_at))
            continue
        price = api.last_price(sym)
        if price <= hi:
            out.append("%s: broke out at %s, but %.2f is back at or below the range high %.2f; no entry."
                       % (sym, brk_at, price, hi))
            continue
        risk = price - stop
        if risk <= max(0.02, price * 0.001):
            out.append("%s: broke out at %s, but the stop %.2f is too close to %.2f to survive the spread."
                       % (sym, brk_at, stop, price))
            continue
        if price > hi + 0.5 * (hi - stop):
            out.append("%s: broke out at %s, but %.2f is more than half a risk unit above the high %.2f; "
                       "not chasing." % (sym, brk_at, price, hi))
            continue
        qty = min(int(st["dollars"] // price), int(st["max_loss"] // risk))
        if qty < 1:
            out.append("%s: breakout, but $%g of risk with a %.2f stop buys no whole share."
                       % (sym, st["max_loss"], risk))
            continue
        if qty * price > cash:
            out.append("%s: breakout, but %d shares need $%.0f and cash is $%.0f." % (sym, qty, qty * price, cash))
            continue
        target = money(price + st["target_r"] * risk)
        o = wait_fill(api, api.submit_order(
            symbol=sym, qty=qty, side="buy", type="market", time_in_force="gtc", order_class="bracket",
            take_profit={"limit_price": "%.2f" % target}, stop_loss={"stop_price": "%.2f" % stop}))
        filled = o.get("status") in ("filled", "dry_run")
        entry = money(float(o.get("filled_avg_price") or price))
        trades[sym] = {"day": today, "qty": qty, "entry": entry, "stop": stop, "target": target,
                       "order_id": o.get("id"), "range": [money(lo), money(hi)], "breakout": brk_at,
                       "status": "open" if filled else "buying"}
        taken, cash = taken + 1, cash - qty * entry
        log(st.get("kind", "orb") + "_entry", symbol=sym, qty=qty, entry=entry, stop=stop, target=target, range_high=money(hi),
            range_low=money(lo), dry_run=dry)
        out.append("%s: %s (5-min close %.2f > range high %.2f at %s) -> %s %d at about %.2f; stop %.2f, "
                   "target %.2f (%gR) held at Alpaca; risk $%.0f; flat by %s." % (
                       sym, label, float(brk["c"]), hi, brk_at, "WOULD BUY" if dry else "BUY", qty, entry, stop,
                       target, st["target_r"], qty * risk, st["flat_by"]))
    return out


def orb_start(api, a):
    syms = sorted({s.upper() for s in a.symbols})
    errs = []
    if a.range % 5 or not 5 <= a.range <= 60:
        errs.append("--range must be 5 to 60 minutes, in steps of 5")
    if a.target_r <= 0 or a.dollars <= 0 or a.max_loss <= 0 or a.max_trades < 1:
        errs.append("--target-r, --dollars, --max-loss and --max-trades must be positive")
    try:
        le, fb = (datetime.time(*(int(x) for x in v.split(":"))) for v in (a.last_entry, a.flat_by))
        if not datetime.time(9, 30) < le < fb <= datetime.time(16, 0):
            errs.append("times must run 9:30 < --last-entry < --flat-by <= 16:00")
    except ValueError:
        errs.append("--last-entry and --flat-by are HH:MM, New York time")
    for e in errs:
        say("orb: " + e)
    if errs:
        return 1
    st = load(ORB) or {"today": {}, "history": [], "day": None}
    st.update(symbols=syms, range_min=a.range, stop_at=a.stop, target_r=a.target_r, dollars=a.dollars,
              max_loss=a.max_loss, max_trades=a.max_trades, last_entry=a.last_entry, flat_by=a.flat_by,
              status="on")
    if not a.dry_run:
        save(ORB, st)
    say("Opening-range breakout on %s: the range is the first %d minutes; buy when a 5-minute bar closes above "
        "its high (until %s), stop at the range %s, target %gR, as one bracket order at Alpaca. Up to $%g a "
        "trade and $%g of risk, %d trades a day, one per name, long only. Everything is sold by %s."
        % (", ".join(syms), a.range, a.last_entry, "midpoint" if a.stop == "mid" else "low", a.target_r,
           a.dollars, a.max_loss, a.max_trades, a.flat_by))
    if a.dry_run:
        say("Preview of a `orb run` now (nothing is saved or sent):")
        for m in orb_step(api, st, et_now(api), True):
            say(m)
    return 0


def day_plan_run(api, a, path, title, cmd):
    st = load(path)
    if not st or (st.get("status") != "on" and not any(
            t["status"] in ("open", "buying") for t in (st.get("today") or {}).values())):
        say("No %s plan. Start one with `%s start SYMBOL ...`." % (title, cmd))
        return 0
    if not market_open(api):
        return 0
    for m in orb_step(api, st, et_now(api), a.dry_run):
        say(m)
    if not a.dry_run:
        save(path, st)
    return 0


def day_plan_stop(api, a, path, title):
    st = load(path)
    if not st:
        say("No %s plan." % title)
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(path, st)
    say("%s: no new trades. Anything open keeps its bracket and is still sold by %s."
        % (title[0].upper() + title[1:], st["flat_by"]))
    return 0


def orb_run(api, a):
    return day_plan_run(api, a, ORB, "opening-range", "orb")


def orb_stop(api, a):
    return day_plan_stop(api, a, ORB, "opening-range breakout")


# ---------------------------------------------------------------- strategy 10: gap-and-go

GAP = os.path.join(DIR, "gap.json")


def prev_close(sym, today):
    """Yesterday's official close (not dividend-adjusted), from Yahoo daily bars."""
    tr = trend_module()
    data = tr.http_json("/v8/finance/chart/%s?range=1mo&interval=1d" % sym.replace(".", "-"))
    r = data["chart"]["result"][0]
    off = (r.get("meta") or {}).get("gmtoffset") or 0
    closes = r["indicators"]["quote"][0]["close"]
    rows = [c for t, c in zip(r["timestamp"], closes) if c is not None and
            datetime.datetime.fromtimestamp(t + off, datetime.timezone.utc).date() < today]
    if not rows:
        raise RuntimeError("no daily closes")
    return float(rows[-1])


def gap_start(api, a):
    syms = sorted({s.upper() for s in a.symbols})
    errs = []
    if a.range % 5 or not 5 <= a.range <= 30:
        errs.append("--range must be 5 to 30 minutes, in steps of 5")
    if a.min_gap <= 0 or (a.max_gap is not None and a.max_gap <= a.min_gap):
        errs.append("--min-gap must be positive, and --max-gap above it")
    if a.target_r <= 0 or a.dollars <= 0 or a.max_loss <= 0 or a.max_trades < 1:
        errs.append("--target-r, --dollars, --max-loss and --max-trades must be positive")
    try:
        le, fb = (datetime.time(*(int(x) for x in v.split(":"))) for v in (a.last_entry, a.flat_by))
        if not datetime.time(9, 30) < le < fb <= datetime.time(16, 0):
            errs.append("times must run 9:30 < --last-entry < --flat-by <= 16:00")
    except ValueError:
        errs.append("--last-entry and --flat-by are HH:MM, New York time")
    for e in errs:
        say("gap: " + e)
    if errs:
        return 1
    trend_module()  # yesterday's close comes from Yahoo through trader-agents' trend.py
    st = load(GAP) or {"today": {}, "history": [], "day": None}
    st.update(kind="gap", symbols=syms, range_min=a.range, stop_at=a.stop, target_r=a.target_r,
              dollars=a.dollars, max_loss=a.max_loss, max_trades=a.max_trades, last_entry=a.last_entry,
              flat_by=a.flat_by, min_gap=a.min_gap, max_gap=a.max_gap, status="on")
    if not a.dry_run:
        save(GAP, st)
    say("Gap-and-go on %s: only names that open %g%% or more above yesterday's close%s. Then buy when a 5-minute "
        "bar closes above the first %d minutes' high (until %s), stop %s, target %gR, as one bracket order at "
        "Alpaca. Up to $%g a trade and $%g of risk, %d trades a day, one per name, long only. Everything is "
        "sold by %s." % (
            ", ".join(syms), a.min_gap, " (and at most %g%%)" % a.max_gap if a.max_gap else "", a.range,
            a.last_entry, {"open": "just under the day's open", "mid": "at the range midpoint",
                           "low": "at the range low"}[a.stop], a.target_r, a.dollars, a.max_loss,
            a.max_trades, a.flat_by))
    if a.dry_run:
        say("Preview of a `gap run` now (nothing is saved or sent):")
        for m in orb_step(api, st, et_now(api), True):
            say(m)
    return 0


def gap_run(api, a):
    return day_plan_run(api, a, GAP, "gap-and-go", "gap")


def gap_stop(api, a):
    return day_plan_stop(api, a, GAP, "gap-and-go")


# ---------------------------------------------------------------- strategy 12: first candle rule (5-min FVG break)

FCR = os.path.join(DIR, "fcr.json")
MIN1 = datetime.timedelta(minutes=1)


def _bar(b, sign):
    """A 1-minute bar as floats; for the short side, mirrored (prices negated) so one long-side scan does both."""
    o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
    return {"t": b["t"], "o": o, "h": h, "l": l, "c": c} if sign > 0 else \
        {"t": b["t"], "o": -o, "h": -l, "l": -h, "c": -c}


def fcr_scan(bars, level, sign, max_wait=15):
    """Long-side scan of completed 1-minute bars after 9:35 (mirrored for shorts):
    1. a fair value gap through the level: bars b0, b1, b2 with b0's low at or under the level, b1 closing
       above it, and b2's low above b0's high (the gap is b0.high .. b2.low);
    2. a retest: a later bar trading back into the gap (low at or under its top);
    3. an engulfing candle at or after the retest: up bar after a down bar, closing at or above that bar's
       open and opening at or under its close.
    A close below the gap's bottom, or no engulfing within `max_wait` bars of the retest, drops the setup.
    Returns ("trigger" | "retest" | "gap" | "none", info) in real prices."""
    bs = [_bar(b, sign) for b in bars]
    lvl = level * sign
    setup = None
    for i, b in enumerate(bs):
        if setup is None:
            if i >= 2:
                b0, b1 = bs[i - 2], bs[i - 1]
                if b0["l"] <= lvl < b1["c"] and b["l"] > b0["h"]:
                    setup = {"bottom": b0["h"], "top": b["l"], "at": b1["t"], "retest": None, "low": None}
            continue
        if b["c"] < setup["bottom"]:
            setup = None  # closed through the gap: the break failed
            continue
        if setup["retest"] is None:
            if b["l"] > setup["top"]:
                continue
            setup.update(retest=i, retest_at=b["t"], low=b["l"])
        else:
            setup["low"] = min(setup["low"], b["l"])
        p = bs[i - 1]
        if b["c"] > b["o"] and p["c"] < p["o"] and b["c"] >= p["o"] and b["o"] <= p["c"] + 0.01:
            info = {"gap": sorted([setup["bottom"] * sign, setup["top"] * sign]), "gap_at": setup["at"],
                    "retest_at": setup["retest_at"], "engulf_at": b["t"], "close": b["c"] * sign,
                    "extreme": setup["low"] * sign}
            return "trigger", info
        if i - setup["retest"] >= max_wait:
            setup = None
    if setup is None:
        return "none", {}
    return ("retest" if setup["retest"] is not None else "gap"), {
        "gap": sorted([setup["bottom"] * sign, setup["top"] * sign]), "gap_at": setup["at"]}


def fcr_close(st, sym, t, px, how, dry):
    sign = 1 if t["side"] == "long" else -1
    risk = (t["entry"] - t["stop"]) * sign
    t.update(status="closed", exit=money(px), how=how, r=round((px - t["entry"]) * sign / risk, 2) if risk > 0 else 0,
             pnl=money((px - t["entry"]) * sign * t["qty"]))
    st["history"].append(dict(t, symbol=sym))
    log("fcr_exit", symbol=sym, side=t["side"], qty=t["qty"], entry=t["entry"], exit=money(px), how=how,
        r=t["r"], pnl=t["pnl"], dry_run=dry)
    return "%s: %s %s at about %.2f: %+.2fR, $%+.2f." % (sym, t["side"].upper(), how, px, t["r"], t["pnl"])


def fcr_manage(api, st, sym, t, now, today, flat_by, dry):
    """One open or pending trade: record a fill, a bracket exit, or flatten at the flat-by time."""
    sign = 1 if t["side"] == "long" else -1
    parent = api.order(t["order_id"]) or {}
    if t["status"] == "entering":
        if parent.get("status") == "filled":
            t.update(status="open", entry=money(float(parent["filled_avg_price"])))
        elif parent.get("status") in LIVE and now < flat_by:
            return "%s: waiting for the entry to fill." % sym
        else:
            if parent.get("status") in LIVE:
                api.cancel_order(t["order_id"])
            t["status"] = "canceled"
            return "%s: the entry order ended %s; no trade." % (sym, parent.get("status", "unfilled"))
    pos = api.position(sym)
    qty = abs(int(float(pos["qty"]))) if pos and float(pos["qty"]) * sign > 0 else 0
    if qty <= 0:
        px, how = t["stop"], "CLOSED"
        for leg in parent.get("legs") or []:
            if leg.get("status") == "filled":
                px = float(leg.get("filled_avg_price") or px)
                how = "TARGET HIT" if leg.get("type") == "limit" else "STOPPED OUT"
        return fcr_close(st, sym, t, px, how, dry)
    if now >= flat_by or t["day"] != today:
        for leg in parent.get("legs") or []:
            if leg.get("status") in LIVE:
                api.cancel_order(leg["id"])
                wait_canceled(api, leg["id"], dry)
        if not dry:
            pos = api.position(sym)
            qty = abs(int(float(pos["qty"]))) if pos else 0
            if qty <= 0:
                return "%s: a bracket leg filled first; the next run records it." % sym
        o = wait_fill(api, api.submit_order(symbol=sym, qty=qty, side="sell" if sign > 0 else "buy",
                                            type="market", time_in_force="day"))
        px = float(o.get("filled_avg_price") or api.last_price(sym))
        return fcr_close(st, sym, t, px, "FLAT BY %s" % st["flat_by"] if t["day"] == today
                         else "LEFT OVER FROM %s, CLOSED" % t["day"], dry)
    price = api.last_price(sym)
    return "%s: %s %d @ %.2f, now %.2f (%+.2fR); stop %.2f, target %.2f at Alpaca." % (
        sym, t["side"], qty, t["entry"], price, (price - t["entry"]) * sign / ((t["entry"] - t["stop"]) * sign),
        t["stop"], t["target"])


def fcr_step(api, st, now, dry):
    out, today = [], now.date().isoformat()
    if st.get("day") != today:
        st["today"] = {s: t for s, t in (st.get("today") or {}).items() if t["status"] in ("open", "entering")}
        st["day"] = today
    trades = st["today"]
    open_dt, first_end = at(now, "09:30"), at(now, "09:35")
    flat_by, last_entry = at(now, st["flat_by"]), at(now, st["last_entry"])

    for sym in sorted(trades):
        if trades[sym]["status"] in ("open", "entering"):
            out.append(fcr_manage(api, st, sym, trades[sym], now, today, flat_by, dry))

    if st.get("status") != "on":
        return out
    waiting = [s for s in st["symbols"] if s not in trades]
    if not waiting:
        return out
    if now < first_end + 3 * MIN1:
        out.append("Waiting for the 9:30-9:35 candle to close; the first gap check is at %s."
                   % (first_end + 3 * MIN1).strftime("%H:%M"))
        return out
    if now >= last_entry:
        out.append("Past the last entry time (%s); no new trades today." % st["last_entry"])
        return out
    taken = len(trades)
    busy = other_bot_symbols()
    acct = api.account() or {}
    power = float(acct.get("buying_power") or acct.get("cash") or 0)
    for sym in waiting:
        if taken >= st["max_trades"]:
            out.append("%d trades taken today (the max); no more." % st["max_trades"])
            break
        if sym in busy or api.position(sym):
            out.append("%s: held or traded by another strategy; skipped." % sym)
            continue
        bars = api.bars(sym, open_dt.isoformat(), now.isoformat(), timeframe="1Min")
        first = [b for b in bars if open_dt <= parse_ts(b["t"]) < first_end]
        after = [b for b in bars if parse_ts(b["t"]) >= first_end and parse_ts(b["t"]) + MIN1 <= now]
        if not first:
            out.append("%s: no bars in the first 5 minutes (IEX feed); skipped." % sym)
            continue
        hi, lo = max(float(b["h"]) for b in first), min(float(b["l"]) for b in first)
        found, notes = None, []
        for side, sign, level in (("long", 1, hi), ("short", -1, lo)):
            if side == "short" and st["sides"] != "both":
                continue
            state, info = fcr_scan(after, level, sign)
            if state == "trigger":
                if not found or info["engulf_at"] < found[2]["engulf_at"]:
                    found = (side, sign, info)
            elif state in ("gap", "retest"):
                notes.append("%s gap %.2f-%.2f (%s), %s" % (
                    side, info["gap"][0], info["gap"][1],
                    parse_ts(info["gap_at"]).astimezone(now.tzinfo).strftime("%H:%M"),
                    "waiting for an engulfing candle" if state == "retest" else "waiting for a retest"))
        if not found:
            out.append("%s: first candle %.2f-%.2f; %s." % (sym, lo, hi, "; ".join(notes) or "no gap through it yet"))
            continue
        side, sign, info = found
        eng_t = parse_ts(info["engulf_at"]).astimezone(now.tzinfo)
        eng_at = eng_t.strftime("%H:%M")
        if eng_t + MIN1 < now - 3 * MIN1:
            out.append("%s: %s setup's engulfing candle was at %s, more than 3 minutes ago; missed, no entry today."
                       % (sym, side, eng_at))
            trades[sym] = {"day": today, "side": side, "status": "missed", "engulf": eng_at}
            continue
        price = api.last_price(sym)
        stop = money(info["extreme"] - 0.01 * sign)
        risk = (price - stop) * sign
        if risk <= max(0.02, price * 0.0005):
            out.append("%s: %s setup at %s, but %.2f is at or past the stop %.2f; no entry." % (
                sym, side, eng_at, price, stop))
            continue
        if (price - info["close"]) * sign > 0.5 * (info["close"] - stop) * sign:
            out.append("%s: %s setup at %s, but %.2f has run more than half a risk unit past the engulfing "
                       "close %.2f; not chasing." % (sym, side, eng_at, price, info["close"]))
            continue
        if side == "short":
            asset = api.asset(sym) or {}
            if not (asset.get("shortable") and asset.get("easy_to_borrow")) or acct.get("shorting_enabled") is False:
                out.append("%s: short setup at %s, but Alpaca won't short it now (not easy to borrow, or "
                           "shorting off); skipped." % (sym, eng_at))
                continue
        qty = min(int(st["dollars"] // price), int(st["max_loss"] // risk))
        if qty < 1:
            out.append("%s: %s setup, but $%g of risk with a %.2f stop buys no whole share." % (
                sym, side, st["max_loss"], risk))
            continue
        if qty * price > power:
            out.append("%s: %s setup, but %d shares need $%.0f and buying power is $%.0f." % (
                sym, side, qty, qty * price, power))
            continue
        target = money(price + st["target_r"] * risk * sign)
        o = wait_fill(api, api.submit_order(
            symbol=sym, qty=qty, side="buy" if sign > 0 else "sell", type="market", time_in_force="gtc",
            order_class="bracket", take_profit={"limit_price": "%.2f" % target},
            stop_loss={"stop_price": "%.2f" % stop}))
        filled = o.get("status") in ("filled", "dry_run")
        entry = money(float(o.get("filled_avg_price") or price))
        trades[sym] = {"day": today, "side": side, "qty": qty, "entry": entry, "stop": stop, "target": target,
                       "order_id": o.get("id"), "first": [money(lo), money(hi)], "gap": [money(x) for x in info["gap"]],
                       "engulf": eng_at, "status": "open" if filled else "entering"}
        taken, power = taken + 1, power - qty * entry
        log("fcr_entry", symbol=sym, side=side, qty=qty, entry=entry, stop=stop, target=target,
            first_high=money(hi), first_low=money(lo), dry_run=dry)
        out.append("%s: FIRST CANDLE %s (candle %.2f-%.2f; gap %.2f-%.2f at %s, retested, engulfing at %s) -> "
                   "%s %d at about %.2f; stop %.2f, target %.2f (%gR) held at Alpaca; risk $%.0f; flat by %s." % (
                       sym, "BREAK UP" if sign > 0 else "BREAK DOWN", lo, hi, info["gap"][0], info["gap"][1],
                       parse_ts(info["gap_at"]).astimezone(now.tzinfo).strftime("%H:%M"), eng_at,
                       ("WOULD " if dry else "") + ("BUY" if sign > 0 else "SHORT"), qty, entry, stop, target,
                       st["target_r"], qty * risk, st["flat_by"]))
    return out


def fcr_start(api, a):
    syms = sorted({s.upper() for s in a.symbols})
    errs = []
    if a.target_r <= 0 or a.dollars <= 0 or a.max_loss <= 0 or a.max_trades < 1:
        errs.append("--target-r, --dollars, --max-loss and --max-trades must be positive")
    try:
        le, fb = (datetime.time(*(int(x) for x in v.split(":"))) for v in (a.last_entry, a.flat_by))
        if not datetime.time(9, 40) <= le < fb <= datetime.time(16, 0):
            errs.append("times must run 9:40 <= --last-entry < --flat-by <= 16:00")
    except ValueError:
        errs.append("--last-entry and --flat-by are HH:MM, New York time")
    for e in errs:
        say("fcr: " + e)
    if errs:
        return 1
    st = load(FCR) or {"today": {}, "history": [], "day": None}
    st.update(kind="fcr", symbols=syms, sides=a.sides, target_r=a.target_r, dollars=a.dollars,
              max_loss=a.max_loss, max_trades=a.max_trades, last_entry=a.last_entry, flat_by=a.flat_by, status="on")
    if not a.dry_run:
        save(FCR, st)
    say("First Candle Rule on %s: mark the high and low of the 9:30-9:35 candle; on 1-minute bars wait for a "
        "break that leaves a fair value gap, a retest of that gap and an engulfing candle, then %s at market "
        "(until %s), stop just past the retest's extreme, target %gR, as one bracket order at Alpaca. Up to $%g "
        "a trade and $%g of risk, %d trades a day, one per name. Everything is closed by %s." % (
            ", ".join(syms), "buy (break up) or short (break down)" if a.sides == "both" else "buy (break up only)",
            a.last_entry, a.target_r, a.dollars, a.max_loss, a.max_trades, a.flat_by))
    if a.dry_run:
        say("Preview of a `fcr run` now (nothing is saved or sent):")
        for m in fcr_step(api, st, et_now(api), True):
            say(m)
    return 0


def fcr_run(api, a):
    st = load(FCR)
    if not st or (st.get("status") != "on" and not any(
            t["status"] in ("open", "entering") for t in (st.get("today") or {}).values())):
        say("No First Candle Rule plan. Start one with `fcr start SYMBOL ...`.")
        return 0
    if not market_open(api):
        return 0
    for m in fcr_step(api, st, et_now(api), a.dry_run):
        say(m)
    if not a.dry_run:
        save(FCR, st)
    return 0


def fcr_stop(api, a):
    st = load(FCR)
    if not st:
        say("No First Candle Rule plan.")
        return 0
    st["status"] = "off"
    if not a.dry_run:
        save(FCR, st)
    say("First Candle Rule: no new trades. Anything open keeps its bracket and is still closed by %s." % st["flat_by"])
    return 0


# ---------------------------------------------------------------- check / status

def check(api, a):
    acct = api.account()
    c = api.clock() or {}
    say("Alpaca PAPER account %s: status %s, cash $%s, equity $%s, options level %s."
        % (acct.get("account_number"), acct.get("status"), acct.get("cash"), acct.get("equity"),
           acct.get("options_trading_level", "?")))
    say("Market is %s (next open %s, next close %s)." % ("OPEN" if c.get("is_open") else "closed",
                                                         c.get("next_open"), c.get("next_close")))
    return 0


def test_buy(api, a):
    if not market_open(api):
        return 1
    o = wait_fill(api, api.submit_order(symbol=a.symbol.upper(), qty=a.qty, side="buy",
                                        type="market", time_in_force="day"))
    say("Test order: buy %d %s, status %s, price %s. Check it in the Alpaca paper dashboard."
        % (a.qty, a.symbol.upper(), o.get("status"), o.get("filled_avg_price")))
    log("test_buy", symbol=a.symbol.upper(), qty=a.qty, status=o.get("status"))
    return 0


def status(api, a):
    check(api, a)
    if os.path.exists(BANKROLL):
        bankroll_show(api, a)
    for p in api.positions():
        say("  %-22s %8s @ %-9s now %-9s P/L %s" % (p["symbol"], p["qty"], p["avg_entry_price"],
                                                    p.get("current_price"), p.get("unrealized_pl")))
    for f in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
        if not f.endswith(".json"):  # skip log.jsonl and leftover .tmp files
            continue
        st = load(os.path.join(DIR, f))
        if f.startswith("trail-"):
            say("trail %s: %s, %d shares, entry %.2f, high %.2f, stop %.2f" % (
                st["symbol"], st["status"], st["qty"], st["entry"], st["hwm"], st["floor"]))
        elif f.startswith("wheel-"):
            op = st.get("open")
            say("wheel %s: premiums so far $%.2f, cost basis %s, open: %s, cycles done %d" % (
                st["symbol"], st["premium_total"], st.get("cost_basis"),
                "%s %s (%s)" % (op["kind"], op["symbol"], op["status"]) if op else "nothing",
                len(st["cycles"])))
        elif f.startswith("cc-"):
            op = st.get("open")
            say("cc %s: %s; premiums so far $%.2f, cost basis %s, open: %s, called away %d time(s)" % (
                st["symbol"], st.get("status"), st["premium_total"], st.get("cost_basis"),
                "call %s %.2f exp %s (%s)" % (op["symbol"], op["strike"], op["expiration"], op["status"])
                if op else "nothing", len(st["cycles"])))
        elif f.startswith("collar-"):
            op = st.get("open")
            say("collar %s: %s; %s; net premiums $%+.2f, collars finished %d" % (
                st["symbol"], st.get("status"),
                "put %.2f / call %.2f exp %s (put %s, call %s)" % (
                    op["put"]["strike"], op["call"]["strike"], op["expiration"], op["put"]["status"],
                    op["call"]["status"]) if op else "no collar on", st["net_total"], len(st["cycles"])))
        elif f == "copy.json":
            say("copy: %d trades copied so far" % len(st.get("trades", [])))
        elif f == "swing.json":
            closed = st.get("closed", [])
            say("swing: %s, watching %s; open: %s; closed trades %d%s" % (
                st.get("status"), ", ".join(st.get("symbols", [])),
                ", ".join("%s %d @ %.2f" % (s, p["qty"], p["entry"]) for s, p in sorted(st["positions"].items()))
                or "nothing", len(closed),
                " (avg %+.1f%%)" % (sum(c["ret_pct"] for c in closed) / len(closed)) if closed else ""))
        elif f == "rebal.json":
            total, rows = rebal_book(api, st)
            say("rebal: %s, %s; worth $%.2f%s; next check %s" % (
                st.get("status"), ", ".join("%s %.1f%% (target %.0f%%)" % (s, r[3] * 100, st["weights"][s] * 100)
                                            for s, r in rows.items()),
                total, " (%+.1f%% on $%g)" % ((total / st["dollars"] - 1) * 100, st["dollars"])
                if st.get("funded") else ", not funded yet", st.get("next")))
        elif f == "rotate.json":
            held, rows = rot_value(api, st)
            total = held + st["cash"]
            say("rotate: %s, top %d of %s; holds %s; sleeve cash $%.2f; worth $%.2f (%+.1f%% on $%g); next %s" % (
                st.get("status"), st["top"], ", ".join(st["symbols"]),
                ", ".join("%s $%.2f" % (s, r[2]) for s, r in sorted(rows.items())) or "nothing",
                st["cash"], total, (total / st["dollars"] - 1) * 100, st["dollars"],
                st.get("next") if st.get("status") == "on" else "none"))
        elif f == "dca.json":
            cost, value, held = dca_value(api, st)
            say("dca: %s, $%g %s into %s; next buy %s; invested $%.2f, worth $%.2f%s" % (
                st.get("status"), st["dollars"], st["every"],
                ", ".join("%s %.0f%%" % (s, w * 100) for s, w in st["weights"].items()),
                st.get("next") if st.get("status") == "on" else "none", cost, value,
                " (%+.1f%%)" % ((value / cost - 1) * 100) if cost else ""))
        elif f == "bounce.json":
            closed = st.get("closed", [])
            say("bounce: %s, watching %s; open: %s; closed trades %d%s" % (
                st.get("status"), ", ".join(st.get("symbols", [])),
                ", ".join("%s %d @ %.2f" % (s, p["qty"], p["entry"]) for s, p in sorted(st["positions"].items()))
                or "nothing", len(closed),
                " (avg %+.1f%%)" % (sum(c["ret_pct"] for c in closed) / len(closed)) if closed else ""))
        elif f in ("orb.json", "gap.json", "fcr.json"):
            hist = st.get("history", [])
            say(f[:-5] + ": %s, watching %s; today (%s): %s; past trades %d%s" % (
                st.get("status"), ", ".join(st.get("symbols", [])), st.get("day"),
                ", ".join("%s %s" % (s, t["status"]) for s, t in sorted((st.get("today") or {}).items()))
                or "none", len(hist),
                ", total $%+.2f, avg %+.2fR" % (sum(t["pnl"] for t in hist), sum(t["r"] for t in hist) / len(hist))
                if hist else ""))
    return 0


# ---------------------------------------------------------------- cli

def parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print orders instead of sending them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    p = sub.add_parser("test-buy")
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, default=1)

    t = sub.add_parser("trail").add_subparsers(dest="sub", required=True)
    p = t.add_parser("start")
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--stop", type=float, default=10, help="stop-loss, %% below entry (default 10)")
    p.add_argument("--trigger", type=float, default=10, help="start trailing once up this %% (default 10)")
    p.add_argument("--trail", type=float, default=5, help="trail this %% below the high (default 5)")
    p.add_argument("--ladder", default="", help="extra buys, DROP%%:QTY,... e.g. 5:5,8:10")
    t.add_parser("run")

    c = sub.add_parser("copy").add_subparsers(dest="sub", required=True)
    p = c.add_parser("run")
    p.add_argument("--member", action="append", default=[], help="House member name, e.g. mccaul")
    p.add_argument("--dollars", type=float, default=1000, help="dollars per copied buy (default 1000)")
    p.add_argument("--max-orders", type=int, default=5, help="orders per run at most (default 5)")
    p.add_argument("--max-age", type=int, default=60, help="skip trades older than this many days")
    p.add_argument("--ptr", help="path to trader-pelosi/ptr.py")
    p.add_argument("--from-json", help="use saved ptr.py JSON instead of fetching")

    w = sub.add_parser("wheel").add_subparsers(dest="sub", required=True)
    p = w.add_parser("start")
    p.add_argument("symbol")
    p.add_argument("--put-otm", type=float, default=10, help="put strike %% below price (default 10)")
    p.add_argument("--call-over", type=float, default=10, help="call strike %% above cost (default 10)")
    p.add_argument("--dte", default="14-28", help="days to expiration window (default 14-28)")
    p.add_argument("--take-profit", type=float, default=50, help="buy back at this %% profit (default 50)")
    p.add_argument("--contracts", type=int, default=1)
    w.add_parser("run")

    s = sub.add_parser("swing").add_subparsers(dest="sub", required=True)
    p = s.add_parser("start")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--dollars", type=float, default=2000, help="dollars per position, whole shares (default 2000)")
    p.add_argument("--stop", type=float, default=10, help="stop-loss, %% below entry (default 10)")
    p.add_argument("--hold-days", type=int, default=91, help="sell after this many days (default 91, ~3 months)")
    p.add_argument("--max-positions", type=int, default=5, help="open swing positions at most (default 5)")
    p.add_argument("--cooldown", type=int, default=21, help="days before rebuying a name it closed (default 21)")
    s.add_parser("run")
    s.add_parser("stop")

    o = sub.add_parser("orb").add_subparsers(dest="sub", required=True)
    p = o.add_parser("start")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--range", type=int, default=15, help="opening range, minutes (default 15)")
    p.add_argument("--stop", choices=("mid", "low"), default="mid", help="stop at the range midpoint or low")
    p.add_argument("--target-r", type=float, default=2, help="target, in multiples of the risk (default 2)")
    p.add_argument("--dollars", type=float, default=2000, help="most dollars in one trade (default 2000)")
    p.add_argument("--max-loss", type=float, default=100, help="most dollars lost at the stop (default 100)")
    p.add_argument("--max-trades", type=int, default=3, help="trades a day at most (default 3)")
    p.add_argument("--last-entry", default="11:30", help="no new entries from this time, ET (default 11:30)")
    p.add_argument("--flat-by", default="15:55", help="sell everything by this time, ET (default 15:55)")
    o.add_parser("run")
    o.add_parser("stop")

    b = sub.add_parser("bounce").add_subparsers(dest="sub", required=True)
    p = b.add_parser("start")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--dollars", type=float, default=2000, help="dollars per position, whole shares (default 2000)")
    p.add_argument("--stop", type=float, default=5, help="stop-loss, %% below entry (default 5)")
    p.add_argument("--hold-days", type=int, default=14, help="sell after this many days (default 14, ~10 sessions)")
    p.add_argument("--max-positions", type=int, default=5, help="open bounce positions at most (default 5)")
    p.add_argument("--cooldown", type=int, default=10, help="days before rebuying a name it closed (default 10)")
    p.add_argument("--above-200", action="store_true",
                   help="only buy bounces while the close is above the 200-day average")
    b.add_parser("run")
    b.add_parser("stop")

    d = sub.add_parser("dca").add_subparsers(dest="sub", required=True)
    p = d.add_parser("start")
    p.add_argument("symbols", nargs="+", help="SYMBOL or SYMBOL:WEIGHT, e.g. SPY:60 QQQ:40")
    p.add_argument("--dollars", type=float, default=100, help="dollars per buy day, split by weight (default 100)")
    p.add_argument("--every", choices=("weekly", "biweekly", "monthly"), default="weekly")
    p.add_argument("--day", default="mon", help="mon..fri for weekly/biweekly, 1-28 for monthly (default mon)")
    p.add_argument("--cap", type=float, help="stop after this many dollars in total")
    d.add_parser("run")
    d.add_parser("stop")

    r = sub.add_parser("rebal").add_subparsers(dest="sub", required=True)
    p = r.add_parser("start")
    p.add_argument("symbols", nargs="+", help="SYMBOL:WEIGHT, e.g. VTI:60 BND:40")
    p.add_argument("--dollars", type=float, default=10000, help="dollars to put in at the start (default 10000)")
    p.add_argument("--every", choices=("monthly", "quarterly"), default="monthly")
    p.add_argument("--day", default="1", help="day of the month to check, 1-28 (default 1)")
    p.add_argument("--band", type=float, default=5, help="rebalance only if a name is this many points off (default 5)")
    p.add_argument("--min-trade", type=float, default=25, help="skip trades smaller than this many dollars (default 25)")
    r.add_parser("run")
    r.add_parser("stop")

    m = sub.add_parser("rotate").add_subparsers(dest="sub", required=True)
    p = m.add_parser("start")
    p.add_argument("symbols", nargs="+", help="the names to rotate between, e.g. XLK XLF XLE XLV")
    p.add_argument("--top", type=int, default=2, help="how many of the leaders to hold (default 2)")
    p.add_argument("--lookback", type=int, default=126, help="sessions of return to rank by (default 126, ~6 months)")
    p.add_argument("--skip", type=int, default=0, help="leave out the last N sessions (default 0; 21 skips a month)")
    p.add_argument("--min-mom", type=float, default=0,
                   help="hold a name only if its return is above this %% (default 0; else that slot sits in cash)")
    p.add_argument("--dollars", type=float, default=10000, help="dollars for the rotation (default 10000)")
    p.add_argument("--every", choices=("weekly", "monthly"), default="monthly")
    p.add_argument("--day", default="1", help="1-28 for monthly, mon..fri for weekly (default 1)")
    p.add_argument("--min-trade", type=float, default=25, help="skip buys smaller than this many dollars (default 25)")
    m.add_parser("run")
    m.add_parser("stop")

    f = sub.add_parser("fcr").add_subparsers(dest="sub", required=True)
    p = f.add_parser("start")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--sides", choices=("both", "long"), default="both",
                   help="trade breaks both ways (default; shorts on a break down) or long only")
    p.add_argument("--target-r", type=float, default=3, help="target, in multiples of the risk (default 3)")
    p.add_argument("--dollars", type=float, default=2000, help="most dollars in one trade (default 2000)")
    p.add_argument("--max-loss", type=float, default=100, help="most dollars lost at the stop (default 100)")
    p.add_argument("--max-trades", type=int, default=3, help="trades a day at most (default 3)")
    p.add_argument("--last-entry", default="11:30", help="no new entries from this time, ET (default 11:30)")
    p.add_argument("--flat-by", default="15:55", help="close everything by this time, ET (default 15:55)")
    f.add_parser("run")
    f.add_parser("stop")

    g = sub.add_parser("gap").add_subparsers(dest="sub", required=True)
    p = g.add_parser("start")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--min-gap", type=float, default=3, help="open at least this %% above yesterday's close (default 3)")
    p.add_argument("--max-gap", type=float, help="and at most this %% (no limit by default)")
    p.add_argument("--range", type=int, default=5, help="first minutes whose high must break (default 5)")
    p.add_argument("--stop", choices=("open", "mid", "low"), default="open",
                   help="stop just under the day's open (default), or the range midpoint or low")
    p.add_argument("--target-r", type=float, default=2, help="target, in multiples of the risk (default 2)")
    p.add_argument("--dollars", type=float, default=2000, help="most dollars in one trade (default 2000)")
    p.add_argument("--max-loss", type=float, default=100, help="most dollars lost at the stop (default 100)")
    p.add_argument("--max-trades", type=int, default=3, help="trades a day at most (default 3)")
    p.add_argument("--last-entry", default="10:30", help="no new entries from this time, ET (default 10:30)")
    p.add_argument("--flat-by", default="15:55", help="sell everything by this time, ET (default 15:55)")
    g.add_parser("run")
    g.add_parser("stop")

    p = sub.add_parser("backtest", help="test the day-trade rules and candle setups on past 1-minute bars")
    p.add_argument("strategies", nargs="+", choices=("orb", "gap", "fcr", "candles", "all"))
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--days", type=int, default=250, help="sessions to test (default 250, about a year)")
    p.add_argument("--slip", type=float, default=0.02, help="dollars per share lost on each market fill (default 0.02)")
    p.add_argument("--dollars", type=float, default=500, help="most dollars in one trade (default 500)")
    p.add_argument("--max-loss", type=float, default=5, help="most dollars lost at the stop (default 5)")
    p.add_argument("--flat-by", default="15:55")
    p.add_argument("--range", type=int, default=15, help="ORB opening range, minutes (default 15)")
    p.add_argument("--orb-r", type=float, default=2, help="ORB and gap target, R (default 2)")
    p.add_argument("--min-gap", type=float, default=3, help="gap-and-go: open this %% over yesterday (default 3)")
    p.add_argument("--fcr-r", type=float, default=3, help="First Candle target, R (default 3)")
    p.add_argument("--sides", choices=("long", "both"), default="long", help="First Candle sides (default long)")
    p.add_argument("--candle-r", type=float, default=2, help="candle setups target, R (default 2)")
    p.add_argument("--candle-last", default="15:00", help="candle setups: last entry, ET (default 15:00)")

    br = sub.add_parser("bankroll").add_subparsers(dest="sub", required=True)
    p = br.add_parser("set")
    p.add_argument("stake", type=float, help="most the bots here may have in play, plus their profits")
    br.add_parser("show")
    br.add_parser("off")

    nt = sub.add_parser("notify").add_subparsers(dest="sub", required=True)
    p = nt.add_parser("setup")
    p.add_argument("--phone", action="store_true", help="also push to your phone with the ntfy app")
    p.add_argument("--no-windows", action="store_true", help="no Windows pop-ups")
    nt.add_parser("test")
    nt.add_parser("off")

    k = sub.add_parser("collar").add_subparsers(dest="sub", required=True)
    p = k.add_parser("start")
    p.add_argument("symbol")
    p.add_argument("--contracts", type=int, default=1, help="collars, 100 shares each (default 1)")
    p.add_argument("--put-otm", type=float, default=10, help="put strike at least this %% below the price (default 10)")
    p.add_argument("--call-otm", type=float, default=10, help="call strike at least this %% above the price (default 10)")
    p.add_argument("--over-cost", type=float, default=0, help="and the call at least this %% above your cost (default 0)")
    p.add_argument("--dte", default="30-60", help="days to expiration window (default 30-60)")
    p.add_argument("--max-debit", type=float, default=0.5,
                   help="most the put may cost beyond the call's premium, per share (default 0.50; 0 = zero-cost)")
    p.add_argument("--buy-shares", action="store_true", help="buy the missing shares at market first")
    k.add_parser("run")
    p = k.add_parser("stop")
    p.add_argument("symbol")

    c = sub.add_parser("cc").add_subparsers(dest="sub", required=True)
    p = c.add_parser("start")
    p.add_argument("symbol")
    p.add_argument("--contracts", type=int, default=1, help="calls to sell, 100 shares each (default 1)")
    p.add_argument("--otm", type=float, default=10, help="strike at least this %% above today's price (default 10)")
    p.add_argument("--over-cost", type=float, default=0, help="and this %% above your cost (default 0)")
    p.add_argument("--dte", default="14-28", help="days to expiration window (default 14-28)")
    p.add_argument("--take-profit", type=float, default=50, help="buy back at this %% profit (default 50)")
    p.add_argument("--buy-shares", action="store_true", help="buy the missing shares at market first")
    c.add_parser("run")
    p = c.add_parser("stop")
    p.add_argument("symbol")
    return ap


def main(argv=None, api=None):
    a = parser().parse_args(argv)
    try:
        api = api or alp.Alpaca()
    except alp.AlpacaError as e:
        say("bot: %s" % e)
        return 2
    cfg = load(BANKROLL) if os.path.exists(BANKROLL) else None
    if cfg:
        api = Capped(api, cfg)
    if a.dry_run:
        api = DryRun(api)
    fn = {("check", None): check, ("status", None): status, ("test-buy", None): test_buy,
          ("trail", "start"): trail_start, ("trail", "run"): trail_run, ("copy", "run"): copy_run,
          ("wheel", "start"): wheel_start, ("wheel", "run"): wheel_run,
          ("swing", "start"): swing_start, ("swing", "run"): swing_run, ("swing", "stop"): swing_stop,
          ("orb", "start"): orb_start, ("orb", "run"): orb_run, ("orb", "stop"): orb_stop,
          ("bounce", "start"): bounce_start, ("bounce", "run"): bounce_run, ("bounce", "stop"): bounce_stop,
          ("dca", "start"): dca_start, ("dca", "run"): dca_run, ("dca", "stop"): dca_stop,
          ("cc", "start"): cc_start, ("cc", "run"): cc_run, ("cc", "stop"): cc_stop,
          ("rebal", "start"): rebal_start, ("rebal", "run"): rebal_run, ("rebal", "stop"): rebal_stop,
          ("gap", "start"): gap_start, ("gap", "run"): gap_run, ("gap", "stop"): gap_stop,
          ("rotate", "start"): rot_start, ("rotate", "run"): rot_run, ("rotate", "stop"): rot_stop,
          ("fcr", "start"): fcr_start, ("fcr", "run"): fcr_run, ("fcr", "stop"): fcr_stop,
          ("collar", "start"): collar_start, ("collar", "run"): collar_run, ("collar", "stop"): collar_stop,
          ("notify", "setup"): notify_setup, ("notify", "test"): notify_test, ("notify", "off"): notify_off,
          ("backtest", None): backtest,
          ("bankroll", "set"): bankroll_set, ("bankroll", "show"): bankroll_show, ("bankroll", "off"): bankroll_off}[(a.cmd, getattr(a, "sub", None))]
    try:
        return fn(api, a)
    except alp.AlpacaError as e:
        say("bot: %s" % e)
        log("error", message=str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
