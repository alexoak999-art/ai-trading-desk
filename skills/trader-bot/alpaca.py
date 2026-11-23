"""Minimal Alpaca client, PAPER ACCOUNT ONLY. Standard library only.

Keys come from the environment, never from the chat:
  ALPACA_API_KEY and ALPACA_SECRET_KEY   (APCA_API_KEY_ID / APCA_API_SECRET_KEY also work)
or from a file you create yourself, trader/alpaca.env, with those two lines
(KEY=value). Keep that file out of git; it is never printed.

There is no live-trading URL in this file on purpose. Going live is a decision
you make yourself, in your own code, not something a bot flips.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
ENV_FILE = "trader/alpaca.env"


class AlpacaError(Exception):
    pass


def _keys(env_file=ENV_FILE):
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    if (not key or not sec) and os.path.exists(env_file):
        vals = {}
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
        key = key or vals.get("ALPACA_API_KEY") or vals.get("APCA_API_KEY_ID")
        sec = sec or vals.get("ALPACA_SECRET_KEY") or vals.get("APCA_API_SECRET_KEY")
    if not key or not sec:
        raise AlpacaError(
            "no Alpaca keys. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your environment "
            "(or in %s). Use PAPER keys from the Alpaca dashboard. Never paste keys into the chat." % env_file)
    if key.startswith("AK"):
        raise AlpacaError("that looks like a LIVE key (starts with AK). This bot is paper-only: "
                          "make paper keys (they start with PK) in the Alpaca dashboard.")
    return key, sec


class Alpaca:
    def __init__(self, env_file=ENV_FILE):
        self._key, self._sec = _keys(env_file)

    # -------------------------------------------------------------- http
    def _req(self, method, base, path, params=None, body=None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._sec,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 404 and method == "GET":
                return None
            raise AlpacaError("%s %s -> HTTP %d: %s" % (method, path, e.code, detail))
        except (urllib.error.URLError, OSError) as e:
            raise AlpacaError("%s %s -> %s" % (method, path, e))

    # -------------------------------------------------------------- account
    def account(self):
        return self._req("GET", TRADING, "/v2/account")

    def clock(self):
        return self._req("GET", TRADING, "/v2/clock")

    def positions(self):
        return self._req("GET", TRADING, "/v2/positions") or []

    def position(self, symbol):
        return self._req("GET", TRADING, "/v2/positions/" + urllib.parse.quote(symbol))

    def orders(self, status="open", symbols=None):
        return self._req("GET", TRADING, "/v2/orders",
                         {"status": status, "symbols": symbols, "nested": "true", "limit": 500}) or []

    def order(self, order_id):
        return self._req("GET", TRADING, "/v2/orders/" + order_id)

    def submit_order(self, **order):
        return self._req("POST", TRADING, "/v2/orders",
                         body={k: (str(v) if isinstance(v, (int, float)) else v)
                               for k, v in order.items() if v is not None})

    def replace_order(self, order_id, **changes):
        return self._req("PATCH", TRADING, "/v2/orders/" + order_id,
                         body={k: str(v) for k, v in changes.items() if v is not None})

    def cancel_order(self, order_id):
        return self._req("DELETE", TRADING, "/v2/orders/" + order_id)

    # -------------------------------------------------------------- data
    def last_price(self, symbol):
        d = self._req("GET", DATA, "/v2/stocks/%s/trades/latest" % urllib.parse.quote(symbol),
                      {"feed": "iex"})
        if not d or "trade" not in d:
            raise AlpacaError("no price for %s" % symbol)
        return float(d["trade"]["p"])

    def option_contracts(self, underlying, kind, exp_from, exp_to, strike_min=None, strike_max=None):
        out, token = [], None
        while True:
            d = self._req("GET", TRADING, "/v2/options/contracts", {
                "underlying_symbols": underlying, "type": kind, "status": "active",
                "expiration_date_gte": exp_from, "expiration_date_lte": exp_to,
                "strike_price_gte": strike_min, "strike_price_lte": strike_max,
                "limit": 1000, "page_token": token}) or {}
            out += d.get("option_contracts") or []
            token = d.get("next_page_token")
            if not token:
                return out

    def option_quotes(self, symbols):
        """{occ_symbol: (bid, ask)} from the latest quotes (free 'indicative' feed)."""
        out = {}
        for i in range(0, len(symbols), 100):
            d = self._req("GET", DATA, "/v1beta1/options/snapshots",
                          {"symbols": ",".join(symbols[i:i + 100]), "feed": "indicative"}) or {}
            for sym, snap in (d.get("snapshots") or {}).items():
                q = snap.get("latestQuote") or {}
                out[sym] = (float(q.get("bp") or 0), float(q.get("ap") or 0))
        return out
