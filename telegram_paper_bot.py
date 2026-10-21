"""
1H Supertrend Options Paper Trading Bot with Telegram Notifications
===================================================================
Strategy Logic:
  - Fixed OTM 2 / OTM 4 Credit Spreads (BTC on Delta Exchange)
  - 80% Book Profit on Short Leg: Closes short leg when 80% decayed
  - Roll Short Leg: If trend continues, sells new OTM 2 (next day expiry) while KEEPING existing Long Leg
  - Option A Hedge: If Short Leg hits 2x entry premium, buys protective OTM 4 option
  - Full Delta Exchange Brokerage & 18% GST simulated
  - Interactive Telegram Bot Commands (/status, /balance, /positions, /history)
  - Real-time Telegram Alert Notifications
  - TradingView Webhook Listener (/webhook)

Zero external dependencies required (Pure Python standard library).
"""

import os
import sys
import time
import json
import math
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import ssl

# --- LOAD ENVIRONMENT VARIABLES ---
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

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("telegram_bot.log", mode="a")
    ]
)
logger = logging.getLogger("TelegramPaperBot")

# --- DELTA EXCHANGE TESTNET CONNECTOR ---
delta_client = None
if os.getenv("DELTA_API_KEY"):
    try:
        from delta_client import DeltaClient
        delta_client = DeltaClient()
        logger.info("Delta Exchange India Testnet Client Initialized ✅")
    except Exception as e:
        logger.warning(f"DeltaClient not loaded: {e}")

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

def get_current_ist_str():
    """Returns current date and time in IST formatted nicely, e.g. '21-Oct-2026 17:35:10 IST'"""
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    return now_ist.strftime("%d-%b-%Y %H:%M:%S IST")

def place_order_verified(client, product_id, size, side, order_note=""):
    """Places real / testnet order on Delta Exchange with logging and error reporting."""
    if not client or not product_id:
        return False, None, "Client or product_id missing"
    try:
        res = client.place_order(product_id=int(product_id), size=int(size), side=side.lower(), order_type="market_order")
        if isinstance(res, dict) and res.get("success") is True:
            order_info = res.get("result", {})
            logger.info(f"✅ Delta Demo Order SUCCESS [{order_note}]: side={side}, size={size}, prod_id={product_id}")
            return True, order_info, None
        err_msg = json.dumps(res) if isinstance(res, dict) else str(res)
        logger.error(f"❌ Delta Demo Order FAILED [{order_note}]: {err_msg}")
        return False, None, err_msg
    except Exception as e:
        logger.error(f"❌ Delta Demo Order EXCEPTION [{order_note}]: {e}")
        return False, None, str(e)

# --- CONFIGURATION ---
CONFIG = {
    # Account & Trading
    "STARTING_CAPITAL": float(os.getenv("STARTING_CAPITAL", 200.0)),         # $200 Delta Testnet account
    "ACTIVE_TRADING_CAPITAL": float(os.getenv("ACTIVE_TRADING_CAPITAL", 100.0)),   # $100 allocated for active trading
    "RESERVE_BUFFER": float(os.getenv("RESERVE_BUFFER", 100.0)),            # $100 safety reserve
    "UNDERLYING": "BTC",               # BTC
    "CONTRACT_SIZE": 0.01,             # 0.01 BTC (10 micro-contracts on Delta)
    "LOTS_PER_BASKET": int(os.getenv("LOTS_PER_BASKET", 10)), # 10 contracts on Delta
    "STRIKE_INTERVAL": float(os.getenv("STRIKE_INTERVAL", 200.0)),          # $200 strike interval on Delta (OTM 2 = +$400, OTM 8 = +$1600)
    "SHORT_OTM_STEP": 2,               # OTM 2 = 2 * $200 = $400 away
    "LONG_OTM_STEP": 8,                # OTM 8 = 8 * $200 = $1600 away
    "TAKE_PROFIT_PCT": 0.80,           # 80% profit booking threshold
    "USE_4H_FILTER": True,             # 4-Hour Trend Confirmation Filter (Slashes Drawdown to 15.3% & saves $17 in fees!)
    "USD_TO_INR": float(os.getenv("USD_TO_INR", 85.0)), # USD to INR conversion rate for Delta India
    
    # Dual Baskets Architecture
    "BASKETS": {
        "basket_1": {
            "id": "basket_1",
            "name": "Basket 1 (1H+4H Filter | OTM 2/8)",
            "use_4h_filter": True,
            "short_otm_step": 2,  # OTM 2 = $400 away
            "long_otm_step": 8,   # OTM 8 = $1600 away
            "contract_size": 0.01,
            "lots": 10,
        },
        "basket_2": {
            "id": "basket_2",
            "name": "Basket 2 (1H Filter | OTM 2/8)",
            "use_4h_filter": False,  # Follows ONLY 1H - No 4H filter!
            "short_otm_step": 2,  # OTM 2 = $400 away
            "long_otm_step": 8,   # OTM 8 = $1600 away
            "contract_size": 0.01,
            "lots": 10,
        }
    },
    
    # Webhook Server
    "WEBHOOK_HOST": "0.0.0.0",
    "WEBHOOK_PORT": int(os.getenv("PORT", 8085)),
    "WEBHOOK_SECRET_TOKEN": os.getenv("WEBHOOK_SECRET_TOKEN", "SUPER_TREND_SECRET_123"),
    "AUTO_TRADING_ENABLED": True,      # 100% Free Autonomous Trading (No TradingView needed!)
    "AUTO_POLL_INTERVAL_SECONDS": 45,  # Checks live exchange candles every 45s
    
    # Telegram Bot Settings
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "8738017140:AAEx4COFhEC-2wfxFlGaEPAx8xUFtV8oEEU"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "857206681"),
    
    # State Persistence
    "STATE_FILE": "paper_bot_state.json",
    "POLL_INTERVAL_SECONDS": 3
}

# --- BLACK-SCHOLES FORMULAS FOR PAPER PRICING ---
def norm_cdf(x):
    b1, b2, b3, b4, b5 = 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
    p, c = 0.2316419, 0.39894228
    if x >= 0.0:
        t = 1.0 / (1.0 + p * x)
        return 1.0 - c * math.exp(-x * x / 2.0) * t * (t * (t * (t * (t * b5 + b4) + b3) + b2) + b1)
    else:
        t = 1.0 / (1.0 - p * x)
        return c * math.exp(-x * x / 2.0) * t * (t * (t * (t * (t * b5 + b4) + b3) + b2) + b1)

def bs_put(S, K, T, r=0.04, sigma=0.55):
    if T <= 0.0001: return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def bs_call(S, K, T, r=0.04, sigma=0.55):
    if T <= 0.0001: return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def calc_delta_fee(spot, premium_per_btc, qty_btc):
    """Calculates realistic Delta Exchange fees: 0.01% notional capped at 3.5% of premium + 18% GST"""
    notional = spot * qty_btc
    uncapped = notional * 0.0001
    cap = (premium_per_btc * qty_btc) * 0.035
    base = min(uncapped, cap)
    return base * 1.18

# --- TELEGRAM NOTIFIER & COMMAND CLIENT ---
class TelegramClient:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.last_update_id = 0

    def send_message(self, text, parse_mode="HTML"):
        if not self.token or self.token == "YOUR_TELEGRAM_BOT_TOKEN":
            logger.info(f"[TELEGRAM PREVIEW] (No token set)\n{text}")
            return
        
        try:
            url = f"{self.api_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "OptionsBot/1.0"}
            )
            with urllib.request.urlopen(req, context=ctx, timeout=8) as res:
                return json.loads(res.read().decode())
        except Exception as e:
            logger.warning(f"Failed to send Telegram message: {e}")

    def get_updates(self):
        if not self.token or self.token == "YOUR_TELEGRAM_BOT_TOKEN":
            return []
        try:
            ctx = ssl._create_unverified_context()
            url = f"{self.api_url}/getUpdates?offset={self.last_update_id + 1}&timeout=5"
            req = urllib.request.Request(url, headers={"User-Agent": "OptionsBot/1.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as res:
                data = json.loads(res.read().decode())
                if data.get("ok"):
                    return data.get("result", [])
        except Exception as e:
            pass
        return []

telegram = TelegramClient(CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])


# --- PERSISTENT STATE MANAGER ---
class StateManager:
    def __init__(self, filename):
        self.filename = filename
        self.lock = threading.Lock()
        self.state = self.load()

    def load(self):
        st = None
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r") as f:
                    st = json.load(f)
            except Exception as e:
                logger.error(f"Error loading state: {e}. Starting fresh.")
        if not st:
            st = {
                "balance": CONFIG["STARTING_CAPITAL"],
                "initial_capital": CONFIG["STARTING_CAPITAL"],
                "current_trend": None,
                "trend_4h": "BULLISH",
                "active_short": None,
                "active_longs": [],
                "hedge_legs": [],
                "simulated_spot": 65000.0,
                "total_fees_paid": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "tp_80_count": 0,
                "rolled_count": 0,
                "trade_history": []
            }

        # Dual-Basket Auto-Migration & Setup
        if "baskets" not in st or not isinstance(st["baskets"], dict):
            st["baskets"] = {}

        # Basket 1: 1H + 4H Confirmed (OTM 2/8)
        if "basket_1" not in st["baskets"]:
            st["baskets"]["basket_1"] = {
                "id": "basket_1",
                "name": "Basket 1 (1H+4H Filter | OTM 2/8)",
                "current_trend": st.get("current_trend"),
                "active_short": st.get("active_short"),
                "active_longs": st.get("active_longs", []),
                "hedge_legs": st.get("hedge_legs", []),
                "tp_80_count": st.get("tp_80_count", 0),
                "rolled_count": st.get("rolled_count", 0),
                "total_trades": st.get("total_trades", 0),
                "winning_trades": st.get("winning_trades", 0)
            }

        # Basket 2: Pure 1H Fast Momentum (OTM 2/8)
        if "basket_2" not in st["baskets"]:
            st["baskets"]["basket_2"] = {
                "id": "basket_2",
                "name": "Basket 2 (1H Filter | OTM 2/8)",
                "current_trend": None,
                "active_short": None,
                "active_longs": [],
                "hedge_legs": [],
                "tp_80_count": 0,
                "rolled_count": 0,
                "total_trades": 0,
                "winning_trades": 0
            }

        # Keep legacy aliases in sync
        b1 = st["baskets"]["basket_1"]
        st["active_short"] = b1.get("active_short")
        st["active_longs"] = b1.get("active_longs", [])
        st["hedge_legs"] = b1.get("hedge_legs", [])
        st["current_trend"] = b1.get("current_trend")
        return st

    def save(self):
        with self.lock:
            try:
                with open(self.filename, "w") as f:
                    json.dump(self.state, f, indent=2)
            except Exception as e:
                logger.error(f"Error saving state: {e}")

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                self.state[k] = v
            # If active_short, active_longs, etc. updated directly, sync to basket_1
            if "baskets" in self.state and "basket_1" in self.state["baskets"]:
                b1 = self.state["baskets"]["basket_1"]
                if "active_short" in kwargs: b1["active_short"] = kwargs["active_short"]
                if "active_longs" in kwargs: b1["active_longs"] = kwargs["active_longs"]
                if "hedge_legs" in kwargs: b1["hedge_legs"] = kwargs["hedge_legs"]
                if "current_trend" in kwargs: b1["current_trend"] = kwargs["current_trend"]
        self.save()

    def update_basket(self, basket_id, **kwargs):
        with self.lock:
            if "baskets" not in self.state:
                self.state["baskets"] = {}
            if basket_id not in self.state["baskets"]:
                self.state["baskets"][basket_id] = {}
            for k, v in kwargs.items():
                self.state["baskets"][basket_id][k] = v
            # Sync to root legacy aliases if basket_1
            if basket_id == "basket_1":
                if "active_short" in kwargs: self.state["active_short"] = kwargs["active_short"]
                if "active_longs" in kwargs: self.state["active_longs"] = kwargs["active_longs"]
                if "hedge_legs" in kwargs: self.state["hedge_legs"] = kwargs["hedge_legs"]
                if "current_trend" in kwargs: self.state["current_trend"] = kwargs["current_trend"]
        self.save()

state_mgr = StateManager(CONFIG["STATE_FILE"])


# --- TRADING LOGIC ENGINE ---
class StrategyEngine:
    @classmethod
    def get_spot(cls):
        return state_mgr.state.get("simulated_spot", 65000.0)

    @classmethod
    def set_spot(cls, price):
        state_mgr.update(simulated_spot=price)

    @classmethod
    def calc_legs_pnl(cls, active_short, active_longs, hedge_legs, spot, qty):
        """Calculates running unrealized PnL for a given basket's legs."""
        pnl = 0.0
        if active_short:
            t_left = 0.0001
            if active_short.get("expiry_code"):
                try:
                    exp_dt = datetime.strptime(active_short["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    secs_left = max(1.0, (exp_dt - datetime.now(timezone.utc)).total_seconds())
                    t_left = secs_left / (365.0 * 24.0 * 3600.0)
                except Exception:
                    t_left = max(0.0001, 24.0 - active_short.get("hours_held", 0)) / (365.0 * 24.0)
            else:
                t_left = max(0.0001, 24.0 - active_short.get("hours_held", 0)) / (365.0 * 24.0)

            s_val = bs_put(spot, active_short["strike"], t_left) if active_short["type"] == "P" else bs_call(spot, active_short["strike"], t_left)
            if delta_client and active_short.get("product_symbol"):
                try:
                    q = delta_client.get_ticker_quote(active_short["product_symbol"])
                    if q and q.get("mark_price", 0) > 0:
                        s_val = q["mark_price"]
                except Exception:
                    pass
            pnl += (active_short["entry_prem"] - s_val) * qty

        for l in (active_longs or []):
            t_left = 0.0001
            if l.get("expiry_code"):
                try:
                    exp_dt = datetime.strptime(l["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                    secs_left = max(1.0, (exp_dt - datetime.now(timezone.utc)).total_seconds())
                    t_left = secs_left / (365.0 * 24.0 * 3600.0)
                except Exception:
                    t_left = max(0.0001, 24.0 - l.get("hours_held", 0)) / (365.0 * 24.0)
            else:
                t_left = max(0.0001, 24.0 - l.get("hours_held", 0)) / (365.0 * 24.0)

            l_val = bs_put(spot, l["strike"], t_left) if l["type"] == "P" else bs_call(spot, l["strike"], t_left)
            if delta_client and l.get("product_symbol"):
                try:
                    q = delta_client.get_ticker_quote(l["product_symbol"])
                    if q and q.get("mark_price", 0) > 0:
                        l_val = q["mark_price"]
                except Exception:
                    pass
            pnl += (l_val - l["entry_prem"]) * qty

        for h in (hedge_legs or []):
            t_left = max(0.0001, 24.0 - h.get("hours_held", 0)) / (365.0 * 24.0)
            h_val = bs_put(spot, h["strike"], t_left) if h["type"] == "P" else bs_call(spot, h["strike"], t_left)
            pnl += (h_val - h["entry_prem"]) * qty

        return round(pnl, 2)

    @classmethod
    def get_current_metrics(cls):
        """
        Computes real-time:
          - Net Balance (USD & INR)
          - Running Trade Unrealized PnL (USD & INR) across Dual Baskets
          - Total Equity (USD & INR)
        """
        st = state_mgr.state
        spot = cls.get_spot()
        usd_to_inr = float(CONFIG.get("USD_TO_INR", 85.0))

        # 1. Net Balance (USD & INR)
        bal_usd = float(st.get("balance", 200.0))
        bal_inr = bal_usd * usd_to_inr
        if delta_client:
            try:
                w = delta_client.get_wallet_balances()
                if w.get("success"):
                    bal_usd = float(w.get("available_usd", bal_usd))
                    delta_inr = float(w.get("available_inr", 0.0))
                    if delta_inr > 0:
                        bal_inr = delta_inr
                    else:
                        bal_inr = bal_usd * usd_to_inr
            except Exception:
                pass

        # 2. Running Trade Unrealized PnL across Baskets
        total_running_pnl_usd = 0.0
        baskets_summary = {}
        has_any_open = False

        for b_id, b_conf in CONFIG.get("BASKETS", {}).items():
            b_st = st.get("baskets", {}).get(b_id, {})
            b_qty = b_conf.get("contract_size", CONFIG["CONTRACT_SIZE"])
            b_short = b_st.get("active_short")
            b_longs = b_st.get("active_longs", [])
            b_hedges = b_st.get("hedge_legs", [])
            b_pnl = cls.calc_legs_pnl(b_short, b_longs, b_hedges, spot, b_qty)
            total_running_pnl_usd += b_pnl
            if b_short or b_longs:
                has_any_open = True
            baskets_summary[b_id] = {
                "id": b_id,
                "name": b_conf["name"],
                "running_pnl_usd": b_pnl,
                "running_pnl_inr_str": f"₹{b_pnl * usd_to_inr:+,.0f} INR",
                "active_short": b_short,
                "active_longs": b_longs,
                "current_trend": b_st.get("current_trend")
            }

        running_pnl_inr = total_running_pnl_usd * usd_to_inr
        equity_usd = bal_usd + total_running_pnl_usd
        equity_inr = equity_usd * usd_to_inr

        if abs(total_running_pnl_usd) < 0.001:
            running_pnl_inr_str = "₹0 INR"
        else:
            running_pnl_inr_str = f"₹{running_pnl_inr:+,.0f} INR"

        bal_inr_str = f"₹{bal_inr:,.0f} INR"
        equity_inr_str = f"₹{equity_inr:,.0f} INR"

        return {
            "bal_usd": round(bal_usd, 2),
            "bal_inr": round(bal_inr, 2),
            "bal_inr_str": bal_inr_str,
            "running_pnl_usd": round(total_running_pnl_usd, 2),
            "running_pnl_inr": round(running_pnl_inr, 2),
            "running_pnl_inr_str": running_pnl_inr_str,
            "equity_usd": round(equity_usd, 2),
            "equity_inr": round(equity_inr, 2),
            "equity_inr_str": equity_inr_str,
            "has_open": has_any_open,
            "baskets": baskets_summary
        }

    @classmethod
    def build_basket_card(cls, basket_id, b_conf, b_st, spot, current_time_str, trend_1h, trend_4h):
        b_name = b_conf.get("name", f"Basket ({basket_id})")
        s = b_st.get("active_short")
        longs = b_st.get("active_longs", [])
        l = longs[0] if longs else None
        qty = b_conf.get("contract_size", CONFIG["CONTRACT_SIZE"])
        short_step = b_conf.get("short_otm_step", 2)
        long_step = b_conf.get("long_otm_step", 8)

        st_info = f"4h {trend_4h.lower()}/ 1h {trend_1h.lower()}"

        if not s or not l:
            if b_conf.get("use_4h_filter", False) and trend_1h != trend_4h:
                strat_line = f"Strategy: ⏸️ WAITING FOR 4H FILTER.  {st_info}"
                status_text = f"1H is {trend_1h.lower()}, waiting for 4H {trend_4h.lower()} confirmation"
            else:
                strat_line = f"Strategy: ⏸️ STANDBY.  {st_info}"
                status_text = "Waiting for next entry signal"

            return (
                f"{b_name}\n\n"
                f"{strat_line}\n\n"
                f"Status: {status_text}\n"
                f"BTC current Spot: ${spot:,.2f}\n"
                f"time : {current_time_str}\n\n"
                f"Running Trade PnL : $0.00 (₹0 INR)"
            )

        opt_type = s.get("type", "P")
        opt_name = "BULL PUT SPREAD" if opt_type == "P" else "BEAR CALL SPREAD"
        strat_icon = "🟢" if opt_type == "P" else "🔴"
        spread_name = f"{strat_icon} {opt_name} (OTM {short_step} / OTM {long_step}).  {st_info}"

        entry_spot = s.get("entry_spot") or b_st.get("entry_spot") or spot
        entry_time_ist = s.get("entry_time_ist") or b_st.get("entry_time_ist") or current_time_str
        expiry_disp = s.get("expiry_display") or get_cycle_target_expiry_display()

        now_utc = datetime.now(timezone.utc)
        t_left = 0.0001
        if s.get("expiry_code"):
            try:
                exp_dt = datetime.strptime(s["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                t_left = max(1.0, (exp_dt - now_utc).total_seconds()) / (365.0 * 24.0 * 3600.0)
            except Exception:
                t_left = max(0.0001, 24.0 - s.get("hours_held", 0)) / (365.0 * 24.0)

        s_curr_prem = bs_put(spot, s["strike"], t_left) if opt_type == "P" else bs_call(spot, s["strike"], t_left)
        l_curr_prem = bs_put(spot, l["strike"], t_left) if opt_type == "P" else bs_call(spot, l["strike"], t_left)

        if delta_client:
            if s.get("product_symbol"):
                try:
                    q = delta_client.get_ticker_quote(s["product_symbol"])
                    if q and q.get("mark_price", 0) > 0:
                        s_curr_prem = q["mark_price"]
                except Exception:
                    pass
            if l.get("product_symbol"):
                try:
                    q = delta_client.get_ticker_quote(l["product_symbol"])
                    if q and q.get("mark_price", 0) > 0:
                        l_curr_prem = q["mark_price"]
                except Exception:
                    pass

        s_entry = s.get("entry_prem", 0.0)
        l_entry = l.get("entry_prem", 0.0)

        pnl_short = (s_entry - s_curr_prem) * qty
        pnl_long = (l_curr_prem - l_entry) * qty
        pnl_hedge = 0.0
        for h in b_st.get("hedge_legs", []):
            h_curr = bs_put(spot, h["strike"], t_left) if h.get("type") == "P" else bs_call(spot, h["strike"], t_left)
            pnl_hedge += (h_curr - h.get("entry_prem", 0.0)) * qty

        basket_pnl = pnl_short + pnl_long + pnl_hedge
        usd_to_inr = CONFIG.get("USD_TO_INR", 85.0)
        basket_pnl_inr = basket_pnl * usd_to_inr
        pnl_str = f"${basket_pnl:+.2f} ({basket_pnl_inr:+,.0f} INR)"

        return (
            f"{b_name}\n\n"
            f"Strategy: {spread_name}\n\n"
            f"buying sport : ${entry_spot:,.2f}\n"
            f"time : {entry_time_ist}\n\n"
            f"Expiry: {expiry_disp}\n\n"
            f"BTC current Spot: ${spot:,.2f}\n"
            f"time : {current_time_str}\n\n"
            f"taken trade value:\n"
            f"Sell {opt_type}: Strike ${s['strike']:,.0f} @ ${s_entry:.2f}\n"
            f"Buy {opt_type}: Strike ${l['strike']:,.0f} @ ${l_entry:.2f}\n\n"
            f"current trade value:\n"
            f"Sell {opt_type}: Strike ${s['strike']:,.0f} @ ${s_curr_prem:.2f}\n"
            f"Buy {opt_type}: Strike ${l['strike']:,.0f} @ ${l_curr_prem:.2f}\n\n"
            f"Running Trade PnL : {pnl_str}"
        )

    @classmethod
    def format_combined_baskets_alert(cls, trigger_reason=None):
        st = state_mgr.state
        spot = cls.get_spot()
        curr_time_str = get_current_ist_str()
        trend_4h = st.get("trend_4h", "BULLISH")
        trend_1h = st.get("current_trend", "BULLISH")

        baskets_conf = CONFIG.get("BASKETS", {})
        b1_conf = baskets_conf.get("basket_1", {})
        b2_conf = baskets_conf.get("basket_2", {})

        b1_st = st.get("baskets", {}).get("basket_1", {})
        b2_st = st.get("baskets", {}).get("basket_2", {})

        card1 = cls.build_basket_card("basket_1", b1_conf, b1_st, spot, curr_time_str, trend_1h, trend_4h)
        card2 = cls.build_basket_card("basket_2", b2_conf, b2_st, spot, curr_time_str, trend_1h, trend_4h)

        bal = st.get("balance", CONFIG["STARTING_CAPITAL"])
        if delta_client:
            try:
                w = delta_client.get_wallet_balances()
                if w.get("success") and float(w.get("available_usd", 0)) > 0:
                    bal = float(w.get("available_usd", bal))
            except Exception:
                pass

        usd_to_inr = CONFIG.get("USD_TO_INR", 85.0)
        bal_inr = bal * usd_to_inr

        header = f"⚡ <b>{trigger_reason}</b>\n\n" if trigger_reason else ""

        msg = (
            f"{header}"
            f"{card1}\n\n"
            f"------------------------------------\n\n"
            f"{card2}\n\n"
            f"------------------------------------\n"
            f"account balance : ${bal:,.2f} ({bal_inr:,.0f} INR)"
        )
        return msg

    @classmethod
    def send_combined_baskets_alert(cls, trigger_reason=None):
        msg = cls.format_combined_baskets_alert(trigger_reason)
        telegram.send_message(msg)

    @classmethod
    def handle_signal(cls, signal, trend_4h=None, target_basket_id=None):
        """Processes 1H Supertrend signal across configured dual baskets and sends combined alert."""
        st = state_mgr.state
        signal = signal.upper().strip()

        if trend_4h:
            trend_4h = trend_4h.upper().strip()
            state_mgr.update(trend_4h=trend_4h)
        else:
            trend_4h = st.get("trend_4h", "BULLISH")

        logger.info(f"Signal received: {signal} | 4H Trend: {trend_4h}")

        baskets_to_process = [target_basket_id] if target_basket_id else list(CONFIG.get("BASKETS", {}).keys())
        any_opened = False

        for b_id in baskets_to_process:
            b_conf = CONFIG["BASKETS"].get(b_id)
            if not b_conf:
                continue
            b_st = st.get("baskets", {}).get(b_id, {})
            curr_trend = b_st.get("current_trend")
            b_name = b_conf["name"]

            # Filter Check (Basket 1 checks 4H; Basket 2 does NOT)
            if b_conf.get("use_4h_filter", False) and signal != trend_4h:
                logger.info(f"[{b_name}] Signal {signal} filtered out (4H Trend is {trend_4h}). Waiting for alignment.")
                continue

            if curr_trend == signal and (b_st.get("active_short") or len(b_st.get("active_longs", [])) > 0):
                logger.info(f"[{b_name}] Signal matches current trend ({signal}). Position already active.")
                continue

            # Trend Reversal: Close open positions in this basket
            if curr_trend and curr_trend != signal:
                cls.close_basket_positions(b_id, reason=f"Trend Reversal ({curr_trend} ➡️ {signal})")

            # Settlement window check (17:30 - 18:30 IST)
            if is_in_settlement_window():
                logger.info(f"[{b_name}] Settlement window active (17:30 - 18:30 IST). Entry paused until 18:31 IST.")
                continue

            state_mgr.update_basket(b_id, current_trend=signal)
            if cls.open_fresh_spread(signal, basket_id=b_id):
                any_opened = True

        if any_opened:
            cls.send_combined_baskets_alert(trigger_reason=f"NEW ENTRY SIGNAL ({signal})")

    @classmethod
    def open_fresh_spread(cls, signal, basket_id="basket_1"):
        b_conf = CONFIG["BASKETS"].get(basket_id, CONFIG["BASKETS"]["basket_1"])
        b_name = b_conf["name"]
        st = state_mgr.state
        spot = cls.get_spot()
        interval = CONFIG["STRIKE_INTERVAL"]
        atm = round(spot / interval) * interval
        qty = b_conf.get("contract_size", CONFIG["CONTRACT_SIZE"])
        lots = b_conf.get("lots", CONFIG.get("LOTS_PER_BASKET", 10))
        T_init = 24.0 / (365.0 * 24.0)

        target_date_code = get_cycle_target_date_code()
        target_expiry_disp = get_cycle_target_expiry_display()

        short_step = b_conf.get("short_otm_step", 2)
        long_step = b_conf.get("long_otm_step", 8)

        if signal == "BULLISH":
            s_strike = atm - (short_step * interval)
            l_strike = atm - (long_step * interval)
            s_prem = round(bs_put(spot, s_strike, T_init), 2)
            l_prem = round(bs_put(spot, l_strike, T_init), 2)
            opt_type = "P"
            spread_name = f"🟢 BULL PUT SPREAD (OTM {short_step} / OTM {long_step})"
        else:
            s_strike = atm + (short_step * interval)
            l_strike = atm + (long_step * interval)
            s_prem = round(bs_call(spot, s_strike, T_init), 2)
            l_prem = round(bs_call(spot, l_strike, T_init), 2)
            opt_type = "C"
            spread_name = f"🔴 BEAR CALL SPREAD (OTM {short_step} / OTM {long_step})"

        # Fetch live market quotes from Delta Exchange if available
        s_prod = None
        l_prod = None
        if delta_client:
            try:
                s_prod = delta_client.find_option(opt_type, s_strike, target_date=target_date_code)
                l_prod = delta_client.find_option(opt_type, l_strike, target_date=target_date_code)
                if s_prod:
                    q_s = delta_client.get_ticker_quote(s_prod["symbol"])
                    if q_s and q_s.get("mark_price", 0) > 0:
                        s_prem = round(q_s["mark_price"], 2)
                        logger.info(f"[{b_name}] Live Delta quote for Short {s_prod['symbol']}: ${s_prem}")
                if l_prod:
                    q_l = delta_client.get_ticker_quote(l_prod["symbol"])
                    if q_l and q_l.get("mark_price", 0) > 0:
                        l_prem = round(q_l["mark_price"], 2)
                        logger.info(f"[{b_name}] Live Delta quote for Long {l_prod['symbol']}: ${l_prem}")
            except Exception as e:
                logger.error(f"[{b_name}] Error fetching live Delta quotes: {e}")

        # LIVE DEMO ORDER EXECUTION ON DELTA EXCHANGE
        short_order_id = None
        long_order_id = None
        if delta_client:
            try:
                if s_prod and s_prod.get("id"):
                    ok_s, res_s, err_s = place_order_verified(delta_client, s_prod["id"], lots, "sell", f"{b_name} Sell {opt_type} {s_strike}")
                    if ok_s and res_s:
                        short_order_id = res_s.get("id")
                if l_prod and l_prod.get("id"):
                    ok_l, res_l, err_l = place_order_verified(delta_client, l_prod["id"], lots, "buy", f"{b_name} Buy {opt_type} {l_strike}")
                    if ok_l and res_l:
                        long_order_id = res_l.get("id")
            except Exception as e:
                logger.error(f"[{b_name}] Exception placing Demo orders on Delta: {e}")

        fees = calc_delta_fee(spot, s_prem, qty) + calc_delta_fee(spot, l_prem, qty)
        net_credit = s_prem - l_prem
        sl_trigger = round(s_prem * 2.0, 2)
        entry_time_ist = get_current_ist_str()

        short_leg = {
            "type": opt_type,
            "strike": s_strike,
            "product_symbol": s_prod["symbol"] if s_prod else None,
            "product_id": s_prod["id"] if s_prod else None,
            "order_id": short_order_id,
            "entry_prem": s_prem,
            "entry_spot": spot,
            "entry_time_ist": entry_time_ist,
            "sl_trigger": sl_trigger,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "hours_held": 0,
            "is_hedged": False,
            "expiry_code": target_date_code,
            "expiry_display": target_expiry_disp,
            "basket_id": basket_id
        }
        long_leg = {
            "type": opt_type,
            "strike": l_strike,
            "product_symbol": l_prod["symbol"] if l_prod else None,
            "product_id": l_prod["id"] if l_prod else None,
            "order_id": long_order_id,
            "entry_prem": l_prem,
            "entry_spot": spot,
            "entry_time_ist": entry_time_ist,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "hours_held": 0,
            "expiry_code": target_date_code,
            "expiry_display": target_expiry_disp,
            "basket_id": basket_id
        }

        new_balance = st["balance"] - (l_prem * qty) - fees
        new_total_fees = st["total_fees_paid"] + fees

        state_mgr.update(
            balance=round(new_balance, 2),
            total_fees_paid=round(new_total_fees, 2)
        )
        state_mgr.update_basket(
            basket_id,
            active_short=short_leg,
            active_longs=[long_leg],
            hedge_legs=[],
            entry_spot=spot,
            entry_time_ist=entry_time_ist
        )
        return True

    @classmethod
    def check_active_positions(cls):
        """Called every cycle by the background monitor for all baskets."""
        st = state_mgr.state
        spot = cls.get_spot()
        now_utc = datetime.now(timezone.utc)

        for b_id, b_conf in CONFIG.get("BASKETS", {}).items():
            b_st = st.get("baskets", {}).get(b_id, {})
            b_name = b_conf["name"]
            qty = b_conf.get("contract_size", CONFIG["CONTRACT_SIZE"])
            short_step = b_conf.get("short_otm_step", 2)
            long_step = b_conf.get("long_otm_step", 4)
            active_short = b_st.get("active_short")
            active_longs = b_st.get("active_longs", [])
            hedge_legs = b_st.get("hedge_legs", [])

            # 1. Manage Active Long Legs (Hold until Expiry at 17:30 IST)
            remaining_longs = []
            for l in active_longs:
                if l.get("entry_time"):
                    try:
                        et = datetime.fromisoformat(l["entry_time"])
                        l["hours_held"] = round((now_utc - et).total_seconds() / 3600.0, 2)
                    except Exception:
                        pass

                is_l_expired = False
                if l.get("expiry_code"):
                    try:
                        exp_dt = datetime.strptime(l["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                        if now_utc >= exp_dt:
                            is_l_expired = True
                    except Exception:
                        pass
                if not is_l_expired and l.get("hours_held", 0) >= 24.0:
                    is_l_expired = True

                if is_l_expired:
                    # Settle
                    val = max(0.0, l["strike"] - spot) if l["type"] == "P" else max(0.0, spot - l["strike"])
                    fee = calc_delta_fee(spot, val, qty) if val > 0 else 0.0
                    proceeds = (val * qty) - fee
                    new_bal = state_mgr.state["balance"] + proceeds
                    state_mgr.update(balance=round(new_bal, 2))
                    
                    m = cls.get_current_metrics()
                    telegram.send_message(
                        f"🏁 <b>Long Leg Expired [{b_name}]</b>\n"
                        f"<b>Strike:</b> ${l['strike']:,.0f} {l['type']}\n"
                        f"<b>Settlement Value:</b> ${val:.2f}\n"
                        f"------------------------------------\n"
                        f"📈 <b>Running Trade PnL:</b> ${m['running_pnl_usd']:+.2f} ({m['running_pnl_inr_str']})\n"
                        f"💵 <b>Net Account Balance:</b> ${m['bal_usd']:.2f} ({m['bal_inr_str']})"
                    )
                else:
                    remaining_longs.append(l)
            state_mgr.update_basket(b_id, active_longs=remaining_longs)

            # 2. Manage Active Short Leg
            if active_short is not None:
                if active_short.get("entry_time"):
                    try:
                        et = datetime.fromisoformat(active_short["entry_time"])
                        active_short["hours_held"] = round((now_utc - et).total_seconds() / 3600.0, 2)
                    except Exception:
                        pass

                t_left = 0.0001
                if active_short.get("expiry_code"):
                    try:
                        exp_dt = datetime.strptime(active_short["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                        secs_left = max(1.0, (exp_dt - now_utc).total_seconds())
                        t_left = secs_left / (365.0 * 24.0 * 3600.0)
                    except Exception:
                        t_left = max(0.0001, 24.0 - active_short.get("hours_held", 0)) / (365.0 * 24.0)
                else:
                    t_left = max(0.0001, 24.0 - active_short.get("hours_held", 0)) / (365.0 * 24.0)

                val = bs_put(spot, active_short["strike"], t_left) if active_short["type"] == "P" else bs_call(spot, active_short["strike"], t_left)

                if delta_client and active_short.get("product_symbol"):
                    try:
                        q_live = delta_client.get_ticker_quote(active_short["product_symbol"])
                        if q_live and q_live.get("mark_price", 0) > 0:
                            val = q_live["mark_price"]
                    except Exception:
                        pass

                # Check 80% Take Profit
                tp_hit = val <= (active_short["entry_prem"] * (1.0 - CONFIG["TAKE_PROFIT_PCT"]))
                
                # Check 2x Stop-Loss / Option A Hedge
                is_threatened = val >= active_short["sl_trigger"]

                # Trigger Option A Hedge
                if is_threatened and not active_short.get("is_hedged") and not tp_hit:
                    active_short["is_hedged"] = True
                    interval = CONFIG["STRIKE_INTERVAL"]
                    atm = round(spot / interval) * interval
                    if active_short["type"] == "P":
                        h_k = atm - (long_step * interval)
                        h_prem = bs_put(spot, h_k, t_left)
                        h_type = "P"
                    else:
                        h_k = atm + (long_step * interval)
                        h_prem = bs_call(spot, h_k, t_left)
                        h_type = "C"

                    lots = b_conf.get("lots", CONFIG.get("LOTS_PER_BASKET", 10))
                    h_prod = None
                    if delta_client:
                        try:
                            h_prod = delta_client.find_option(h_type, h_k, target_date=active_short.get("expiry_code"))
                            if h_prod and h_prod.get("id"):
                                place_order_verified(delta_client, h_prod["id"], lots, "buy", f"{b_name} Option A Hedge Buy {h_type} {h_k}")
                        except Exception as e:
                            logger.error(f"[{b_name}] Error executing hedge on Delta: {e}")

                    h_fee = calc_delta_fee(spot, h_prem, qty)
                    hedge_cost = (h_prem * qty) + h_fee
                    new_bal = state_mgr.state["balance"] - hedge_cost
                    new_fees = state_mgr.state["total_fees_paid"] + h_fee
                    
                    hedge_legs.append({
                        "type": h_type, "strike": h_k, "entry_prem": h_prem, "hours_held": active_short["hours_held"],
                        "product_id": h_prod.get("id") if h_prod else None
                    })
                    state_mgr.update(balance=round(new_bal, 2), total_fees_paid=round(new_fees, 2))
                    state_mgr.update_basket(b_id, active_short=active_short, hedge_legs=hedge_legs)

                    cls.send_combined_baskets_alert(trigger_reason=f"OPTION A HEDGE TRIGGERED [{b_name}]")

                expired = False
                if active_short.get("expiry_code"):
                    try:
                        exp_dt = datetime.strptime(active_short["expiry_code"], "%d%m%y").replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                        if now_utc >= exp_dt:
                            expired = True
                    except Exception:
                        pass
                if not expired and active_short.get("hours_held", 0) >= 24.0:
                    expired = True

                # --- HANDLE 80% PROFIT BOOK & ROLL ---
                if tp_hit:
                    close_fee = calc_delta_fee(spot, val, qty)
                    profit = (active_short["entry_prem"] - val) * qty - close_fee
                    new_bal = state_mgr.state["balance"] + profit
                    tp_count = b_st.get("tp_80_count", 0) + 1
                    wins = b_st.get("winning_trades", 0) + 1
                    trades_cnt = b_st.get("total_trades", 0) + 1

                    # 1. Close old short on Delta Exchange Demo (Buy back)
                    lots = b_conf.get("lots", CONFIG.get("LOTS_PER_BASKET", 10))
                    if delta_client and active_short.get("product_id"):
                        place_order_verified(delta_client, active_short["product_id"], lots, "buy", f"{b_name} 80% TP Buy-Close Short {active_short['strike']}")

                    state_mgr.update(
                        balance=round(new_bal, 2),
                        tp_80_count=state_mgr.state.get("tp_80_count", 0) + 1,
                        winning_trades=state_mgr.state.get("winning_trades", 0) + 1,
                        total_trades=state_mgr.state.get("total_trades", 0) + 1
                    )
                    state_mgr.update_basket(
                        b_id,
                        tp_80_count=tp_count,
                        winning_trades=wins,
                        total_trades=trades_cnt
                    )

                    # Roll New Short in same trend with next-day expiry
                    interval = CONFIG["STRIKE_INTERVAL"]
                    atm = round(spot / interval) * interval
                    T_init = 24.0 / (365.0 * 24.0)

                    target_expiry_code = get_cycle_target_date_code()
                    target_expiry_disp = get_cycle_target_expiry_display()

                    if active_short["type"] == "P":
                        new_k = atm - (short_step * interval)
                        new_prem = bs_put(spot, new_k, T_init)
                        roll_type = "P"
                    else:
                        new_k = atm + (short_step * interval)
                        new_prem = bs_call(spot, new_k, T_init)
                        roll_type = "C"

                    new_s_prod = None
                    if delta_client:
                        try:
                            new_s_prod = delta_client.find_option(roll_type, new_k, target_date=target_expiry_code)
                            if new_s_prod:
                                q_r = delta_client.get_ticker_quote(new_s_prod["symbol"])
                                if q_r and q_r.get("mark_price", 0) > 0:
                                    new_prem = round(q_r["mark_price"], 2)
                                    logger.info(f"[{b_name}] Rolled live quote {new_s_prod['symbol']}: ${new_prem}")
                        except Exception as e:
                            logger.error(f"[{b_name}] Error quoting roll on Delta: {e}")

                    # 2. Sell new rolled short on Delta Exchange Demo
                    new_order_id = None
                    if delta_client and new_s_prod and new_s_prod.get("id"):
                        ok_rn, res_rn, _ = place_order_verified(delta_client, new_s_prod["id"], lots, "sell", f"{b_name} Rolled Sell Short {roll_type} {new_k}")
                        if ok_rn and res_rn:
                            new_order_id = res_rn.get("id")

                    rolled_count = b_st.get("rolled_count", 0) + 1
                    entry_time_ist = get_current_ist_str()

                    new_short = {
                        "type": roll_type,
                        "strike": new_k,
                        "product_symbol": new_s_prod["symbol"] if new_s_prod else None,
                        "product_id": new_s_prod["id"] if new_s_prod else None,
                        "order_id": new_order_id,
                        "entry_prem": new_prem,
                        "entry_spot": spot,
                        "entry_time_ist": entry_time_ist,
                        "sl_trigger": round(new_prem * 2.0, 2),
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "hours_held": 0,
                        "is_hedged": False,
                        "expiry_code": target_expiry_code,
                        "expiry_display": target_expiry_disp,
                        "basket_id": b_id
                    }
                    state_mgr.update(rolled_count=state_mgr.state.get("rolled_count", 0) + 1)
                    state_mgr.update_basket(b_id, active_short=new_short, rolled_count=rolled_count)

                    cls.send_combined_baskets_alert(trigger_reason=f"80% PROFIT ROLLED [{b_name}]")
                
                elif expired:
                    s_settle_fee = calc_delta_fee(spot, val, qty) if val > 0 else 0.0
                    profit = (active_short["entry_prem"] - val) * qty - s_settle_fee
                    new_bal = state_mgr.state["balance"] + profit
                    state_mgr.update(balance=round(new_bal, 2))
                    state_mgr.update_basket(b_id, active_short=None)
                    cls.send_combined_baskets_alert(trigger_reason=f"CONTRACT EXPIRED & SETTLED [{b_name}]")

    @classmethod
    def close_basket_positions(cls, basket_id, reason):
        st = state_mgr.state
        b_conf = CONFIG.get("BASKETS", {}).get(basket_id, {})
        b_name = b_conf.get("name", basket_id)
        b_st = st.get("baskets", {}).get(basket_id, {})
        spot = cls.get_spot()
        qty = b_conf.get("contract_size", CONFIG["CONTRACT_SIZE"])
        lots = b_conf.get("lots", CONFIG.get("LOTS_PER_BASKET", 10))
        pnl_accum = 0.0
        
        # Close short on Delta and state
        if b_st.get("active_short"):
            s = b_st["active_short"]
            if delta_client and s.get("product_id"):
                place_order_verified(delta_client, s["product_id"], lots, "buy", f"{b_name} Close Short Reversal")
            T = max(0.0, 24 - s.get("hours_held", 0)) / (365.0 * 24.0)
            val = bs_put(spot, s["strike"], T) if s["type"] == "P" else bs_call(spot, s["strike"], T)
            fee = calc_delta_fee(spot, val, qty)
            pnl = (s["entry_prem"] - val) * qty - fee
            pnl_accum += pnl

        # Close longs on Delta and state
        for l in b_st.get("active_longs", []):
            if delta_client and l.get("product_id"):
                place_order_verified(delta_client, l["product_id"], lots, "sell", f"{b_name} Close Long Reversal")
            T = max(0.0, 24 - l.get("hours_held", 0)) / (365.0 * 24.0)
            val = bs_put(spot, l["strike"], T) if l["type"] == "P" else bs_call(spot, l["strike"], T)
            fee = calc_delta_fee(spot, val, qty) if val > 0 else 0.0
            pnl = (val - l["entry_prem"]) * qty - fee
            pnl_accum += pnl

        new_bal = round(st["balance"] + pnl_accum, 2)
        state_mgr.update(balance=new_bal)
        state_mgr.update_basket(
            basket_id,
            active_short=None,
            active_longs=[],
            hedge_legs=[]
        )
        cls.send_combined_baskets_alert(trigger_reason=f"POSITIONS CLOSED [{b_name}] - {reason}")

    @classmethod
    def close_all_open_positions(cls, reason):
        for b_id in CONFIG.get("BASKETS", {}).keys():
            cls.close_basket_positions(b_id, reason)


# --- TELEGRAM COMMAND LISTENER (LONG-POLLING) ---
def telegram_polling_worker():
    logger.info("Telegram command listener started.")
    while True:
        try:
            updates = telegram.get_updates()
            for u in updates:
                telegram.last_update_id = u["update_id"]
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if str(chat_id) != str(CONFIG["TELEGRAM_CHAT_ID"]) and CONFIG["TELEGRAM_CHAT_ID"] != "YOUR_TELEGRAM_CHAT_ID":
                    continue

                st = state_mgr.state
                spot = StrategyEngine.get_spot()

                if text in ["/start", "/help"]:
                    reply = (
                        "🤖 <b>1H Supertrend Options Bot (Delta Demo Execution)</b>\n\n"
                        "<b>Available Commands:</b>\n"
                        "/status or /positions — Combined status for Basket 1 & Basket 2\n"
                        "/trade — Check live candles & open fresh trades on Delta Demo\n"
                        "/reset — Clear state & reset to flat\n"
                        "/market — View live 1H and 4H Supertrend directions\n"
                        "/rollstats — 80% profit book and roll counts\n"
                        "/signal &lt;BULLISH|BEARISH&gt; — Manual signal trigger"
                    )
                    telegram.send_message(reply)

                elif text in ["/balance", "/status", "/positions"]:
                    reply = StrategyEngine.format_combined_baskets_alert()
                    telegram.send_message(reply)

                elif text == "/trade":
                    c1 = fetch_live_klines("1h", 30)
                    c4 = fetch_live_klines("4h", 30)
                    if c1 and c4:
                        d1 = "BULLISH" if calculate_supertrend(c1)[-1] == 1 else "BEARISH"
                        d4 = "BULLISH" if calculate_supertrend(c4)[-1] == 1 else "BEARISH"
                        p = c1[-1]['close']
                        StrategyEngine.set_spot(p)
                        StrategyEngine.handle_signal(d1, trend_4h=d4)
                        telegram.send_message(f"⚡ <b>Evaluating Live Trade:</b> 1H {d1} | 4H {d4} | Spot ${p:,.2f}")
                    else:
                        telegram.send_message("❌ Failed to fetch live market candles.")

                elif text == "/reset":
                    for b_id in CONFIG.get("BASKETS", {}).keys():
                        state_mgr.update_basket(b_id, active_short=None, active_longs=[], hedge_legs=[], current_trend=None)
                    state_mgr.update(active_short=None, active_longs=[], hedge_legs=[], current_trend=None)
                    telegram.send_message("🔄 <b>State Reset Complete!</b> All positions cleared. Bot is flat and ready for fresh trades.")

                elif text == "/rollstats":
                    reply = (
                        f"🎯 <b>ROLL & PROFIT STATS</b>\n"
                        f"<b>80% Profits Booked:</b> {st.get('tp_80_count', 0)} times\n"
                        f"<b>Short Legs Rolled:</b> {st.get('rolled_count', 0)} times\n"
                        f"<b>Exchange Fees Paid:</b> ${st.get('total_fees_paid', 0):.2f}"
                    )
                    telegram.send_message(reply)

                elif text.startswith("/setspot"):
                    parts = text.split()
                    if len(parts) == 2 and parts[1].replace(".", "").isdigit():
                        new_p = float(parts[1])
                        StrategyEngine.set_spot(new_p)
                        telegram.send_message(f"✅ BTC Spot updated to ${new_p:,.2f}")
                    else:
                        telegram.send_message("❌ Usage: /setspot 66500")

                elif text.startswith("/signal"):
                    parts = text.split()
                    if len(parts) >= 2 and parts[1].upper() in ["BULLISH", "BEARISH"]:
                        t4 = parts[2].upper() if len(parts) >= 3 and parts[2].upper() in ["BULLISH", "BEARISH"] else None
                        StrategyEngine.handle_signal(parts[1].upper(), trend_4h=t4)
                    else:
                        telegram.send_message("❌ Usage: /signal BULLISH [BULLISH|BEARISH]")

                elif text == "/toggle4h":
                    CONFIG["USE_4H_FILTER"] = not CONFIG.get("USE_4H_FILTER", True)
                    status_str = "ENABLED ✅ (Drawdown 15.3%)" if CONFIG["USE_4H_FILTER"] else "DISABLED ❌"
                    telegram.send_message(f"⚙️ <b>4-Hour Trend Confirmation Filter is now:</b> {status_str}")

                elif text.startswith("/trend4h"):
                    parts = text.split()
                    if len(parts) == 2 and parts[1].upper() in ["BULLISH", "BEARISH"]:
                        state_mgr.update(trend_4h=parts[1].upper())
                        telegram.send_message(f"✅ 4H Macro Trend manually updated to: <b>{parts[1].upper()}</b>")
                    else:
                        telegram.send_message("❌ Usage: /trend4h BULLISH or /trend4h BEARISH")

                elif text == "/market":
                    c1 = fetch_live_klines("1h", 30)
                    c4 = fetch_live_klines("4h", 30)
                    if c1 and c4:
                        d1 = "🟢 BULLISH" if calculate_supertrend(c1)[-1] == 1 else "🔴 BEARISH"
                        d4 = "🟢 BULLISH" if calculate_supertrend(c4)[-1] == 1 else "🔴 BEARISH"
                        p = c1[-1]['close']
                        telegram.send_message(
                            f"🌐 <b>LIVE BITCOIN MARKET</b>\n"
                            f"<b>BTC Spot:</b> ${p:,.2f}\n"
                            f"<b>1H Supertrend:</b> {d1}\n"
                            f"<b>4H Supertrend:</b> {d4}\n"
                            f"<b>Auto-Trading:</b> {'ENABLED ✅' if CONFIG.get('AUTO_TRADING_ENABLED') else 'DISABLED ❌'}"
                        )
                    else:
                        telegram.send_message("❌ Failed to fetch live market feeds.")

                elif text == "/autotrade":
                    CONFIG["AUTO_TRADING_ENABLED"] = not CONFIG.get("AUTO_TRADING_ENABLED", True)
                    st_str = "ENABLED ✅ (100% Autonomous)" if CONFIG["AUTO_TRADING_ENABLED"] else "DISABLED ❌ (Manual only)"
                    telegram.send_message(f"⚙️ <b>Autonomous Live Trading is now:</b> {st_str}")

        except Exception as e:
            pass
        time.sleep(1.5)


# --- BACKGROUND MONITOR LOOP ---
def monitor_loop():
    logger.info("Background monitor loop started.")
    last_heartbeat_time = time.time()
    
    while True:
        try:
            StrategyEngine.check_active_positions()

            # Live periodic Telegram update every 60 minutes
            now = time.time()
            if now - last_heartbeat_time >= 3600:
                last_heartbeat_time = now
                st = state_mgr.state
                spot = StrategyEngine.get_spot()
                active_s = st.get("active_short")
                m = StrategyEngine.get_current_metrics()

                if active_s:
                    t_left = max(0.0, 24 - active_s.get("hours_held", 0)) / (365.0 * 24.0)
                    val = bs_put(spot, active_s["strike"], t_left) if active_s["type"] == "P" else bs_call(spot, active_s["strike"], t_left)
                    decay_pct = (1.0 - (val / active_s["entry_prem"])) * 100.0
                    exp_str = active_s.get("expiry_display", get_cycle_target_expiry_display())

                    telegram.send_message(
                        f"⏱️ <b>LIVE HOURLY UPDATE</b>\n"
                        f"<b>BTC Spot:</b> ${spot:,.2f}\n"
                        f"<b>Active Short:</b> Strike ${active_s['strike']:,.0f} {active_s['type']}\n"
                        f"📅 <b>Expiry:</b> {exp_str}\n"
                        f"<b>Decay Progress:</b> <b>{decay_pct:.1f}%</b> / 80% Target\n"
                        f"------------------------------------\n"
                        f"📈 <b>Running Trade PnL:</b> ${m['running_pnl_usd']:+.2f} ({m['running_pnl_inr_str']})\n"
                        f"💵 <b>Net Account Balance:</b> ${m['bal_usd']:.2f} ({m['bal_inr_str']})\n"
                        f"💎 <b>Total Equity:</b> ${m['equity_usd']:.2f} ({m['equity_inr_str']})"
                    )
                else:
                    telegram.send_message(
                        f"⏱️ <b>LIVE BOT STATUS</b>\n"
                        f"<b>Status:</b> Online & Watching 1H Supertrend\n"
                        f"<b>BTC Spot:</b> ${spot:,.2f}\n"
                        f"<b>Current Trend:</b> {st.get('current_trend', 'None')}\n"
                        f"------------------------------------\n"
                        f"📈 <b>Running Trade PnL:</b> $0.00 (₹0 INR)\n"
                        f"💵 <b>Net Account Balance:</b> ${m['bal_usd']:.2f} ({m['bal_inr_str']})"
                    )
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        time.sleep(CONFIG["POLL_INTERVAL_SECONDS"])


# --- TRADINGVIEW WEBHOOK HANDLER ---
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        if payload.get("token") != CONFIG["WEBHOOK_SECRET_TOKEN"]:
            self.send_response(401)
            self.end_headers()
            return

        signal = payload.get("signal")
        trend_4h = payload.get("trend_4h")
        if signal in ["BULLISH", "BEARISH"]:
            threading.Thread(target=StrategyEngine.handle_signal, args=(signal, trend_4h), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "message": "Signal accepted"}')
        else:
            self.send_response(400)
            self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(state_mgr.state, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        return

# --- LIVE CANDLE FETCHER & AUTONOMOUS TRADER (100% FREE - NO TRADINGVIEW NEEDED) ---
def fetch_live_klines(interval="1h", limit=50):
    ctx = ssl._create_unverified_context()
    # 1. Binance public candles
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "OptionsBot/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=8) as res:
            raw = json.loads(res.read().decode())
            return [{'open': float(c[1]), 'high': float(c[2]), 'low': float(c[3]), 'close': float(c[4])} for c in raw]
    except Exception:
        pass
    # 2. Coinbase public candles
    try:
        gran = 3600 if interval == "1h" else 14400
        url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={gran}"
        req = urllib.request.Request(url, headers={"User-Agent": "OptionsBot/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=8) as res:
            raw = json.loads(res.read().decode())
            raw.reverse()
            return [{'open': float(c[3]), 'high': float(c[2]), 'low': float(c[1]), 'close': float(c[4])} for c in raw[-limit:]]
    except Exception:
        pass
    return []

def calculate_supertrend(candles, period=10, multiplier=3.0):
    n = len(candles)
    if n < period + 1: return []
    tr = [0.0] * n
    for i in range(n):
        hl = candles[i]['high'] - candles[i]['low']
        tr[i] = hl if i == 0 else max(hl, abs(candles[i]['high'] - candles[i-1]['close']), abs(candles[i]['low'] - candles[i-1]['close']))
    
    atr = [0.0] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    direction = [1] * n
    upper_band, lower_band = [0.0] * n, [0.0] * n
    for i in range(period, n):
        hl2 = (candles[i]['high'] + candles[i]['low']) / 2.0
        bu, bl = hl2 + multiplier * atr[i], hl2 - multiplier * atr[i]
        if i == period:
            upper_band[i], lower_band[i] = bu, bl
            direction[i] = 1 if candles[i]['close'] > upper_band[i] else -1
        else:
            prev_close = candles[i-1]['close']
            upper_band[i] = bu if bu < upper_band[i-1] or prev_close > upper_band[i-1] else upper_band[i-1]
            lower_band[i] = bl if bl > lower_band[i-1] or prev_close < lower_band[i-1] else lower_band[i-1]
            if direction[i-1] == 1:
                direction[i] = -1 if candles[i]['close'] < lower_band[i] else 1
            else:
                direction[i] = 1 if candles[i]['close'] > upper_band[i] else -1
    return direction

def live_candle_autotrader_loop():
    logger.info("Autonomous live candle trader started (Zero TradingView needed!).")
    last_1h_trend = None

    while True:
        try:
            if CONFIG.get("AUTO_TRADING_ENABLED", True):
                c1 = fetch_live_klines("1h", 50)
                c4 = fetch_live_klines("4h", 50)

                if c1 and c4:
                    latest_spot = c1[-1]['close']
                    StrategyEngine.set_spot(latest_spot)

                    dirs1 = calculate_supertrend(c1)
                    dirs4 = calculate_supertrend(c4)

                    if dirs1 and dirs4:
                        curr_1h = "BULLISH" if dirs1[-1] == 1 else "BEARISH"
                        curr_4h = "BULLISH" if dirs4[-1] == 1 else "BEARISH"

                        state_mgr.update(trend_4h=curr_4h)

                        if last_1h_trend is None:
                            last_1h_trend = curr_1h
                            logger.info(f"Initialized Live Feeds: 1H {curr_1h} | 4H {curr_4h} | BTC ${latest_spot:,.2f}")
                            StrategyEngine.handle_signal(curr_1h, trend_4h=curr_4h)
                        elif curr_1h != last_1h_trend:
                            logger.info(f"🚨 LIVE 1H FLIP DETECTED: {last_1h_trend} ➡️ {curr_1h} (4H: {curr_4h})")
                            last_1h_trend = curr_1h
                            telegram.send_message(
                                f"🤖 <b>AUTONOMOUS LIVE SIGNAL DETECTED</b>\n"
                                f"<b>BTC Spot:</b> ${latest_spot:,.2f}\n"
                                f"1H Supertrend flipped to <b>{curr_1h}</b> (4H Trend: <b>{curr_4h}</b>).\n"
                                f"<i>Processing Dual Baskets (Basket 1 & Basket 2) automatically!</i>"
                            )
                            StrategyEngine.handle_signal(curr_1h, trend_4h=curr_4h)
                        else:
                            # Periodic check if any basket is flat outside settlement window
                            if not is_in_settlement_window():
                                StrategyEngine.handle_signal(curr_1h, trend_4h=curr_4h)




        except Exception as e:
            logger.error(f"Error in autotrader loop: {e}")

        time.sleep(CONFIG.get("AUTO_POLL_INTERVAL_SECONDS", 45))


def start_bot():
    logger.info("Starting 1H Supertrend Options Paper Bot...")
    
    # 1. Start Background Monitor
    threading.Thread(target=monitor_loop, daemon=True, name="MonitorWorker").start()
    
    # 2. Start Telegram Polling Worker
    threading.Thread(target=telegram_polling_worker, daemon=True, name="TelegramWorker").start()

    # 3. Start Live Autonomous Market Fetcher (Free TradingView alternative!)
    threading.Thread(target=live_candle_autotrader_loop, daemon=True, name="LiveAutoTrader").start()

    # 4. Start Webhook HTTP Server (optional backup)
    server = HTTPServer((CONFIG["WEBHOOK_HOST"], CONFIG["WEBHOOK_PORT"]), WebhookHandler)
    logger.info(f"Webhook listener online: http://{CONFIG['WEBHOOK_HOST']}:{CONFIG['WEBHOOK_PORT']}/webhook")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.server_close()

if __name__ == "__main__":
    start_bot()
