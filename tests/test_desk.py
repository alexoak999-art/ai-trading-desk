import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "trader-agents"))
import trend  # noqa: E402
import paper  # noqa: E402


def chart(closes):
    t0 = 1_600_000_000
    return {"chart": {"result": [{"meta": {"gmtoffset": 0},
                                  "timestamp": [t0 + 86400 * i for i in range(len(closes))],
                                  "indicators": {"quote": [{"close": closes}]}}]}}


def uptrend_then_flat():
    # 250 sessions grinding up 0.1%/day, 60 sessions running up 0.5%/day, then 70 flat
    c, x = [], 100.0
    for _ in range(250):
        x *= 1.001
        c.append(x)
    for _ in range(60):
        x *= 1.005
        c.append(x)
    c += [x] * 70
    return c


class Rule(unittest.TestCase):
    def test_setup_after_run_and_reset(self):
        s = trend.evaluate(uptrend_then_flat())[-1]
        self.assertTrue(s["above_ma"] and s["ma_rising"] and s["ran"])
        self.assertLessEqual(abs(s["roc"]), 5)
        self.assertEqual(s["verdict"], "SETUP")

    def test_still_running_is_watch(self):
        c = uptrend_then_flat()[:310]
        self.assertEqual(trend.evaluate(c)[-1]["verdict"], "WATCH")

    def test_downtrend_is_no(self):
        c = [200 * 0.998 ** i for i in range(300)]
        s = trend.evaluate(c)[-1]
        self.assertEqual(s["verdict"], "NO")
        self.assertIn("below", trend.why(s))

    def test_not_enough_data(self):
        self.assertIsNone(trend.evaluate([100.0] * 150)[-1])

    def test_sma_and_roc(self):
        self.assertEqual(trend.sma_series([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        r = trend.roc_series([100, 110], 1)
        self.assertIsNone(r[0])
        self.assertAlmostEqual(r[1], 10.0)

    def test_history_counts_fresh_signals_once_per_horizon(self):
        c = uptrend_then_flat() + [uptrend_then_flat()[-1] * 1.001 ** i for i in range(80)]
        states = trend.evaluate(c)
        dates = [str(i) for i in range(len(c))]
        trades = trend.history(dates, c, states)
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]["ret_pct"], 0)

    def test_cli_offline_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            for sym, closes in {"UP": uptrend_then_flat(),
                                "DOWN": [200 * 0.998 ** i for i in range(300)]}.items():
                with open(os.path.join(d, sym + ".json"), "w") as fh:
                    json.dump(chart(closes), fh)
            cfg = os.path.join(d, "agents.json")
            with open(cfg, "w") as fh:
                json.dump({"names": ["UP", "DOWN", "MISSING"]}, fh)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                rc = trend.main(["--config", cfg, "--offline", d, "--write", "--json"])
                out = json.load(open("trader/agents/technical.json"))
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0)
            v = {n["symbol"]: n["verdict"] for n in out["names"]}
            self.assertEqual(v, {"UP": "SETUP", "DOWN": "NO", "MISSING": "ERROR"})


class Paper(unittest.TestCase):
    def test_buy_sell_and_positions(self):
        with tempfile.TemporaryDirectory() as d:
            book = os.path.join(d, "paper.json")
            self.assertEqual(paper.main(["buy", "msft", "--qty", "10", "--price", "100",
                                         "--book", book, "--date", "2026-11-01"]), 0)
            self.assertEqual(paper.main(["buy", "MSFT", "--qty", "10", "--price", "110",
                                         "--book", book]), 0)
            self.assertEqual(paper.main(["sell", "MSFT", "--qty", "30", "--price", "120",
                                         "--book", book]), 1)  # no shorts
            self.assertEqual(paper.main(["sell", "MSFT", "--qty", "5", "--price", "120",
                                         "--book", book]), 0)
            p = paper.positions(paper.load(book))["MSFT"]
            self.assertEqual(p["qty"], 15)
            self.assertAlmostEqual(p["realized"], 5 * (120 - 105))
            self.assertAlmostEqual(p["cost"] / p["qty"], 105)
            self.assertEqual(paper.main(["sell", "MSFT", "--price", "90", "--book", book]), 0)
            self.assertEqual(paper.positions(paper.load(book))["MSFT"]["qty"], 0)
            self.assertEqual(paper.main(["buy", "MSFT", "--price", "90", "--book", book]), 1)


if __name__ == "__main__":
    unittest.main()
