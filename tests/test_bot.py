import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "trader-bot"))
import alpaca  # noqa: E402
import bot  # noqa: E402

TODAY = datetime.date.today()


class FakeAlpaca:
    """A tiny in-memory paper broker: market orders fill at the current price,
    stop orders fill when trigger_stops() sees the price at or under them,
    option limit orders fill at their limit."""

    def __init__(self, cash=50000.0):
        self.is_open, self.cash, self.prices = True, cash, {}
        self.pos, self.orders, self.sent = {}, {}, []
        self.contracts, self.quotes, self._n = [], {}, 0

    # account
    def account(self):
        return {"account_number": "PA-TEST", "status": "ACTIVE", "cash": str(self.cash),
                "equity": str(self.cash), "options_trading_level": 3}

    def clock(self):
        return {"is_open": self.is_open, "next_open": "tomorrow", "next_close": "16:00"}

    def positions(self):
        return [dict(p, symbol=s) for s, p in self.pos.items() if float(p["qty"]) != 0]

    def position(self, s):
        p = self.pos.get(s)
        return dict(p, symbol=s) if p and float(p["qty"]) != 0 else None

    def order(self, oid):
        return self.orders.get(oid)

    def last_price(self, s):
        return self.prices[s]

    def option_contracts(self, u, kind, lo, hi, kmin=None, kmax=None):
        return [c for c in self.contracts if c["type"] == kind and lo <= c["expiration_date"] <= hi
                and (kmin is None or float(c["strike_price"]) >= kmin)
                and (kmax is None or float(c["strike_price"]) <= kmax)]

    def option_quotes(self, syms):
        return {s: self.quotes[s] for s in syms if s in self.quotes}

    # orders
    def _id(self):
        self._n += 1
        return "o%d" % self._n

    def _fill(self, s, qty, price, cls="us_equity"):
        p = self.pos.setdefault(s, {"qty": "0", "avg_entry_price": "0", "asset_class": cls})
        q0, a0 = float(p["qty"]), float(p["avg_entry_price"])
        q1 = q0 + qty
        if q1 and (q0 == 0 or (q0 > 0) == (qty > 0)):
            p["avg_entry_price"] = str((q0 * a0 + qty * price) / q1)
        p["qty"] = str(q1)
        mult = 100 if cls == "us_option" else 1
        self.cash -= qty * price * mult

    def submit_order(self, **o):
        self.sent.append(o)
        o = dict(o, id=self._id(), status="new")
        s, side = o["symbol"], o["side"]
        sign = 1 if side == "buy" else -1
        if o["type"] == "market":
            px = self.prices[s]
            qty = float(o["qty"]) if "qty" in o else float(o["notional"]) / px
            self._fill(s, sign * qty, px)
            o.update(status="filled", filled_avg_price=str(px))
        elif o["type"] == "limit" and bot.occ_underlying(s) != s:
            self._fill(s, sign * float(o["qty"]), float(o["limit_price"]), "us_option")
            o.update(status="filled", filled_avg_price=o["limit_price"])
        self.orders[o["id"]] = o
        return o

    def replace_order(self, oid, **c):
        old = self.orders[oid]
        old["status"] = "replaced"
        new = dict(old, **c, id=self._id(), status="new")
        self.orders[new["id"]] = new
        self.sent.append({"replace": oid, **c})
        return new

    def cancel_order(self, oid):
        self.orders[oid]["status"] = "canceled"

    def trigger_stops(self):
        for o in self.orders.values():
            if o["type"] == "stop" and o["status"] == "new" and self.prices[o["symbol"]] <= float(o["stop_price"]):
                self._fill(o["symbol"], -float(o["qty"]), float(o["stop_price"]))
                o["status"] = "filled"


def run(api, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = bot.main(list(argv), api=api)
    return rc, out.getvalue()


class InTmp(unittest.TestCase):
    def setUp(self):
        self._cwd, self._tmp = os.getcwd(), tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()


class Keys(InTmp):
    def test_no_keys_is_a_plain_error(self):
        for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            os.environ.pop(k, None)
        with self.assertRaises(alpaca.AlpacaError) as e:
            alpaca.Alpaca()
        self.assertIn("Never paste keys", str(e.exception))

    def test_live_key_refused_and_env_file_read(self):
        os.makedirs("trader")
        with open("trader/alpaca.env", "w") as fh:
            fh.write("ALPACA_API_KEY=AKLIVE123\nALPACA_SECRET_KEY=x\n")
        with self.assertRaises(alpaca.AlpacaError) as e:
            alpaca.Alpaca()
        self.assertIn("LIVE", str(e.exception))
        with open("trader/alpaca.env", "w") as fh:
            fh.write("ALPACA_API_KEY=PKPAPER1\nALPACA_SECRET_KEY=s3cret\n")
        self.assertEqual(alpaca.Alpaca()._key, "PKPAPER1")

    def test_no_live_url_anywhere(self):
        self.assertEqual(alpaca.TRADING, "https://paper-api.alpaca.markets")
        src = open(alpaca.__file__).read() + open(bot.__file__).read()
        self.assertNotIn("://api.alpaca.markets", src)


class Trail(InTmp):
    def test_floor_math(self):
        st = {"entry": 100.0, "stop_pct": 10, "trigger_pct": 10, "trail_pct": 5, "floor": None}
        self.assertEqual(bot.floor_for(st, 100), 90.0)
        self.assertEqual(bot.floor_for(st, 109.99), 90.0)
        self.assertEqual(bot.floor_for(st, 110), 104.5)
        st["floor"] = 114.0
        self.assertEqual(bot.floor_for(st, 112), 114.0)  # never goes down

    def test_ladder_below_stop_rejected(self):
        errs = bot.validate_trail(10, 10, 5, bot.parse_ladder("20:10,30:20"))
        self.assertEqual(len(errs), 2)
        self.assertIn("can never happen", errs[0])
        self.assertEqual(bot.validate_trail(10, 10, 5, bot.parse_ladder("5:5,8:10")), [])
        api = FakeAlpaca()
        rc, out = run(api, "trail", "start", "TSLA", "--qty", "10", "--ladder", "20:10")
        self.assertEqual(rc, 1)
        self.assertEqual(api.sent, [])

    def test_full_cycle_trail_up_then_stop_out(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        rc, out = run(api, "trail", "start", "TSLA", "--qty", "10")
        self.assertEqual(rc, 0)
        stop = [o for o in api.orders.values() if o["type"] == "stop"][0]
        self.assertEqual(float(stop["stop_price"]), 90.0)
        self.assertEqual(stop["time_in_force"], "gtc")

        api.prices["TSLA"] = 110.0
        run(api, "trail", "run")
        live = [o for o in api.orders.values() if o["type"] == "stop" and o["status"] == "new"]
        self.assertEqual(len(live), 1)
        self.assertEqual(float(live[0]["stop_price"]), 104.5)

        api.prices["TSLA"] = 120.0
        run(api, "trail", "run")
        api.prices["TSLA"] = 113.0          # falls through the 114 stop
        api.trigger_stops()
        rc, out = run(api, "trail", "run")
        self.assertIn("Plan closed", out)
        self.assertGreater(api.cash, 50000)  # sold at 114 after buying at 100
        st = json.load(open("trader/bot/trail-TSLA.json"))
        self.assertEqual(st["status"], "closed")

    def test_ladder_buy_resizes_stop(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        run(api, "trail", "start", "TSLA", "--qty", "10", "--ladder", "5:5")
        api.prices["TSLA"] = 94.0
        rc, out = run(api, "trail", "run")
        self.assertIn("LADDER BUY 5", out)
        self.assertEqual(float(api.pos["TSLA"]["qty"]), 15)
        live = [o for o in api.orders.values() if o["type"] == "stop" and o["status"] == "new"][0]
        self.assertEqual(int(float(live["qty"])), 15)
        self.assertEqual(float(live["stop_price"]), 90.0)
        run(api, "trail", "run")             # the ladder fires once only
        self.assertEqual(float(api.pos["TSLA"]["qty"]), 15)

    def test_closed_market_does_nothing(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        run(api, "trail", "start", "TSLA", "--qty", "10")
        n = len(api.sent)
        api.is_open, api.prices["TSLA"] = False, 150.0
        rc, out = run(api, "trail", "run")
        self.assertIn("Market is closed", out)
        self.assertEqual(len(api.sent), n)

    def test_dry_run_sends_nothing(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        rc, out = run(api, "--dry-run", "trail", "start", "TSLA", "--qty", "10")
        self.assertIn("DRY RUN, not sent", out)
        self.assertIn("would buy 10 TSLA", out)
        self.assertNotIn("BOUGHT", out)
        self.assertEqual(api.sent, [])
        self.assertFalse(os.path.exists("trader/bot/trail-TSLA.json"))


def ptr_json(rows, doc="20035143", name="Hon. Test Member"):
    return {"new": [{"doc_id": doc}], "reports": [{"filing": {"doc_id": doc},
                                                     "report": {"name": name, "rows": rows}}]}


class Copy(InTmp):
    def row(self, **kw):
        r = {"ticker": "NVDA", "asset_type": "ST", "type": "P", "type_name": "purchase",
             "date": (TODAY - datetime.timedelta(days=20)).isoformat(), "amount": "$1,001 - $15,000"}
        r.update(kw)
        return r

    def test_decide(self):
        d = bot.copy_decide
        self.assertEqual(d(self.row(), 0, 1000, TODAY, 60), ("buy", 1000))
        self.assertEqual(d(self.row(type="S"), 10, 1000, TODAY, 60), ("sell", 10))
        self.assertEqual(d(self.row(type="S (partial)"), 10, 1000, TODAY, 60), ("sell", 5))
        self.assertEqual(d(self.row(type="S"), 0, 1000, TODAY, 60)[0], "skip")
        self.assertIn("options", d(self.row(asset_type="OP", option={"kind": "call"}), 0, 1000, TODAY, 60)[1])
        old = (TODAY - datetime.timedelta(days=90)).isoformat()
        self.assertIn("older", d(self.row(date=old), 0, 1000, TODAY, 60)[1])
        self.assertEqual(d(self.row(ticker=None), 0, 1000, TODAY, 60)[0], "skip")

    def test_run_buys_once_and_skips_options(self):
        api = FakeAlpaca()
        api.prices.update(NVDA=200.0, AAPL=250.0)
        os.makedirs("in")
        json.dump(ptr_json([self.row(), self.row(ticker="AAPL", asset_type="OP", option={"kind": "call"}),
                            self.row(ticker="AAPL", type="S")]), open("in/p.json", "w"))
        rc, out = run(api, "copy", "run", "--from-json", "in/p.json", "--dollars", "500")
        self.assertEqual(rc, 0)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["notional"], 500.0)
        self.assertAlmostEqual(float(api.pos["NVDA"]["qty"]), 2.5)
        self.assertIn("options and funds are not copied", out)
        run(api, "copy", "run", "--from-json", "in/p.json")
        self.assertEqual(len(api.sent), 1)    # never copies the same filing row twice

    def test_closed_market_keeps_rows_for_later(self):
        api = FakeAlpaca()
        api.is_open = False
        api.prices["NVDA"] = 200.0
        os.makedirs("in")
        json.dump(ptr_json([self.row()]), open("in/p.json", "w"))
        run(api, "copy", "run", "--from-json", "in/p.json")
        api.is_open = True
        run(api, "copy", "run", "--from-json", "in/p.json")
        self.assertEqual(len(api.sent), 1)


def contract(kind, strike, days):
    exp = (TODAY + datetime.timedelta(days=days)).isoformat()
    sym = "TSLA%s%s%08d" % (exp[2:].replace("-", ""), kind[0].upper(), int(strike * 1000))
    return {"symbol": sym, "type": kind, "strike_price": str(strike), "expiration_date": exp}


class Wheel(InTmp):
    def setUp(self):
        super().setUp()
        self.api = api = FakeAlpaca(cash=50000)
        api.prices["TSLA"] = 250.0
        for kind in ("put", "call"):
            for k in (210, 220, 225, 230, 240, 250, 260, 270, 280):
                for d in (7, 21, 35):
                    c = contract(kind, k, d)
                    api.contracts.append(c)
                    api.quotes[c["symbol"]] = (4.8, 5.2)
        run(api, "wheel", "start", "TSLA")

    def open_short(self):
        return [p for p in self.api.positions() if p["asset_class"] == "us_option"]

    def test_dry_run_start_previews_the_first_order(self):
        os.remove("trader/bot/wheel-TSLA.json")
        rc, out = run(self.api, "--dry-run", "wheel", "start", "TSLA")
        self.assertIn("Preview of the first `wheel run`", out)
        self.assertIn("SELL 1 PUT", out)
        self.assertEqual(self.api.sent, [])
        self.assertFalse(os.path.exists("trader/bot/wheel-TSLA.json"))

    def test_sells_put_10pct_below_in_window(self):
        rc, out = run(self.api, "wheel", "run")
        self.assertIn("SELL 1 PUT", out)
        o = self.api.sent[-1]
        self.assertEqual(o["side"], "sell")
        self.assertIn("P00225000", o["symbol"])     # 250 - 10% = 225, 21 days out
        self.assertEqual(o["limit_price"], 5.0)     # the mid

    def test_put_strike_choice(self):
        c = self.api.option_contracts("TSLA", "put", TODAY.isoformat(), "2100-01-01", None, 225)
        pick = bot.pick_contract(c, self.api.quotes, "put", 225, TODAY, 14, 28)
        self.assertEqual(pick["strike"], 225)
        self.assertEqual(pick["dte"], 21)

    def test_not_enough_cash_refuses(self):
        self.api.cash = 10000
        rc, out = run(self.api, "wheel", "run")
        self.assertIn("needs $22500 cash", out)
        self.assertEqual(self.api.sent, [])

    def test_take_profit_then_assignment_then_call_never_below_cost(self):
        api = self.api
        run(api, "wheel", "run")                    # sell the 225 put at 5.00
        run(api, "wheel", "run")                    # reconcile -> open
        st = json.load(open("trader/bot/wheel-TSLA.json"))
        self.assertEqual(st["open"]["status"], "open")
        self.assertEqual(st["premium_total"], 500.0)
        occ = st["open"]["symbol"]

        api.quotes[occ] = (2.3, 2.4)                # 52% profit
        rc, out = run(api, "wheel", "run")
        self.assertIn("BUYING TO CLOSE", out)
        rc, out = run(api, "wheel", "run")          # position gone -> closed, nothing new this run
        self.assertIn("bought back", out)
        st = json.load(open("trader/bot/wheel-TSLA.json"))
        self.assertIsNone(st["open"])
        self.assertEqual(st["premium_total"], 260.0)

        # next put, then assignment: the short put disappears and 100 shares appear
        run(api, "wheel", "run")
        run(api, "wheel", "run")
        st = json.load(open("trader/bot/wheel-TSLA.json"))
        occ, credit = st["open"]["symbol"], st["open"]["credit"]
        del api.pos[occ]
        api.pos["TSLA"] = {"qty": "100", "avg_entry_price": "225", "asset_class": "us_equity"}
        rc, out = run(api, "wheel", "run")
        self.assertIn("ASSIGNED", out)
        st = json.load(open("trader/bot/wheel-TSLA.json"))
        self.assertEqual(st["cost_basis"], round(225 - credit, 2))   # strike minus the premium

        rc, out = run(api, "wheel", "run")          # now covered calls, >= cost * 1.10
        self.assertIn("SELL 1 CALL", out)
        self.assertGreaterEqual(float(api.sent[-1]["symbol"][-8:]) / 1000, st["cost_basis"] * 1.10)

    def test_called_away_goes_back_to_puts(self):
        api = self.api
        api.pos["TSLA"] = {"qty": "100", "avg_entry_price": "230", "asset_class": "us_equity"}
        run(api, "wheel", "run")
        run(api, "wheel", "run")
        st = json.load(open("trader/bot/wheel-TSLA.json"))
        self.assertEqual(st["open"]["kind"], "call")
        self.assertGreaterEqual(st["open"]["strike"], 253)
        del api.pos[st["open"]["symbol"]]
        del api.pos["TSLA"]
        rc, out = run(api, "wheel", "run")
        self.assertIn("CALLED AWAY", out)
        rc, out = run(api, "wheel", "run")
        self.assertIn("SELL 1 PUT", out)


class Status(InTmp):
    def test_status_runs(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        run(api, "trail", "start", "TSLA", "--qty", "1")
        rc, out = run(api, "status")
        self.assertEqual(rc, 0)
        self.assertIn("PAPER account", out)
        self.assertIn("trail TSLA: open", out)

    def test_status_survives_a_long_log(self):
        api = FakeAlpaca()
        api.prices["TSLA"] = 100.0
        run(api, "test-buy", "TSLA")
        run(api, "trail", "start", "TSLA", "--qty", "1")
        with open("trader/bot/log.jsonl", encoding="utf-8") as fh:
            self.assertGreater(len(fh.readlines()), 1)
        open("trader/bot/copy.json.tmp", "w").close()
        rc, out = run(api, "status")
        self.assertEqual(rc, 0)
        self.assertIn("trail TSLA: open", out)


class PtrPath(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ.pop("TRADER_PELOSI_PTR", None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = self._tmp.name
        os.environ["LOCALAPPDATA"] = os.path.join(self._tmp.name, "local")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def make(self, *parts):
        p = os.path.join(self._tmp.name, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        return p

    def test_env_wins(self):
        os.environ["TRADER_PELOSI_PTR"] = "x/ptr.py"
        self.assertEqual(bot.default_ptr(), "x/ptr.py")

    def test_finds_plugin_copy(self):
        p = self.make(".claude", "plugins", "cache", "day-trader", "skills", "trader-pelosi", "ptr.py")
        self.assertEqual(bot.default_ptr(), os.path.normpath(p))

    def test_finds_desktop_app_copy(self):
        p = self.make("local", "Claude-Data", "acct", "rpm", "a", "b", "plugin_1",
                      "skills", "trader-pelosi", "ptr.py")
        self.assertEqual(bot.default_ptr(), os.path.normpath(p))

    def test_user_skill_folder_first(self):
        self.make(".claude", "plugins", "x", "skills", "trader-pelosi", "ptr.py")
        p = self.make(".claude", "skills", "trader-pelosi", "ptr.py")
        self.assertEqual(os.path.normpath(bot.default_ptr()), os.path.normpath(p))

    def test_missing_falls_back_to_skill_path(self):
        self.assertTrue(bot.default_ptr().endswith(os.path.join("trader-pelosi", "ptr.py")))


if __name__ == "__main__":
    unittest.main()
