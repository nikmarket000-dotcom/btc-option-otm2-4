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
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("DeltaClient")

def get_cycle_target_date_code():
    """
    Returns exact date code (DDMMYY) for the Next-Day Daily 17:30 IST expiry:
    On any day D in IST (from 00:00 to 23:59), selects expiry D+1 at 17:30 IST.
    E.g. On 20th Oct (6:31 PM - 11:59 PM) -> '211026' (21st Oct expiry)
         After 00:00 midnight (becomes 21st Oct) -> '221026' (22nd Oct expiry)
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    target_dt = now_ist.date() + timedelta(days=1)
    return target_dt.strftime("%d%m%y")

def get_cycle_target_expiry_display():
    """Returns human-readable string for the target expiry, e.g. '21-Oct-2026 17:30 IST (211026)'"""
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    target_dt = now_ist.date() + timedelta(days=1)
    return f"{target_dt.strftime('%d-%b-%Y')} 17:30 IST ({target_dt.strftime('%d%m%y')})"

def is_in_settlement_window():
    """
    Delta Exchange daily settlement window is 17:30 IST to 18:30 IST.
    During this 1-hour window, order placement can be rejected by exchange.
    New trade cycle entry opens strictly after 18:30 IST (from 18:31 IST onwards).
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    start_buffer = now_ist.replace(hour=17, minute=30, second=0, microsecond=0)
    end_buffer = now_ist.replace(hour=18, minute=31, second=0, microsecond=0)
    return start_buffer <= now_ist < end_buffer


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
        self.base_url = base_url or os.getenv("DELTA_BASE_URL", "https://api.testnet.deltaex.org")
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
        payload_str = json.dumps(body) if body else ""
        data_bytes = payload_str.encode("utf-8") if body else None

        # Build candidate base URLs (avoid CDN for authenticated / POST requests)
        candidate_bases = [self.base_url]
        if auth or method == "POST":
            is_testnet = "testnet" in self.base_url or "demo" in self.base_url
            api_fallbacks = ["https://api.testnet.deltaex.org", "https://api.testnet.delta.exchange"] if is_testnet else ["https://api.india.delta.exchange", "https://api.delta.exchange"]
            cleaned = []
            for b in candidate_bases:
                c_clean = b.replace("cdn-ind.", "api.").replace("cdn.", "api.")
                if c_clean not in cleaned:
                    cleaned.append(c_clean)
            for fb in api_fallbacks:
                if fb not in cleaned:
                    cleaned.append(fb)
            candidate_bases = cleaned

        last_error = "Unknown error"
        last_code = 500

        for b_url in candidate_bases:
            url = b_url + path + qs
            headers = self._sign_request(method, path, qs, payload_str) if auth else {
                "User-Agent": "DeltaBot/1.0",
                "Content-Type": "application/json"
            }
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=8) as res:
                    return json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as he:
                err_body = he.read().decode("utf-8") if hasattr(he, "read") else ""
                last_error = err_body
                last_code = he.code
                if he.code == 403 or "not allowed by policy" in err_body:
                    continue  # Try next candidate API domain
                logger.error(f"Delta API HTTP {he.code} on {path}: {err_body}")
                return {"success": False, "error": err_body, "code": he.code}
            except Exception as e:
                last_error = str(e)
                continue

        logger.error(f"Delta API Exception across endpoints on {path}: {last_error}")
        return {"success": False, "error": str(last_error), "code": last_code}

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
        """Fetches tickers for all options directly from Delta Exchange (matches configured environment)."""
        endpoints = [self.base_url]
        if "testnet" in self.base_url or "demo" in self.base_url:
            endpoints += ["https://cdn-ind.testnet.deltaex.org", "https://cdn.testnet.deltaex.org", "https://api.india.delta.exchange"]
        else:
            endpoints += ["https://api.india.delta.exchange", "https://cdn.india.delta.exchange", "https://api.delta.exchange"]

        for b_url in endpoints:
            try:
                url = b_url + "/v2/tickers?contract_types=call_options,put_options"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json"
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, context=self.ctx, timeout=6) as res:
                    d = json.loads(res.read().decode("utf-8"))
                    if d.get("success") and d.get("result"):
                        return {t["symbol"]: t for t in d["result"] if "symbol" in t}
            except Exception:
                continue
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

    def find_option(self, opt_type, target_strike, target_date=None, min_strike=None, max_strike=None, min_hours=1, max_hours=48):
        """
        Finds the closest option contract matching target_strike and opt_type ('C' or 'P').
        Strictly matches the target cycle expiry date code (DDMMYY) so all contracts in a cycle expire together.
        Ensures strikes obey min_strike and max_strike constraints so wings never duplicate short legs.
        """
        if not target_date:
            target_date = get_cycle_target_date_code()
        prefix = "C-BTC-" if opt_type in ["C", "call_options"] else "P-BTC-"
        tickers = self.get_option_tickers()
        now = datetime.now(timezone.utc)
        
        candidates = []
        for sym, t in tickers.items():
            if not sym.startswith(prefix):
                continue
            parts = sym.split("-")
            if len(parts) >= 4:
                date_code = parts[3]
                if date_code != target_date:
                    continue
                try:
                    exp_dt = datetime.strptime(date_code, "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    h_left = (exp_dt - now).total_seconds() / 3600.0
                except Exception:
                    continue
            else:
                continue

            strike = float(t.get("strike_price", 0))
            if min_strike is not None and strike < min_strike:
                continue
            if max_strike is not None and strike > max_strike:
                continue

            diff = abs(strike - target_strike)
            candidates.append((diff, h_left, {
                "id": t.get("product_id"),
                "product_id": t.get("product_id"),
                "symbol": sym,
                "strike": strike,
                "mark_price": float(t.get("mark_price", 0) or 0)
            }))
        
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            return candidates[0][2]

        # Fallback: Closest future expiry with at least 8 hours left
        fallback_candidates = []
        for sym, t in tickers.items():
            if not sym.startswith(prefix):
                continue
            parts = sym.split("-")
            if len(parts) >= 4:
                date_code = parts[3]
                try:
                    exp_dt = datetime.strptime(date_code, "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    h_left = (exp_dt - now).total_seconds() / 3600.0
                    if h_left < 8.0:
                        continue
                except Exception:
                    continue
            else:
                continue
            strike = float(t.get("strike_price", 0))
            diff = abs(strike - target_strike)
            fallback_candidates.append((diff, h_left, {
                "id": t.get("product_id"),
                "product_id": t.get("product_id"),
                "symbol": sym,
                "strike": strike,
                "mark_price": float(t.get("mark_price", 0) or 0)
            }))
        if fallback_candidates:
            fallback_candidates.sort(key=lambda x: (x[0], x[1]))
            return fallback_candidates[0][2]

        return None


    def find_option_by_premium(self, opt_type, target_prem, min_strike=None, max_strike=None, target_date=None):
        """
        Scans live Delta Exchange options chain for target_date.
        Finds the exact contract whose live mark_price is closest to target_prem,
        strictly bounded within [min_strike, max_strike].
        """
        if not target_date:
            target_date = get_cycle_target_date_code()
        prefix = "C-BTC-" if opt_type in ["C", "call_options"] else "P-BTC-"
        tickers = self.get_option_tickers()
        candidates = []
        for sym, t in tickers.items():
            if not sym.startswith(prefix):
                continue
            parts = sym.split("-")
            if len(parts) >= 4:
                date_code = parts[3]
                if date_code != target_date:
                    continue
            else:
                continue

            try:
                strike = float(t.get("strike_price", 0))
                mark_p = float(t.get("mark_price", 0) or 0)
            except Exception:
                continue

            if mark_p <= 0:
                continue

            if min_strike is not None and strike < min_strike:
                continue
            if max_strike is not None and strike > max_strike:
                continue

            diff = abs(mark_p - target_prem)
            candidates.append((diff, {
                "id": t.get("product_id"),
                "product_id": t.get("product_id"),
                "symbol": sym,
                "strike": strike,
                "mark_price": mark_p
            }))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None

    def find_18delta_pair(self, target_delta=0.18, target_date=None):
        """
        Scans live Delta Exchange options chain tickers.
        Finds the exact 18-delta Call and Put contracts strictly matching target_date (DDMMYY).
        Returns (spot_price, call_contract, put_contract)
        """
        if not target_date:
            target_date = get_cycle_target_date_code()

        # Try configured base_url first (ensures testnet matches demo.delta.exchange, and live matches live.delta.exchange)
        endpoints = [self.base_url]
        if "testnet" in self.base_url or "demo" in self.base_url:
            endpoints += ["https://cdn-ind.testnet.deltaex.org", "https://cdn.testnet.deltaex.org", "https://api.india.delta.exchange"]
        else:
            endpoints += ["https://api.india.delta.exchange", "https://cdn.india.delta.exchange", "https://api.delta.exchange"]

        tickers_data = None
        for b_url in endpoints:
            try:
                url = b_url + "/v2/tickers?contract_types=call_options,put_options"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json"
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, context=self.ctx, timeout=7) as res:
                    d = json.loads(res.read().decode("utf-8"))
                    if d.get("success") and d.get("result"):
                        tickers_data = d.get("result")
                        break
            except Exception:
                continue

        if not tickers_data:
            return None, None, None

        now = datetime.now(timezone.utc)
        spot_price = None

        daily_calls = []
        daily_puts = []

        for t in tickers_data:
            sym = t.get("symbol", "")
            if not (sym.startswith("C-BTC-") or sym.startswith("P-BTC-")):
                continue

            parts = sym.split("-")
            if len(parts) >= 4:
                date_code = parts[3]
                if date_code != target_date:
                    continue
                try:
                    exp_dt = datetime.strptime(date_code, "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    h_left = (exp_dt - now).total_seconds() / 3600.0
                    st_str = exp_dt.isoformat()
                except Exception:
                    continue
            else:
                continue

            if not spot_price:
                sp = (t.get("greeks") or {}).get("spot") or t.get("spot_price") or t.get("spot_index_price") or t.get("index_price")
                if sp:
                    try:
                        p_val = float(sp)
                        if p_val > 10000:
                            spot_price = p_val
                    except Exception:
                        pass

            greeks = t.get("greeks") or {}
            delta_raw = greeks.get("delta")
            if delta_raw is None:
                continue

            try:
                delta_val = float(delta_raw)
            except Exception:
                continue

            strike = float(t.get("strike_price", 0.0))
            mark_price = float(t.get("mark_price", 0.0) or 0.0)
            quotes = t.get("quotes") or {}

            c_info = {
                "symbol": sym,
                "product_id": t.get("product_id"),
                "strike": strike,
                "delta": delta_val,
                "mark_price": mark_price,
                "settlement_time": st_str,
                "hours_left": h_left,
                "best_bid": float(quotes.get("best_bid", 0.0) or 0.0),
                "best_ask": float(quotes.get("best_ask", 0.0) or 0.0)
            }

            if sym.startswith("C-BTC-"):
                diff = abs(delta_val - target_delta)
                daily_calls.append((diff, c_info))
            else:
                diff = abs(abs(delta_val) - target_delta)
                daily_puts.append((diff, c_info))

        best_call = None
        best_put = None

        if daily_calls:
            daily_calls.sort(key=lambda x: x[0])
            best_call = daily_calls[0][1]

        if daily_puts:
            daily_puts.sort(key=lambda x: x[0])
            best_put = daily_puts[0][1]

        return spot_price, best_call, best_put

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

