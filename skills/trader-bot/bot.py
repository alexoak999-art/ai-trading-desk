#!/usr/bin/env python3
"""Paper-trading bot on Alpaca: trailing stop with ladder buys, copying Congress
trades, and the options wheel. PAPER ACCOUNT ONLY.

  python bot.py check                                   # keys work? paper? market open?
  python bot.py test-buy AAPL                           # the connection test: buy 1 share
  python bot.py trail start TSLA --qty 10 --stop 10 --trigger 10 --trail 5 --ladder 5:5,8:10
  python bot.py trail run                               # every 5 min in market hours
  python bot.py copy run --member mccaul --dollars 1000 # each morning / hourly
  python bot.py wheel start TSLA --put-otm 10 --call-over 10 --dte 14-28 --take-profit 50
  python bot.py wheel run                               # every 15 min in market hours
  python bot.py status                                  # account + every strategy
  add --dry-run to any command to print what it would send without sending it.

State and a log of every order live in trader/bot/. Each `run` does nothing
when the market is closed. Nothing here trades real money.
"""
import argparse
import datetime
import json
import os
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
        for lv in st["ladder"]:
            if not lv["done"] and price <= st["entry"] * (1 - lv["drop_pct"] / 100):
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
    return os.environ.get("TRADER_PELOSI_PTR") or os.path.expanduser("~/.claude/skills/trader-pelosi/ptr.py")


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
            say("copy: the Pelosi tracker is not installed (%s). Install the Day Trader pack." % ptr)
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
            elif op["kind"] == "call" and shares <= op["shares_before"] - 100 * n:
                gain = (op["strike"] - (st["cost_basis"] or op["strike"])) * 100 * n
                st["cycles"].append({"ended": today.isoformat(), "stock_gain": money(gain),
                                     "premium_total": st["premium_total"]})
                st["history"].append({"date": today.isoformat(), "event": "called away",
                                      "strike": op["strike"], "stock_gain": money(gain)})
                st["cost_basis"] = None
                msg = "CALLED AWAY at %.2f: stock gain %.2f. Back to selling puts." % (op["strike"], gain)
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
    lo = (today + datetime.timedelta(days=st["dte_min"])).isoformat()
    hi = (today + datetime.timedelta(days=st["dte_max"])).isoformat()
    if shares >= 100 * n:
        if st.get("cost_basis") is None:
            st["cost_basis"] = money(float(pos["avg_entry_price"]))
        target = st["cost_basis"] * (1 + st["call_over"] / 100)
        kind, strike_lo, strike_hi = "call", max(target, st["cost_basis"]), None
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
    log("wheel_sell", kind=kind, **pick)
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
    for p in api.positions():
        say("  %-22s %8s @ %-9s now %-9s P/L %s" % (p["symbol"], p["qty"], p["avg_entry_price"],
                                                    p.get("current_price"), p.get("unrealized_pl")))
    for f in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
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
        elif f == "copy.json":
            say("copy: %d trades copied so far" % len(st.get("trades", [])))
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
    return ap


def main(argv=None, api=None):
    a = parser().parse_args(argv)
    try:
        api = api or alp.Alpaca()
    except alp.AlpacaError as e:
        say("bot: %s" % e)
        return 2
    if a.dry_run:
        api = DryRun(api)
    fn = {("check", None): check, ("status", None): status, ("test-buy", None): test_buy,
          ("trail", "start"): trail_start, ("trail", "run"): trail_run, ("copy", "run"): copy_run,
          ("wheel", "start"): wheel_start, ("wheel", "run"): wheel_run}[(a.cmd, getattr(a, "sub", None))]
    try:
        return fn(api, a)
    except alp.AlpacaError as e:
        say("bot: %s" % e)
        log("error", message=str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
