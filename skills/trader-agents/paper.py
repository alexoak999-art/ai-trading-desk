#!/usr/bin/env python3
"""Paper-trading journal for the agent desk. No broker, no real money.

Every trade is written to trader/paper.json with the date, the price, your
reason and the agents' verdicts at that moment, so later you can see whether
the process works -- the point of paper trading, per the reel.

  python paper.py buy AAPL --qty 10 --reason "SETUP: ROC reset, earnings beat"
  python paper.py buy AAPL --qty 10 --price 271.50
  python paper.py sell AAPL                     # the whole position
  python paper.py show                          # marked to the last close
  python paper.py journal

Prices default to Yahoo's latest regular-market price (free, can lag).
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trend  # noqa: E402  (same folder)

BOOK = "trader/paper.json"


def load(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"cash_start": 10000.0, "trades": []}


def save(book, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(book, fh, indent=1)


def last_price(sym):
    data = trend.http_json("/v8/finance/chart/%s?range=5d&interval=1d" % sym.replace(".", "-"))
    meta = data["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


def positions(book):
    pos = {}
    for t in book["trades"]:
        p = pos.setdefault(t["symbol"], {"qty": 0, "cost": 0.0, "realized": 0.0, "opened": None})
        if t["side"] == "buy":
            if p["qty"] == 0:
                p["opened"] = t["date"]
            p["qty"] += t["qty"]
            p["cost"] += t["qty"] * t["price"]
        else:
            avg = p["cost"] / p["qty"]
            p["realized"] += t["qty"] * (t["price"] - avg)
            p["cost"] -= t["qty"] * avg
            p["qty"] -= t["qty"]
    return pos


def verdicts_now(sym):
    path = "trader/agents/technical.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for it in json.load(fh).get("names", []):
                if it["symbol"] == sym:
                    return {"technical": it.get("verdict"), "technical_why": it.get("why")}
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["buy", "sell", "show", "journal"])
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--qty", type=int)
    ap.add_argument("--price", type=float, help="default: Yahoo's latest price")
    ap.add_argument("--reason", default="")
    ap.add_argument("--book", default=BOOK)
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    a = ap.parse_args(argv)
    book = load(a.book)
    today = a.date or datetime.date.today().isoformat()

    if a.action in ("buy", "sell"):
        if not a.symbol:
            print("paper.py: which symbol?")
            return 1
        sym = a.symbol.upper()
        held = positions(book).get(sym, {}).get("qty", 0)
        qty = a.qty if a.qty is not None else (held if a.action == "sell" else None)
        if not qty or qty <= 0:
            print("paper.py: give --qty (a whole number of shares)." if a.action == "buy"
                  else "paper.py: no paper position in %s." % sym)
            return 1
        if a.action == "sell" and qty > held:
            print("paper.py: you hold %d %s on paper, not %d. No short sales." % (held, sym, qty))
            return 1
        price = a.price if a.price is not None else last_price(sym)
        t = {"date": today, "side": a.action, "symbol": sym, "qty": qty,
             "price": round(price, 2), "reason": a.reason}
        v = verdicts_now(sym)
        if v:
            t["agents"] = v
        book["trades"].append(t)
        save(book, a.book)
        print("PAPER %s %d %s at %.2f on %s. Recorded in %s. No real order was sent."
              % (a.action.upper(), qty, sym, price, today, a.book))
        return 0

    if a.action == "journal":
        for t in book["trades"]:
            print("%s  %-4s %4d %-6s @ %9.2f  %s" % (t["date"], t["side"], t["qty"], t["symbol"],
                                                     t["price"], t["reason"]))
        if not book["trades"]:
            print("No paper trades yet.")
        return 0

    pos = positions(book)
    cash = book["cash_start"]
    for t in book["trades"]:
        cash += (-1 if t["side"] == "buy" else 1) * t["qty"] * t["price"]
    print("Paper account (started with $%.2f). Prices: Yahoo, latest, can lag." % book["cash_start"])
    value, realized = cash, 0.0
    for sym, p in sorted(pos.items()):
        realized += p["realized"]
        if p["qty"] == 0:
            continue
        try:
            px = last_price(sym)
        except Exception as e:
            print("  %-6s %d shares, price unavailable (%s)" % (sym, p["qty"], e))
            value += p["cost"]
            continue
        avg = p["cost"] / p["qty"]
        days = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(p["opened"])).days
        value += p["qty"] * px
        print("  %-6s %4d sh  avg %9.2f  now %9.2f  %+7.2f%%  open %d days (horizon ~91)"
              % (sym, p["qty"], avg, px, (px / avg - 1) * 100, days))
    print("  cash $%.2f   account $%.2f (%+.2f%%)   closed trades: $%+.2f"
          % (cash, value, (value / book["cash_start"] - 1) * 100, realized))
    return 0


if __name__ == "__main__":
    sys.exit(main())
