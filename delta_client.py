"""
Delta Exchange India Testnet API Client
Handles authentication, product discovery, ticker quotes, and order placement.
"""

import os
import time
import hmac
import hashlib
import json
import urllib.request
import urllib.parse
import ssl
import logging
from datetime import datetime, timezone

def load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v

load_env_file()

class DeltaClient:
    def __init__(self, api_key=None, api_secret=None, base_url=None):
        self.api_key = api_key or os.getenv("DELTA_API_KEY", "")
        self.api_secret = api_secret or os.getenv("DELTA_API_SECRET", "")
        self.base_url = base_url or os.getenv("DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org")
        self.ctx = ssl._create_unverified_context()

    def _sign_request(self, method, path, query_string="", payload=""):
        ts = str(int(time.time()))
        sig_data = method + ts + path + query_string + payload
        sig = hmac.new(self.api_secret.encode("utf-8"), sig_data.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": ts,
            "signature": sig,
            "User-Agent": "DeltaBot/1.0",
            "Content-Type": "application/json"
        }

    def _request(self, method, path, query_params=None, body=None, auth=False):
        qs = ""
        if query_params:
            qs = "?" + urllib.parse.urlencode(query_params)
        url = self.base_url + path + qs
        payload_str = json.dumps(body) if body else ""

        headers = self._sign_request(method, path, qs, payload_str) if auth else {
            "User-Agent": "DeltaBot/1.0",
            "Content-Type": "application/json"
        }

        data_bytes = payload_str.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=8) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8") if hasattr(he, "read") else ""
            logger.error(f"Delta API HTTP {he.code} on {path}: {err_body}")
            return {"success": False, "error": err_body, "code": he.code}
        except Exception as e:
            logger.error(f"Delta API Exception on {path}: {e}")
            return {"success": False, "error": str(e)}

    def get_wallet_balances(self):
        """Fetches wallet balances (USD/INR/BTC)."""
        data = self._request("GET", "/v2/wallet/balances", auth=True)
        if data.get("success"):
            usd_bal = 0.0
            inr_bal = 0.0
            for item in data.get("result", []):
                if item.get("asset_symbol") == "USD":
                    usd_bal = float(item.get("available_balance", 0.0))
                    inr_bal = float(item.get("available_balance_inr", 0.0))
            return {
                "success": True,
                "net_equity": float(data.get("meta", {}).get("net_equity", 0.0)),
                "available_usd": usd_bal,
                "available_inr": inr_bal
            }
        return {"success": False, "error": data.get("error")}

    def get_btc_options_products(self):
        """Fetches all active BTC options products."""
        data = self._request("GET", "/v2/products")
        if data.get("success"):
            opts = []
            for p in data.get("result", []):
                if p.get("underlying_asset", {}).get("symbol") == "BTC" and p.get("contract_type") in ["call_options", "put_options"]:
                    opts.append(p)
            return opts
        return []

    def get_option_tickers(self):
        """Fetches tickers for all options."""
        data = self._request("GET", "/v2/tickers", query_params={"contract_types": "call_options,put_options"})
        if data.get("success"):
            return {t["symbol"]: t for t in data.get("result", []) if "symbol" in t}
        return {}

    def get_margined_positions(self):
        """Fetches active open positions on Delta."""
        data = self._request("GET", "/v2/positions/margined", auth=True)
        if data.get("success"):
            return data.get("result", [])
        return []

    def place_order(self, product_id, size, side, order_type="market_order", limit_price=None):
        """
        Places an order on Delta Exchange.
        side: 'buy' or 'sell'
        order_type: 'market_order' or 'limit_order'
        size: integer contract size
        """
        body = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side.lower(),
            "order_type": order_type
        }
        if limit_price and order_type == "limit_order":
            body["limit_price"] = str(limit_price)

        return self._request("POST", "/v2/orders", body=body, auth=True)

    def find_option(self, opt_type, target_strike, min_hours=2, max_hours=48):
        """
        Finds the closest option contract matching target_strike and opt_type ('C' or 'P' or 'call_options'/'put_options').
        """
        contract_type = "call_options" if opt_type in ["C", "call_options"] else "put_options"
        opts = self.get_btc_options_products()
        now = datetime.now(timezone.utc)
        
        candidates = []
        for p in opts:
            if p.get("contract_type") != contract_type:
                continue
            st_str = p.get("settlement_time")
            if not st_str:
                continue
            try:
                dt = datetime.fromisoformat(st_str.replace("Z", "+00:00"))
                h_left = (dt - now).total_seconds() / 3600.0
                if min_hours <= h_left <= max_hours:
                    strike = float(p.get("strike_price", 0))
                    diff = abs(strike - target_strike)
                    candidates.append((diff, h_left, p))
            except Exception:
                continue

        if not candidates:
            # Fallback without hour restriction
            for p in opts:
                if p.get("contract_type") == contract_type:
                    strike = float(p.get("strike_price", 0))
                    candidates.append((abs(strike - target_strike), 24.0, p))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            return candidates[0][2]
        return None

    def get_ticker_quote(self, symbol):
        """Fetches live quotes for a specific product symbol."""
        tickers = self.get_option_tickers()
        t = tickers.get(symbol)
        if t:
            quotes = t.get("quotes", {})
            return {
                "symbol": symbol,
                "mark_price": float(t.get("mark_price", 0.0) or 0.0),
                "best_bid": float(quotes.get("best_bid", 0.0) or 0.0) if quotes.get("best_bid") else None,
                "best_ask": float(quotes.get("best_ask", 0.0) or 0.0) if quotes.get("best_ask") else None,
            }
        return None

    def place_market_order(self, product_id, size, side):
        """Places a market order on Delta."""
        return self.place_order(product_id=product_id, size=size, side=side, order_type="market_order")

    def cancel_order(self, order_id, product_id):
        """Cancels an order."""
        body = {
            "id": int(order_id),
            "product_id": int(product_id)
        }
        return self._request("DELETE", "/v2/orders", body=body, auth=True)

