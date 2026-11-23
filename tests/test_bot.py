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


def sig(verdict="SETUP", close=100.0, ma=90.0):
    above = close > ma
    return {"close": close, "ma": ma, "pct_over_ma": round((close / ma - 1) * 100, 2),
            "ma_slope_pct": 2.0 if verdict != "NO" else -2.0, "roc": 1.0 if verdict == "SETUP" else 12.0,
            "roc_peak": 25.0, "above_ma": above, "ma_rising": verdict != "NO", "ran": True,
            "reset": verdict == "SETUP", "verdict": verdict}


class Swing(InTmp):
    def setUp(self):
        super().setUp()
        self.signals = {}
        self._orig = bot.swing_signal
        bot.swing_signal = lambda s: self.signals[s]
        self.api = api = FakeAlpaca(cash=50000)
        api.prices.update(AAPL=190.0, MSFT=400.0, NVDA=120.0)

    def tearDown(self):
        bot.swing_signal = self._orig
        super().tearDown()

    def start(self, *extra):
        return run(self.api, "swing", "start", "AAPL", "MSFT", "--dollars", "2000", *extra)

    def test_setup_buys_whole_shares_with_a_gtc_stop(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("WATCH"))
        self.start()
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: SETUP -> BUY 10 at about 190.00; stop 171.00", out)
        self.assertIn("MSFT: WATCH", out)
        buy, stop = self.api.sent
        self.assertEqual((buy["side"], buy["qty"], buy["type"]), ("buy", 10, "market"))
        self.assertEqual((stop["type"], stop["stop_price"], stop["time_in_force"]), ("stop", 171.0, "gtc"))
        st = bot.load(bot.SWING)
        self.assertEqual(st["positions"]["AAPL"]["qty"], 10)

    def test_holds_then_stops_out(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("NO", 300, 350))
        self.start()
        run(self.api, "swing", "run")
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: holding 10, entry 190.00", out)
        self.assertEqual(len(self.api.sent), 2)  # nothing new
        self.api.prices["AAPL"] = 170.0
        self.api.trigger_stops()
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: STOPPED OUT at about 171.00 (-10.0%", out)
        self.assertIn("AAPL: closed on %s; no new buy before %s" % (
            TODAY.isoformat(), (TODAY + datetime.timedelta(days=21)).isoformat()), out)
        st = bot.load(bot.SWING)
        self.assertEqual(st["positions"], {})
        self.assertEqual(st["closed"][0]["reason"], "stop filled")
        self.assertIsNone(self.api.position("AAPL"))

    def test_trend_break_cancels_stop_and_sells(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("WATCH"))
        self.start()
        run(self.api, "swing", "run")
        stop_id = bot.load(bot.SWING)["positions"]["AAPL"]["stop_order_id"]
        self.signals["AAPL"] = sig("NO", 180, 185)
        self.api.prices["AAPL"] = 180.0
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: SELL 10 at about 180.00: closed below the 200-day average", out)
        self.assertEqual(self.api.orders[stop_id]["status"], "canceled")
        self.assertIsNone(self.api.position("AAPL"))

    def test_time_exit_after_hold_days(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("WATCH"))
        self.start("--hold-days", "91")
        run(self.api, "swing", "run")
        st = bot.load(bot.SWING)
        st["positions"]["AAPL"]["opened"] = (TODAY - datetime.timedelta(days=92)).isoformat()
        bot.save(bot.SWING, st)
        self.api.prices["AAPL"] = 209.0
        rc, out = run(self.api, "swing", "run")
        self.assertIn("held 92 days (limit 91)", out)
        self.assertIn("+10.0% vs entry 190.00", out)

    def test_max_positions_and_other_strategies_are_respected(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("SETUP"), NVDA=sig("SETUP"))
        bot.save(bot.trail_path("NVDA"), {"symbol": "NVDA", "status": "open"})
        run(self.api, "swing", "start", "AAPL", "MSFT", "NVDA", "--max-positions", "1")
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: SETUP -> BUY", out)
        self.assertIn("MSFT: SETUP, but 1 swing positions are open (the max)", out)
        self.assertIn("NVDA: skipped, a trailing-stop or wheel plan already trades it", out)

    def test_not_enough_cash_or_dollars(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("SETUP"))
        run(self.api, "swing", "start", "AAPL", "MSFT", "--dollars", "300")
        rc, out = run(self.api, "swing", "run")
        self.assertIn("MSFT: SETUP, but $300 buys no whole share at 400.00", out)
        self.api = FakeAlpaca(cash=100)
        self.api.prices.update(AAPL=190.0, MSFT=400.0)
        os.remove(bot.SWING)
        run(self.api, "swing", "start", "AAPL", "--dollars", "300")
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: SETUP, but 1 shares need $190 and cash is $100", out)

    def test_dry_run_start_previews_and_saves_nothing(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("WATCH"))
        rc, out = run(self.api, "--dry-run", "swing", "start", "AAPL", "MSFT")
        self.assertIn("Preview of the first `swing run`", out)
        self.assertIn("AAPL: SETUP -> WOULD BUY 10", out)
        self.assertEqual(self.api.sent, [])
        self.assertFalse(os.path.exists(bot.SWING))

    def test_stop_keeps_managing_open_positions(self):
        self.signals.update(AAPL=sig("SETUP"), MSFT=sig("SETUP"))
        run(self.api, "swing", "start", "AAPL", "MSFT", "--max-positions", "1")
        run(self.api, "swing", "run")
        rc, out = run(self.api, "swing", "stop")
        self.assertIn("no new buys. 1 open position(s)", out)
        rc, out = run(self.api, "swing", "run")
        self.assertIn("AAPL: holding", out)
        self.assertNotIn("MSFT", out)
        rc, out = run(self.api, "status")
        self.assertIn("swing: off on AAPL, MSFT; open: AAPL 10 @ 190.00", out)

    def test_bad_settings_refused(self):
        rc, out = run(self.api, "swing", "start", "AAPL", "--stop", "0")
        self.assertEqual(rc, 1)
        self.assertIn("--stop must be between 0 and 100", out)


class SwingSignal(unittest.TestCase):
    def test_uses_the_trend_rule(self):
        tr = bot.trend_module()
        orig = tr.load_chart
        closes = [100 + i * 0.5 for i in range(260)]  # steady climb: above a rising average
        tr.load_chart = lambda s, offline=None, rng="2y": [("d%d" % i, c) for i, c in enumerate(closes)]
        try:
            s = bot.swing_signal("ANY")
        finally:
            tr.load_chart = orig
        self.assertTrue(s["above_ma"] and s["ma_rising"])
        self.assertIn(s["verdict"], ("SETUP", "WATCH"))


def bsig(rsi_prev=25.0, close=95.0, prev=94.0, ma20=100.0, ma200=110.0, rsi=29.0):
    return {"close": close, "prev": prev, "rsi": rsi, "rsi_prev": rsi_prev, "ma20": ma20, "ma200": ma200}


class Rsi(unittest.TestCase):
    def test_wilder_by_hand(self):
        # n=2: first changes +1, -1 -> avg gain .5, loss .5 -> 50; then +1 -> gain .75, loss .25 -> 75
        self.assertEqual(bot.rsi_series([1, 2, 1, 2], 2), [None, None, 50.0, 75.0])

    def test_extremes(self):
        self.assertEqual(bot.rsi_series(list(range(1, 20)))[-1], 100.0)
        self.assertEqual(bot.rsi_series(list(range(20, 1, -1)))[-1], 0.0)


class Bounce(InTmp):
    def setUp(self):
        super().setUp()
        self.signals = {}
        self._orig = bot.bounce_signal
        bot.bounce_signal = lambda s: self.signals[s]
        self.api = api = FakeAlpaca(cash=50000)
        api.prices.update(AAPL=95.0, MSFT=400.0)

    def tearDown(self):
        bot.bounce_signal = self._orig
        super().tearDown()

    def test_buys_the_bounce_with_a_5pct_stop(self):
        self.signals.update(AAPL=bsig(), MSFT=bsig(rsi_prev=41.0, rsi=43.0, close=400, prev=398))
        run(self.api, "bounce", "start", "AAPL", "MSFT")
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("AAPL: BOUNCE -> BUY 21 at about 95.00; stop 90.25 at Alpaca (-5%)", out)
        self.assertIn("MSFT: RSI 43.0 (yesterday 41.0); needs under 30 yesterday and an up close today", out)
        self.assertEqual(self.api.sent[1]["stop_price"], 90.25)

    def test_oversold_but_still_falling_is_not_a_bounce(self):
        self.signals.update(AAPL=bsig(close=93.0, prev=94.0))
        run(self.api, "bounce", "start", "AAPL")
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("no entry", out)
        self.assertEqual(self.api.sent, [])

    def test_sells_back_at_the_20_day_average(self):
        self.signals.update(AAPL=bsig())
        run(self.api, "bounce", "start", "AAPL")
        run(self.api, "bounce", "run")
        self.signals["AAPL"] = bsig(close=100.5, prev=99, ma20=100.2, rsi_prev=55, rsi=58)
        self.api.prices["AAPL"] = 100.5
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("AAPL: SELL 21 at about 100.50: back at the 20-day average (100.50 >= 100.20). +5.8%", out)
        self.assertIn("AAPL: closed on", out)  # cool-down starts right away

    def test_time_exit_and_stop(self):
        self.signals.update(AAPL=bsig())
        run(self.api, "bounce", "start", "AAPL", "--hold-days", "14")
        run(self.api, "bounce", "run")
        self.signals["AAPL"] = bsig(close=96, prev=95.5, ma20=99, rsi_prev=35, rsi=36)
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("AAPL: holding 21, entry 95.00", out)
        self.api.prices["AAPL"] = 90.0
        self.api.trigger_stops()
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("AAPL: STOPPED OUT at about 90.25 (-5.0%", out)

    def test_above_200_filter(self):
        self.signals.update(AAPL=bsig(ma200=120.0))
        run(self.api, "bounce", "start", "AAPL", "--above-200")
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("under the 200-day average 120.0 (--above-200)", out)
        self.assertEqual(self.api.sent, [])

    def test_does_not_buy_what_another_strategy_holds(self):
        self.signals.update(AAPL=bsig())
        self.api._fill("AAPL", 10, 95.0)  # e.g. the swing holds it
        run(self.api, "bounce", "start", "AAPL")
        rc, out = run(self.api, "bounce", "run")
        self.assertIn("AAPL: BOUNCE, but you already hold it outside the bounce; skipped.", out)

    def test_dry_run_and_status(self):
        self.signals.update(AAPL=bsig())
        rc, out = run(self.api, "--dry-run", "bounce", "start", "AAPL")
        self.assertIn("AAPL: BOUNCE -> WOULD BUY 21", out)
        self.assertEqual(self.api.sent, [])
        self.assertFalse(os.path.exists(bot.BOUNCE))
        run(self.api, "bounce", "start", "AAPL")
        run(self.api, "bounce", "run")
        run(self.api, "bounce", "stop")
        rc, out = run(self.api, "status")
        self.assertIn("bounce: off, watching AAPL; open: AAPL 21 @ 95.00; closed trades 0", out)

    def test_bad_settings_refused(self):
        rc, out = run(self.api, "bounce", "start", "AAPL", "--stop", "150")
        self.assertEqual(rc, 1)


class BounceSignal(unittest.TestCase):
    def test_reads_closes_through_trend(self):
        tr = bot.trend_module()
        orig = tr.load_chart
        closes = [100.0] * 200 + [100 - i for i in range(1, 16)] + [86.0]  # a slide, then an up day
        tr.load_chart = lambda s, offline=None, rng="2y": [("d%d" % i, c) for i, c in enumerate(closes)]
        try:
            s = bot.bounce_signal("ANY")
        finally:
            tr.load_chart = orig
        self.assertLess(s["rsi_prev"], 30)
        self.assertGreater(s["close"], s["prev"])
        self.assertEqual(s["ma200"], round(sum(closes[-200:]) / 200, 2))


ET = datetime.timezone(datetime.timedelta(hours=-5))


class FakeOrb(FakeAlpaca):
    """Adds a clock with a New York timestamp, 5-minute bars, and bracket orders
    whose stop and take-profit legs cancel each other."""

    def __init__(self, cash=50000.0):
        super().__init__(cash)
        self.now = datetime.datetime(2026, 11, 23, 10, 0, tzinfo=ET)
        self.bar_data = {}

    def clock(self):
        return dict(super().clock(), timestamp=self.now.isoformat(timespec="microseconds"))

    def bars(self, s, start, end=None, timeframe="5Min"):
        return [b for b in self.bar_data.get(s, []) if bot.parse_ts(b["t"]) < bot.parse_ts(end)]

    def set_bars(self, s, rows, start="09:30"):
        """rows: (o, h, l, c) per 5-minute bar from 9:30 ET."""
        t0 = self.now.replace(hour=int(start[:2]), minute=int(start[3:]))
        self.bar_data[s] = [{"t": (t0 + i * bot.BAR).astimezone(datetime.timezone.utc).isoformat()
                                  .replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c, "v": 1000}
                            for i, (o, h, l, c) in enumerate(rows)]

    def submit_order(self, **o):
        if o.get("order_class") != "bracket":
            return super().submit_order(**o)
        base = {k: v for k, v in o.items() if k not in ("order_class", "take_profit", "stop_loss")}
        parent = super().submit_order(**base)
        legs = []
        for kind, px in (("limit", o["take_profit"]["limit_price"]), ("stop", o["stop_loss"]["stop_price"])):
            leg = {"id": self._id(), "symbol": o["symbol"], "qty": o["qty"], "side": "sell", "type": kind,
                   "status": "new", ("limit_price" if kind == "limit" else "stop_price"): px}
            self.orders[leg["id"]] = leg
            legs.append(leg)
        parent["legs"] = legs
        self.sent[-1] = o
        return parent

    def move(self, s, price):
        """Price moves; a bracket leg that is hit fills and cancels its sibling."""
        self.prices[s] = price
        for o in list(self.orders.values()):
            for leg in o.get("legs") or []:
                if leg["status"] != "new":
                    continue
                hit = (leg["type"] == "stop" and price <= float(leg["stop_price"])) or \
                      (leg["type"] == "limit" and price >= float(leg["limit_price"]))
                if hit:
                    px = float(leg.get("stop_price") or leg.get("limit_price"))
                    self._fill(s, -float(leg["qty"]), px)
                    leg.update(status="filled", filled_avg_price=str(px))
                    for other in o["legs"]:
                        if other is not leg and other["status"] == "new":
                            other["status"] = "canceled"


RANGE = [(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101), (101, 102, 100.2, 101.2)]  # high 102, low 99.5


class Orb(InTmp):
    def setUp(self):
        super().setUp()
        self.api = FakeOrb()
        self.api.prices.update(AAPL=102.3, MSFT=400.0)
        run(self.api, "orb", "start", "AAPL", "MSFT")

    def test_waits_while_the_range_builds(self):
        self.api.now = self.api.now.replace(hour=9, minute=40)
        self.api.set_bars("AAPL", RANGE[:2])
        rc, out = run(self.api, "orb", "run")
        self.assertIn("Building the 15-minute opening range; the first breakout check is at 09:50", out)
        self.assertEqual(self.api.sent, [])

    def test_breakout_buys_a_bracket_sized_by_risk(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])  # 9:45 bar closes above 102
        self.api.set_bars("MSFT", RANGE)
        rc, out = run(self.api, "orb", "run")
        # stop = midpoint 100.75; risk 1.55 at 102.30 -> $100 / 1.55 = 64 shares; $2000 / 102.3 = 19 -> 19
        self.assertIn("AAPL: BREAKOUT (5-min close 102.40 > range high 102.00 at 09:45) -> BUY 19 at about 102.30; "
                      "stop 100.75, target 105.40 (2R)", out)
        self.assertIn("MSFT: waiting. Range 99.50-102.00", out)
        o = self.api.sent[0]
        self.assertEqual((o["order_class"], o["time_in_force"]), ("bracket", "gtc"))
        self.assertEqual((o["stop_loss"]["stop_price"], o["take_profit"]["limit_price"]), ("100.75", "105.40"))

    def test_max_loss_caps_the_size(self):
        run(self.api, "orb", "start", "AAPL", "--max-loss", "20", "--stop", "low")
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        rc, out = run(self.api, "orb", "run")
        self.assertIn("BUY 7 at about 102.30; stop 99.50", out)  # $20 / 2.80 risk = 7 shares

    def test_target_then_no_second_trade_in_the_name(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        self.api.set_bars("MSFT", RANGE)
        run(self.api, "orb", "run")
        self.api.move("AAPL", 105.5)
        self.api.now = self.api.now.replace(minute=30)
        rc, out = run(self.api, "orb", "run")
        self.assertIn("AAPL: TARGET HIT at about 105.40: +2.00R", out)
        self.assertEqual(sum(1 for o in self.api.sent if o.get("symbol") == "AAPL"), 1)
        st = bot.load(bot.ORB)
        self.assertEqual(st["history"][0]["how"], "TARGET HIT")

    def test_stop_out(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        run(self.api, "orb", "run")
        self.api.move("AAPL", 100.0)
        rc, out = run(self.api, "orb", "run")
        self.assertIn("AAPL: STOPPED OUT at about 100.75: -1.00R", out)

    def test_flat_by_cancels_legs_and_sells(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        run(self.api, "orb", "run")
        legs = self.api.sent and [o for o in self.api.orders.values() if o.get("legs")][0]["legs"]
        self.api.prices["AAPL"] = 103.0
        self.api.now = self.api.now.replace(hour=15, minute=55)
        rc, out = run(self.api, "orb", "run")
        self.assertIn("AAPL: FLAT BY 15:55 at about 103.00", out)
        self.assertTrue(all(l["status"] == "canceled" for l in legs))
        self.assertIsNone(self.api.position("AAPL"))

    def test_does_not_chase_or_enter_late(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        self.api.prices["AAPL"] = 103.5  # more than half a risk unit above 102
        rc, out = run(self.api, "orb", "run")
        self.assertIn("not chasing", out)
        self.api.now = self.api.now.replace(hour=11, minute=30)
        rc, out = run(self.api, "orb", "run")
        self.assertIn("Past the last entry time (11:30)", out)
        self.assertEqual(self.api.sent, [])

    def test_stale_or_failed_breakouts_are_not_bought(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)] + [(102, 102.2, 101, 101.5)] * 4)
        self.api.now = self.api.now.replace(minute=20)  # breakout bar 9:45-9:50 finished 30 min ago
        rc, out = run(self.api, "orb", "run")
        self.assertIn("AAPL: the first breakout was the 09:45 bar, more than 10 minutes ago; missed", out)
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        self.api.now = self.api.now.replace(minute=0)
        self.api.prices["AAPL"] = 101.9
        rc, out = run(self.api, "orb", "run")
        self.assertIn("AAPL: broke out at 09:45, but 101.90 is back at or below the range high 102.00", out)
        self.assertEqual(self.api.sent, [])

    def test_skips_names_other_strategies_hold(self):
        bot.save(bot.trail_path("MSFT"), {"symbol": "MSFT", "status": "open"})
        self.api.set_bars("AAPL", RANGE)
        rc, out = run(self.api, "orb", "run")
        self.assertIn("MSFT: held or traded by another strategy; skipped", out)

    def test_dry_run_start_previews(self):
        os.remove(bot.ORB)
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        self.api.set_bars("MSFT", RANGE)
        rc, out = run(self.api, "--dry-run", "orb", "start", "AAPL", "MSFT")
        self.assertIn("AAPL: BREAKOUT", out)
        self.assertIn("WOULD BUY 19", out)
        self.assertEqual(self.api.sent, [])
        self.assertFalse(os.path.exists(bot.ORB))

    def test_bad_settings_refused(self):
        rc, out = run(self.api, "orb", "start", "AAPL", "--range", "7", "--flat-by", "11:00")
        self.assertEqual(rc, 1)
        self.assertIn("--range must be 5 to 60", out)
        self.assertIn("9:30 < --last-entry < --flat-by", out)

    def test_status_line(self):
        self.api.set_bars("AAPL", RANGE + [(101.2, 102.5, 101.1, 102.4)])
        run(self.api, "orb", "run")
        self.api.move("AAPL", 105.5)
        run(self.api, "orb", "run")
        rc, out = run(self.api, "status")
        self.assertIn("orb: on, watching AAPL, MSFT; today (2026-11-23): AAPL closed; past trades 1, "
                      "total $+58.90, avg +2.00R", out)

    def test_parse_ts_handles_alpaca_nanoseconds(self):
        t = bot.parse_ts("2026-11-23T10:00:00.123456789-05:00")
        self.assertEqual((t.hour, t.utcoffset()), (10, datetime.timedelta(hours=-5)))


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
