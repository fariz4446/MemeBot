"""MemeBot - personal Solana meme-coin bot with a localhost-only dashboard.

* Data: DexScreener (discovery + prices), RugCheck (risk flags), Jupiter (quotes / swaps)
* Paper mode uses REAL Jupiter quotes for fills (price impact + routing included).
* Dashboard binds to 127.0.0.1 only and needs a random token cookie.
* Private key is never stored: config only keeps the PATH to your keypair file.
"""
import asyncio
import base64
import collections
import json
import os
import random
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MemeBot"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
TOKEN_PATH = APP_DIR / "access.token"
PORT = 8787

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
JUP = "https://lite-api.jup.ag/swap/v1"
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
BASE_TX_FEE_LAMPORTS = 5000

DEFAULTS = {
    "mode": "paper",              # paper | live
    "live_acknowledged": False,   # set from dashboard after typed confirmation
    "buy_amount_sol": 0.1,
    "slippage_bps": 300,
    "min_liquidity_usd": 15000,
    "min_volume_usd": 8000,       # 24h volume
    "max_market_cap_usd": 4000000,
    "min_age_minutes": 5,
    "max_age_hours": 24,
    "take_profit_pct": 100,       # 0 = off
    "stop_loss_pct": 30,          # 0 = off
    "trailing_stop_pct": 20,      # 0 = off, trails from peak once price was above entry
    "max_open_positions": 5,
    "rug_check": True,            # RugCheck: reject token if any 'danger' flag
    "scan_interval_sec": 20,
    "priority_fee_lamports": 100000,
    "safety_cap_sol": 0.5,        # hard cap per trade, protects against typos
    "paper_start_balance_sol": 10,
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "keypair_path": "",
    "scanner_enabled": True,            # DEX scanner bot (DexScreener + Jupiter)
    "sol_usd": 0.0,                     # 0 = fetch live SOL price
    # ---- Pump.fun sniper (PumpDev feed). launch_mode: paper = DEMO, live = real trades ----
    "launch_mode": "paper",
    "launch_live_ack": False,           # set from the dashboard after the typed confirmation
    "launch_env_path": "",              # your pumpbot.env (PumpDev key). blank = %APPDATA%\\MemeBot\\pumpdev.env
    "launch_max_total_spend_sol": 0.5,  # live only: stop buying after this much SOL spent in total (0 = off)
    # ---- Pump.fun launch mode (PAPER ONLY) - mirrors the user's pumpbot bot.yaml ----
    "launch_enabled": False,
    "launch_strategy": "snipe",         # snipe (buy at launch) | momentum (watch first)
    "launch_buy_sol": 0.1,
    "launch_exit_strategy": "tp_sl",    # tp_sl | time_based | manual
    "launch_take_profit_pct": 50,
    "launch_stop_loss_pct": 20,
    "launch_max_hold_sec": 60,          # timeout (0 = none)
    "launch_trail_start_x": 1.25,       # trailing only arms once value reached this multiple
    "launch_trail_pct": 0,              # 0 = off
    "launch_stall_sec": 0,              # 0 = off
    "launch_stall_min_x": 1.10,
    "launch_exit_on_dev_sell": False,
    "launch_marry_mode": False,         # never sell automatically
    "launch_yolo_mode": True,           # False = buy one token only, then stop buying
    "launch_match_string": "",
    "launch_creator_address": "",
    "launch_max_token_age_sec": 0,      # 0 = off
    "launch_max_mcap_sol": 50.0,        # entry filter (snipe)
    "launch_min_dev_buy_sol": 0.5,
    "launch_max_dev_buy_sol": 5.0,
    "launch_watch_sec": 45,             # momentum settings
    "launch_max_watch": 30,
    "launch_min_buyers": 5,
    "launch_min_net_buy_sol": 2.0,
    "launch_min_mcap_gain_pct": 10,
    "launch_max_entry_mcap_sol": 70.0,
    "launch_max_sell_ratio": 0.35,
    "launch_max_positions": 3,
    "launch_max_consec_losses": 8,      # 0 = off here
    "launch_tip_sol": 0.0005,           # priority fee per transaction
    "launch_commission_pct": 0.25,      # PumpDev-style commission per side
    "launch_extra_impact_pct": 2.0,     # simulated price impact per side
    "launch_curve_fee_pct": 1.0,        # Pump.fun's own fee (set 0 to match pumpbot paper)
    "launch_latency_ms": 800,           # extras not in pumpbot paper (set 0 to match it)
    "launch_fail_pct": 25,
    "launch_sell_fail_pct": 10,
    "launch_max_slippage_pct": 30,
}


def res(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


def http_get(url, **kw):
    r = requests.get(url, timeout=12, headers={"User-Agent": "MemeBot/1.0"}, **kw)
    r.raise_for_status()
    return r.json()


class Bot:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                self.cfg.update({k: v for k, v in json.loads(CONFIG_PATH.read_text()).items() if k in DEFAULTS})
            except Exception:
                pass
        self.state = {"paper_balance": self.cfg["paper_start_balance_sol"], "positions": [], "history": [], "bought": [], "launch_positions": []}
        if STATE_PATH.exists():
            try:
                self.state.update(json.loads(STATE_PATH.read_text()))
            except Exception:
                pass
        self.state.setdefault("launch_positions", [])
        self.state.setdefault("launch_live_spent", 0.0)
        self.sol_usd_auto = 0.0
        self.running = False
        self.logs = []
        self.rejected = {}   # mint -> expiry epoch
        self.live_balance = None
        self._kp = None
        self.last_scan = {"t": None, "checked": 0, "bought": 0, "stats": {}, "rows": []}

    # ---------- persistence / logging ----------
    def save(self):
        STATE_PATH.write_text(json.dumps(self.state))
        CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2))

    def log(self, msg):
        with self.lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "m": msg})
            self.logs = self.logs[-200:]
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    # ---------- config ----------
    def update_config(self, patch):
        with self.lock:
            for k, v in patch.items():
                if k not in DEFAULTS:
                    continue
                d = DEFAULTS[k]
                try:
                    if isinstance(d, bool):
                        v = bool(v)
                    elif isinstance(d, int):
                        v = int(float(v))
                    elif isinstance(d, float):
                        v = float(v)
                    else:
                        v = str(v).strip()
                except (TypeError, ValueError):
                    continue
                self.cfg[k] = v
            if self.cfg["mode"] not in ("paper", "live"):
                self.cfg["mode"] = "paper"
            if self.cfg["mode"] == "live" and not self.cfg["live_acknowledged"]:
                self.cfg["mode"] = "paper"
                self.log("Live mode needs the confirmation in the dashboard - staying on paper.")
            if self.cfg["launch_strategy"] not in ("snipe", "momentum"):
                self.cfg["launch_strategy"] = "snipe"
            if self.cfg["launch_exit_strategy"] not in ("tp_sl", "time_based", "manual"):
                self.cfg["launch_exit_strategy"] = "tp_sl"
            if self.cfg["launch_mode"] not in ("paper", "live"):
                self.cfg["launch_mode"] = "paper"
            if self.cfg["launch_mode"] == "live":
                if not self.cfg["launch_live_ack"]:
                    self.cfg["launch_mode"] = "paper"
                    self.log("Sniper LIVE mode needs the typed confirmation - staying on DEMO.")
                elif not LAUNCH.api_key():
                    self.cfg["launch_mode"] = "paper"
                    self.log("Sniper LIVE mode needs your PumpDev key file (see Settings) - staying on DEMO.")
            self._kp = None
            self.save()

    # ---------- wallet ----------
    def keypair(self):
        if self._kp is None:
            from solders.keypair import Keypair
            p = Path(self.cfg["keypair_path"])
            if not p.is_file():
                raise RuntimeError("Keypair file not found. Set its path in settings.")
            self._kp = Keypair.from_bytes(bytes(json.loads(p.read_text())))
        return self._kp

    def rpc(self, method, params):
        r = requests.post(self.cfg["rpc_url"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(str(j["error"]))
        return j["result"]

    def refresh_live_balance(self):
        try:
            kp = self.keypair()
            self.live_balance = self.rpc("getBalance", [str(kp.pubkey())])["value"] / LAMPORTS
        except Exception:
            self.live_balance = None

    # ---------- market data ----------
    def discover(self):
        mints = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            try:
                for t in http_get(DEX + path):
                    if t.get("chainId") == "solana" and t["tokenAddress"] not in mints:
                        mints.append(t["tokenAddress"])
            except Exception as e:
                self.log(f"discovery error: {e}")
        now = time.time()
        bought = set(self.state["bought"])
        mints = [m for m in mints if m not in bought and self.rejected.get(m, 0) < now][:30]
        if not mints:
            return []
        pairs = http_get(f"{DEX}/tokens/v1/solana/{','.join(mints)}")
        best = {}
        for p in pairs:
            addr = p["baseToken"]["address"]
            if addr in mints and (addr not in best or (p.get("liquidity") or {}).get("usd", 0) > (best[addr].get("liquidity") or {}).get("usd", 0)):
                best[addr] = p
        return list(best.values())

    def prices(self, mints):
        out = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            for p in http_get(f"{DEX}/tokens/v1/solana/{','.join(chunk)}"):
                a = p["baseToken"]["address"]
                if a in chunk and p.get("quoteToken", {}).get("address") == WSOL and p.get("priceNative"):
                    liq = (p.get("liquidity") or {}).get("usd", 0)
                    if a not in out or liq > out[a][1]:
                        out[a] = (float(p["priceNative"]), liq)
        return {k: v[0] for k, v in out.items()}

    def check_filters(self, p):
        c = self.cfg
        liq = (p.get("liquidity") or {}).get("usd", 0) or 0
        vol = (p.get("volume") or {}).get("h24", 0) or 0
        mc = p.get("marketCap") or p.get("fdv") or 0
        created = p.get("pairCreatedAt")
        age_min = (time.time() - created / 1000) / 60 if created else None
        if p.get("quoteToken", {}).get("address") != WSOL:
            return "not a SOL pair"
        if liq < c["min_liquidity_usd"]:
            return f"liquidity ${liq:,.0f}"
        if vol < c["min_volume_usd"]:
            return f"volume ${vol:,.0f}"
        if mc and mc > c["max_market_cap_usd"]:
            return f"mcap ${mc:,.0f}"
        if age_min is not None:
            if age_min < c["min_age_minutes"]:
                return f"too young ({age_min:.0f}m)"
            if age_min > c["max_age_hours"] * 60:
                return "too old"
        return None

    def rug_dangers(self, mint):
        data = http_get(f"{RUG}/tokens/{mint}/report/summary")
        return [r.get("name", "?") for r in data.get("risks", []) if r.get("level") == "danger"]

    # ---------- swaps ----------
    def quote(self, inp, out, amount):
        return http_get(f"{JUP}/quote", params={"inputMint": inp, "outputMint": out, "amount": int(amount),
                                                 "slippageBps": self.cfg["slippage_bps"]})

    def decimals(self, mint):
        return self.rpc("getTokenSupply", [mint])["value"]["decimals"]

    def execute_swap(self, q):
        from solders.transaction import VersionedTransaction
        kp = self.keypair()
        r = requests.post(f"{JUP}/swap", json={
            "quoteResponse": q, "userPublicKey": str(kp.pubkey()),
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": self.cfg["priority_fee_lamports"]}, timeout=20)
        r.raise_for_status()
        raw = base64.b64decode(r.json()["swapTransaction"])
        vt = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(vt.message, [kp])
        sig = self.rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        for _ in range(30):
            time.sleep(2)
            st = self.rpc("getSignatureStatuses", [[sig]])["value"][0]
            if st:
                if st.get("err"):
                    raise RuntimeError(f"tx failed: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig
        raise RuntimeError("tx not confirmed in 60s: " + sig)

    def fee_sol(self):
        return (self.cfg["priority_fee_lamports"] + BASE_TX_FEE_LAMPORTS) / LAMPORTS

    # ---------- trading ----------
    def buy(self, p):
        c = self.cfg
        mint, sym = p["baseToken"]["address"], p["baseToken"]["symbol"]
        sol = min(c["buy_amount_sol"], c["safety_cap_sol"])
        live = c["mode"] == "live"
        if not live and self.state["paper_balance"] < sol + self.fee_sol():
            self.log("paper balance too low for next buy")
            return False
        q = self.quote(WSOL, mint, sol * LAMPORTS)
        impact = float(q.get("priceImpactPct") or 0) * 100
        if impact * 100 > c["slippage_bps"]:
            self.log(f"skip {sym}: price impact {impact:.2f}% above slippage setting")
            self.rejected[mint] = time.time() + 600
            return False
        out_raw = int(q["outAmount"])
        dec = self.decimals(mint)
        tokens = out_raw / 10 ** dec
        if tokens <= 0:
            return False
        sig = None
        if live:
            sig = self.execute_swap(q)
        else:
            self.state["paper_balance"] -= sol + self.fee_sol()
        entry = sol / tokens
        with self.lock:
            self.state["bought"].append(mint)
            self.state["positions"].append({
                "mint": mint, "symbol": sym, "entry_price": entry, "entry_sol": sol, "tokens_raw": out_raw,
                "decimals": dec, "peak_price": entry, "last_price": entry, "opened_at": time.time(),
                "mode": c["mode"], "sig": sig, "fails": 0, "impact_pct": impact})
            self.save()
        self.log(f"BUY [{c['mode']}] {sym} {sol} SOL @ {entry:.3e} SOL/token (impact {impact:.2f}%)")
        return True

    def sell(self, pos, reason):
        try:
            q = self.quote(pos["mint"], WSOL, pos["tokens_raw"])
            proceeds = int(q["outAmount"]) / LAMPORTS - self.fee_sol()
            if pos["mode"] == "live":
                self.execute_swap(q)
            else:
                self.state["paper_balance"] += max(proceeds, 0)
        except Exception as e:
            pos["fails"] += 1
            self.log(f"sell failed {pos['symbol']} ({pos['fails']}): {e}")
            if pos["mode"] == "paper" and pos["fails"] >= 10:
                proceeds = 0.0
                reason = "no exit route (written off)"
            else:
                return False
        with self.lock:
            self.state["positions"] = [x for x in self.state["positions"] if x is not pos and x["mint"] != pos["mint"]]
            self.state["history"].append({
                "symbol": pos["symbol"], "mint": pos["mint"], "mode": pos["mode"], "reason": reason,
                "entry_sol": pos["entry_sol"], "exit_sol": proceeds, "pnl_sol": proceeds - pos["entry_sol"],
                "opened_at": pos["opened_at"], "closed_at": time.time()})
            self.state["history"] = self.state["history"][-500:]
            self.save()
        self.log(f"SELL [{pos['mode']}] {pos['symbol']} {reason}: {proceeds - pos['entry_sol']:+.4f} SOL")
        return True

    def manage_positions(self):
        with self.lock:
            positions = list(self.state["positions"])
        if not positions:
            return
        px = self.prices([p["mint"] for p in positions])
        c = self.cfg
        for pos in positions:
            price = px.get(pos["mint"])
            if price is None:
                pos["fails"] += 0  # no price this tick; keep waiting
                continue
            pos["last_price"] = price
            pos["peak_price"] = max(pos["peak_price"], price)
            pnl = (price / pos["entry_price"] - 1) * 100
            reason = None
            if c["take_profit_pct"] and pnl >= c["take_profit_pct"]:
                reason = f"take profit {pnl:.0f}%"
            elif c["stop_loss_pct"] and pnl <= -c["stop_loss_pct"]:
                reason = f"stop loss {pnl:.0f}%"
            elif c["trailing_stop_pct"] and pos["peak_price"] > pos["entry_price"] and \
                    price <= pos["peak_price"] * (1 - c["trailing_stop_pct"] / 100):
                reason = f"trailing stop (peak {(pos['peak_price'] / pos['entry_price'] - 1) * 100:.0f}%)"
            if reason:
                self.sell(pos, reason)
        self.save()

    def _row(self, p, verdict):
        created = p.get("pairCreatedAt")
        return {"symbol": p["baseToken"]["symbol"], "mint": p["baseToken"]["address"],
                "liq": (p.get("liquidity") or {}).get("usd", 0) or 0,
                "vol": (p.get("volume") or {}).get("h24", 0) or 0,
                "mc": p.get("marketCap") or p.get("fdv") or 0,
                "age_min": (time.time() - created / 1000) / 60 if created else None,
                "verdict": verdict}

    def scan_and_buy(self):
        c = self.cfg
        cands = self.discover()
        stats, rows, bought = {}, [], 0
        for p in cands:
            if len(self.state["positions"]) >= c["max_open_positions"]:
                break
            mint, sym = p["baseToken"]["address"], p["baseToken"]["symbol"]
            why = self.check_filters(p)
            if why:
                self.rejected[mint] = time.time() + 300
                key = why.split(" $")[0].split(" (")[0]
                stats[key] = stats.get(key, 0) + 1
                rows.append(self._row(p, "filter: " + why))
                continue
            if c["rug_check"]:
                try:
                    dangers = self.rug_dangers(mint)
                except Exception as e:
                    self.log(f"rugcheck unavailable for {sym}, skipping for now: {e}")
                    rows.append(self._row(p, "rugcheck unavailable"))
                    continue  # fail closed
                if dangers:
                    self.rejected[mint] = time.time() + 86400
                    stats["rugcheck danger"] = stats.get("rugcheck danger", 0) + 1
                    self.log(f"REJECT {sym}: {', '.join(dangers)[:120]}")
                    rows.append(self._row(p, "rug: " + ", ".join(dangers)[:60]))
                    continue
            try:
                ok = self.buy(p)
                bought += 1 if ok else 0
                rows.append(self._row(p, "BOUGHT" if ok else "skipped (impact/balance)"))
            except Exception as e:
                self.log(f"buy failed {sym}: {e}")
                self.rejected[mint] = time.time() + 600
                rows.append(self._row(p, "buy failed"))
        self.last_scan = {"t": time.strftime("%H:%M:%S"), "checked": len(cands), "bought": bought, "stats": stats, "rows": rows[:30]}
        self.log(f"scan: {len(cands)} new tokens checked, bought {bought}, rejected by: {stats or 'none'}")

    def loop(self):
        while True:
            try:
                if self.running and self.cfg["scanner_enabled"]:
                    self.manage_positions()
                    if len(self.state["positions"]) < self.cfg["max_open_positions"]:
                        self.scan_and_buy()
                    if self.cfg["mode"] == "live":
                        self.refresh_live_balance()
            except Exception as e:
                self.log(f"loop error: {e}")
                traceback.print_exc()
            time.sleep(max(5, self.cfg["scan_interval_sec"]))

    def sol_usd(self):
        return self.cfg["sol_usd"] or self.sol_usd_auto

    def price_loop(self):
        while True:
            try:
                pairs = http_get(f"{DEX}/tokens/v1/solana/{WSOL}")
                best = max((p for p in pairs if p["baseToken"]["address"] == WSOL),
                           key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
                self.sol_usd_auto = float(best["priceUsd"])
            except Exception:
                pass
            time.sleep(60)

    # ---------- views ----------
    def view(self):
        with self.lock:
            hist = self.state["history"]
            sc_hist = [h for h in hist if not str(h.get("mode", "")).startswith("launch")]
            wins = sum(1 for h in sc_hist if h["pnl_sol"] > 0)
            pos = []
            for p in self.state["positions"]:
                pos.append({**p, "pnl_pct": (p["last_price"] / p["entry_price"] - 1) * 100,
                            "value_sol": p["entry_sol"] * p["last_price"] / p["entry_price"]})
            lh = [h for h in hist if str(h.get("mode", "")).startswith("launch")]
            lwins = sum(1 for h in lh if h["pnl_sol"] > 0)
            lval = 0.0
            for lp in self.state["launch_positions"]:
                if lp.get("mode") != "live":
                    v = LAUNCH.value_of(lp)
                    lval += v if v is not None else lp["entry_sol"]
            sval = sum(p["value_sol"] for p in pos if p["mode"] == "paper")
            return {
                "cfg": self.cfg, "running": self.running, "scan": self.last_scan, "launch": LAUNCH.view(),
                "sol_usd": self.sol_usd(),
                "paper_balance": self.state["paper_balance"], "live_balance": self.live_balance,
                "positions": pos, "history": sc_hist[-100:][::-1], "logs": self.logs[-80:][::-1],
                "stats": {"trades": len(sc_hist), "win_rate": (wins / len(sc_hist) * 100) if sc_hist else 0,
                          "realized_sol": sum(h["pnl_sol"] for h in sc_hist)},
                "sniper": {"positions": LAUNCH.positions_view(), "history": lh[-40:][::-1],
                           "equity_sol": self.state["paper_balance"] + lval + sval,
                           "start_sol": self.cfg["paper_start_balance_sol"],
                           "stats": {"trades": len(lh), "win_rate": (lwins / len(lh) * 100) if lh else 0,
                                     "realized_sol": sum(h["pnl_sol"] for h in lh),
                                     "live_realized_sol": sum(h["pnl_sol"] for h in lh if h["mode"] == "launch-live")}}}

    def reset_paper(self):
        with self.lock:
            self.state = {"paper_balance": self.cfg["paper_start_balance_sol"], "positions": [], "history": [], "bought": [], "launch_positions": []}
            self.save()


FEE = 0.01  # default pump.fun fee on the bonding curve


def curve_buy(sol, vsol, vtok, fee=FEE):
    """tokens received for `sol` SOL on the pump.fun constant-product curve"""
    s = sol * (1 - fee)
    return vtok - (vsol * vtok) / (vsol + s)


def curve_sell(tok, vsol, vtok, fee=FEE):
    """SOL received for selling `tok` tokens (after fee)"""
    return (vsol - (vsol * vtok) / (vtok + tok)) * (1 - fee)


class Launch:
    """Pump.fun launch bot on the PumpDev feed.
    DEMO = simulated fills (bonding-curve math + commission + impact + delay + failed fills).
    LIVE = real trades through PumpDev's Lightning API with the key from your env file."""
    URL = "wss://pumpdev.io/ws"
    TRADE_URL = "https://pumpdev.io/api/trade-lightning"

    def __init__(self, bot):
        self.bot = bot
        self.watch, self.last = {}, {}
        self.stats = {"seen": 0, "bought": 0, "failed_fills": 0}
        self.connected = False
        self.skips = {}
        self.pending = 0
        self.consec = 0
        self.halted = False
        self.session_buys = 0
        self.events = collections.deque(maxlen=80)
        self.decisions = collections.deque(maxlen=80)
        self.ws = None
        self.new_sub = False

    # ---------------- helpers ----------------
    def pos(self):
        return self.bot.state["launch_positions"]

    def entries_on(self):
        return self.bot.running and self.bot.cfg["launch_enabled"]

    def needs_stream(self):
        # keep managing open positions (exits!) even after Stop, until they are all closed
        return self.entries_on() or len(self.pos()) > 0

    def reset_session(self):
        self.consec, self.halted, self.session_buys = 0, False, 0

    def ev(self, m):
        k = self.api_key()
        if k:
            m = m.replace(k, "***")  # never let the PumpDev key reach the log or dashboard
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "m": m})
        self.bot.log("[sniper] " + m)

    def decide(self, w, text):
        self.decisions.appendleft({"t": time.strftime("%H:%M:%S"), "sym": w["sym"], "mcap": w["mcap"],
                                   "dev": w["dev_buy"], "d": text})

    def reject(self, w, key, detail=None):
        self.skips[key] = self.skips.get(key, 0) + 1
        self.decide(w, "skip: " + (detail or key))
        return True

    # ---------------- PumpDev key (never stored in config.json, never logged) ----------------
    def env_path(self):
        p = self.bot.cfg["launch_env_path"].strip()
        return Path(p) if p else APP_DIR / "pumpdev.env"

    def api_key(self):
        try:
            vals = {}
            for line in self.env_path().read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.replace("export ", "").strip()] = v.strip().strip('"').strip("'")
        except Exception:
            return None
        for name in ("PUMPDEV_API_KEY", "PUMPDEV_LIGHTNING_KEY", "LIGHTNING_API_KEY", "PUMPDEV_KEY", "API_KEY"):
            if vals.get(name):
                return vals[name]
        for v in vals.values():
            if v.startswith("pk_"):
                return v
        return None

    def key_info(self):
        k = self.api_key()
        return {"path": str(self.env_path()), "found": bool(k), "masked": (k[:5] + "..." + k[-4:]) if k else None}

    def live_ready(self):
        c = self.bot.cfg
        return c["launch_mode"] == "live" and c["launch_live_ack"] and bool(self.api_key())

    async def live_trade(self, body):
        key = self.api_key()
        if not key:
            return None, {"error": "PumpDev key not found"}

        def post():
            try:
                r = requests.post(self.TRADE_URL, params={"api-key": key}, json=body, timeout=20)
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"error": r.text[:200].replace(key, "***")}
            except Exception as e:
                return None, {"error": str(e).replace(key, "***")}
        return await asyncio.to_thread(post)

    # ---------------- pricing ----------------
    def tokens_for(self, sol, vsol, vtok, live=False):
        c = self.bot.cfg
        fee = 0.01 if live else c["launch_curve_fee_pct"] / 100
        imp = 0.0 if live else c["launch_extra_impact_pct"] / 100
        return curve_buy(sol, vsol, vtok, fee) * (1 - imp)

    def sol_for(self, tok, vsol, vtok, live=False):
        c = self.bot.cfg
        fee = 0.01 if live else c["launch_curve_fee_pct"] / 100
        imp = 0.0 if live else c["launch_extra_impact_pct"] / 100
        return curve_sell(tok, vsol, vtok, fee) * (1 - imp) * (1 - c["launch_commission_pct"] / 100)

    def value_of(self, p):
        l = self.last.get(p["mint"])
        return self.sol_for(p["tokens"], l[0], l[1], p.get("mode") == "live") if l else None

    def delay(self):
        return self.bot.cfg["launch_latency_ms"] * random.uniform(0.6, 1.5) / 1000

    # ---------------- views ----------------
    def positions_view(self):
        out, now = [], time.time()
        for p in self.pos():
            v, l = self.value_of(p), self.last.get(p["mint"])
            e = p["entry_sol"]
            out.append({"mint": p["mint"], "symbol": p["symbol"], "mode": p.get("mode", "paper"), "entry_sol": e,
                        "entry_mcap": p.get("entry_mcap", 0), "mcap_now": (l[0] / l[1] * 1e9) if l else 0,
                        "pnl_pct": ((v / e - 1) * 100) if v is not None else 0.0,
                        "pnl_sol": (v - e) if v is not None else 0.0, "age": now - p["opened_at"],
                        "hist": [round((x / e - 1) * 100, 2) for x in p.get("hist", [])[-90:]],
                        "stuck": bool(p.get("stuck"))})
        return out

    def watching_view(self):
        now, out = time.time(), []
        for m, w in list(self.watch.items()):
            if w.get("decided"):
                continue
            out.append({"sym": w["sym"], "age": now - w["created"], "buyers": len(w["buyers"]),
                        "net": w["buy_sol"] - w["sell_sol"], "mcap": w["mcap"],
                        "gain": ((w["mcap"] / w["mcap0"] - 1) * 100) if w["mcap0"] else 0})
        return out[:15]

    def view(self):
        return {"enabled": self.bot.cfg["launch_enabled"], "connected": self.connected, "stats": self.stats,
                "skips": self.skips, "events": list(self.events)[:40], "decisions": list(self.decisions)[:40],
                "watching": self.watching_view(), "halted": self.halted, "consec": self.consec,
                "key": self.key_info(), "live_spent": self.bot.state.get("launch_live_spent", 0.0)}

    async def send(self, **m):
        try:
            if self.ws:
                await self.ws.send(json.dumps(m))
        except Exception:
            pass

    async def drop(self, mint):
        self.watch.pop(mint, None)
        if not any(p["mint"] == mint for p in self.pos()):
            self.last.pop(mint, None)
            await self.send(method="unsubscribeTokenTrade", keys=[mint])

    # ---------------- stream ----------------
    async def run(self):
        try:
            import ssl
            import certifi
            import websockets
        except ImportError:
            self.bot.log("sniper needs the 'websockets' package")
            return
        ctx = ssl.create_default_context(cafile=certifi.where())
        while True:
            if not self.needs_stream():
                await asyncio.sleep(2)
                continue
            try:
                async with websockets.connect(self.URL, ssl=ctx, ping_interval=20, max_size=2 ** 22) as ws:
                    self.ws, self.connected, self.new_sub = ws, True, False
                    keys = [p["mint"] for p in self.pos()]
                    for i in range(0, len(keys), 100):
                        await self.send(method="subscribeTokenTrade", keys=keys[i:i + 100])
                    self.ev("connected to PumpDev feed")
                    tick = asyncio.create_task(self.ticker())
                    try:
                        async for raw in ws:
                            if not self.needs_stream():
                                break
                            try:
                                await self.on_msg(json.loads(raw))
                            except Exception as e:
                                self.bot.log(f"sniper msg error: {e}")
                    finally:
                        tick.cancel()
            except Exception as e:
                self.bot.log(f"sniper stream error: {e}")
            self.ws, self.connected = None, False
            self.watch.clear()
            await asyncio.sleep(3)

    async def on_msg(self, m):
        mint, tx = m.get("mint"), m.get("txType")
        if not mint or "vSolInBondingCurve" not in m:
            return
        c = self.bot.cfg
        vsol, vtok, now = float(m["vSolInBondingCurve"]), float(m["vTokensInBondingCurve"]), time.time()
        if tx == "create":
            if not self.entries_on():
                return
            self.stats["seen"] += 1
            mc = float(m.get("marketCapSol") or 0)
            w = {"sym": m.get("symbol", "?"), "name": m.get("name", ""), "created": now, "dev": m.get("traderPublicKey"),
                 "buyers": set(), "buy_sol": 0.0, "sell_sol": 0.0, "dev_sold": False, "mcap": mc, "mcap0": mc,
                 "dev_buy": float(m.get("solAmount") or 0)}
            if c["launch_strategy"] == "snipe":
                if self.snipe_check(w):
                    return
                self.decide(w, "MATCH")
                w["decided"] = True
                self.watch[mint] = w
                self.last[mint] = (vsol, vtok, now)
                self.pending += 1
                await self.send(method="subscribeTokenTrade", keys=[mint])
                asyncio.create_task(self.try_buy(mint, w))
                return
            if len(self.watch) >= c["launch_max_watch"]:
                self.reject(w, "watch list full")
                return
            self.watch[mint] = w
            self.last[mint] = (vsol, vtok, now)
            await self.send(method="subscribeTokenTrade", keys=[mint])
            return
        w = self.watch.get(mint)
        held = [p for p in self.pos() if p["mint"] == mint]
        if not w and not held:
            return
        self.last[mint] = (vsol, vtok, now)
        who, sol = m.get("traderPublicKey"), float(m.get("solAmount") or 0)
        if w:
            w["mcap"] = float(m.get("marketCapSol") or w["mcap"])
            if who == w["dev"]:
                if tx == "sell":
                    w["dev_sold"] = True
            elif tx == "buy":
                w["buyers"].add(who)
                w["buy_sol"] += sol
            elif tx == "sell":
                w["sell_sol"] += sol
        for p in held:
            if tx == "sell" and who == p.get("dev"):
                p["dev_sold"] = True

    # ---------------- entry rules ----------------
    def gate(self, w):
        c = self.bot.cfg
        if self.halted:
            return self.reject(w, "halted (loss streak)")
        if not c["launch_yolo_mode"] and self.session_buys + self.pending >= 1:
            return self.reject(w, "yolo off (one token only)")
        if len(self.pos()) + self.pending >= c["launch_max_positions"]:
            return self.reject(w, "max positions", f"max positions ({c['launch_max_positions']}) reached")
        ms = c["launch_match_string"].strip().lower()
        if ms and ms not in (w["sym"] + " " + w["name"]).lower():
            return self.reject(w, "no match string")
        ca = c["launch_creator_address"].strip()
        if ca and ca != w["dev"]:
            return self.reject(w, "other creator")
        if c["launch_max_token_age_sec"] and time.time() - w["created"] > c["launch_max_token_age_sec"]:
            return self.reject(w, "token too old")
        return False

    def dev_check(self, w):
        c = self.bot.cfg
        if w["dev_buy"] < c["launch_min_dev_buy_sol"]:
            return self.reject(w, "dev buy too small", f"dev buy {w['dev_buy']:.2f} < {c['launch_min_dev_buy_sol']:g}")
        if c["launch_max_dev_buy_sol"] and w["dev_buy"] > c["launch_max_dev_buy_sol"]:
            return self.reject(w, "dev buy too big", f"dev buy {w['dev_buy']:.2f} > {c['launch_max_dev_buy_sol']:g}")
        return False

    def snipe_check(self, w):
        c = self.bot.cfg
        if self.gate(w) or self.dev_check(w):
            return True
        if w["mcap"] > c["launch_max_mcap_sol"]:
            return self.reject(w, "mcap too high", f"mcap {w['mcap']:.1f} > {c['launch_max_mcap_sol']:g}")
        return False

    def evaluate(self, w):
        c = self.bot.cfg
        if self.gate(w) or self.dev_check(w):
            return True
        if w["dev_sold"]:
            return self.reject(w, "dev sold")
        if len(w["buyers"]) < c["launch_min_buyers"]:
            return self.reject(w, "too few buyers", f"buyers {len(w['buyers'])} < {c['launch_min_buyers']}")
        net = w["buy_sol"] - w["sell_sol"]
        if net < c["launch_min_net_buy_sol"]:
            return self.reject(w, "net buying too low", f"net buy {net:.1f} < {c['launch_min_net_buy_sol']:g} SOL")
        gain = (w["mcap"] / w["mcap0"] - 1) * 100 if w["mcap0"] else 0
        if gain < c["launch_min_mcap_gain_pct"]:
            return self.reject(w, "mcap gain too small", f"mcap gain {gain:.0f}% < {c['launch_min_mcap_gain_pct']:g}%")
        if w["mcap"] > c["launch_max_entry_mcap_sol"]:
            return self.reject(w, "mcap too high", f"mcap {w['mcap']:.1f} > {c['launch_max_entry_mcap_sol']:g}")
        if w["buy_sol"] > 0 and w["sell_sol"] / w["buy_sol"] > c["launch_max_sell_ratio"]:
            return self.reject(w, "too many sells", f"sell ratio {w['sell_sol'] / w['buy_sol']:.2f} > {c['launch_max_sell_ratio']:g}")
        return False

    async def ticker(self):
        while True:
            await asyncio.sleep(1)
            c, now = self.bot.cfg, time.time()
            want = self.entries_on()
            if want != self.new_sub:
                await self.send(method="subscribeNewToken" if want else "unsubscribeNewToken")
                self.new_sub = want
                if not want:
                    for m in list(self.watch):
                        await self.drop(m)
            for mint, w in list(self.watch.items()):
                if w.get("decided") or now - w["created"] < c["launch_watch_sec"]:
                    continue
                w["decided"] = True
                if self.evaluate(w):
                    await self.drop(mint)
                else:
                    self.decide(w, "MATCH")
                    self.pending += 1
                    asyncio.create_task(self.try_buy(mint, w))
            for p in list(self.pos()):
                v = self.value_of(p)
                if v is not None:
                    h = p.setdefault("hist", [])
                    h.append(round(v, 6))
                    del h[:-90]
                if p.get("selling") or (p.get("stuck") and not p.get("force_close")):
                    continue
                reason = self.exit_reason(p, now)
                if reason:
                    p["selling"] = True
                    asyncio.create_task(self.do_sell(p, reason))

    # ---------------- exit rules ----------------
    def exit_reason(self, p, now):
        c = self.bot.cfg
        if p.get("force_close"):
            return "manual close"
        if c["launch_marry_mode"] or c["launch_exit_strategy"] == "manual":
            return None
        age = now - p["opened_at"]
        hold = c["launch_max_hold_sec"]
        v = self.value_of(p)
        if v is None:
            if age > (hold or 300):
                return "time exit (no price data)" if p.get("mode") == "live" else "no data (written off)"
            return None
        p["peak_value"] = max(p.get("peak_value", v), v)
        entry = p["entry_sol"]
        pnl = (v / entry - 1) * 100
        if c["launch_exit_on_dev_sell"] and p.get("dev_sold"):
            return "dev sold"
        if c["launch_exit_strategy"] == "tp_sl":
            if c["launch_take_profit_pct"] and pnl >= c["launch_take_profit_pct"]:
                return f"take profit {1 + pnl / 100:.2f}x"
            if c["launch_stop_loss_pct"] and pnl <= -c["launch_stop_loss_pct"]:
                return f"stop loss {1 + pnl / 100:.2f}x"
            if c["launch_trail_pct"] and p["peak_value"] >= c["launch_trail_start_x"] * entry and \
                    v <= p["peak_value"] * (1 - c["launch_trail_pct"] / 100):
                return "trailing stop"
            if c["launch_stall_sec"] and age >= c["launch_stall_sec"] and p["peak_value"] < c["launch_stall_min_x"] * entry:
                return "stalled"
        if hold and age >= hold:
            return "time exit"
        return None

    # ---------------- fills ----------------
    async def try_buy(self, mint, w):
        c, sym = self.bot.cfg, w["sym"]
        live = c["launch_mode"] == "live"
        try:
            l0 = self.last.get(mint)
            if not l0:
                return
            p0 = l0[0] / l0[1]
            sol, tip = min(c["launch_buy_sol"], c["safety_cap_sol"]), c["launch_tip_sol"]
            comm = sol * c["launch_commission_pct"] / 100
            if live:
                if not self.live_ready():
                    self.ev("LIVE buy blocked: key file / confirmation missing")
                    return
                spent, cap = self.bot.state.get("launch_live_spent", 0.0), c["launch_max_total_spend_sol"]
                if cap and spent + sol > cap:
                    self.skip_key("max total spend reached")
                    return
                code, res = await self.live_trade({"action": "buy", "mint": mint, "amount": sol, "denominatedInSol": "true",
                                                   "slippage": c["launch_max_slippage_pct"], "priorityFee": tip})
                sig = (res or {}).get("signature")
                if not sig:
                    self.stats["failed_fills"] += 1
                    self.ev(f"LIVE BUY FAILED {sym}: {str((res or {}).get('error') or code)[:140]}")
                    return
                l = self.last.get(mint) or l0
                vsol, vtok = l[0], l[1]
                with self.bot.lock:
                    self.bot.state["launch_live_spent"] = spent + sol
                    self.pos().append({"mint": mint, "symbol": sym, "dev": w["dev"], "mode": "live", "sig": sig,
                                       "tokens": self.tokens_for(sol, vsol, vtok, True), "entry_sol": sol,
                                       "entry_mcap": vsol / vtok * 1e9, "tip_in": tip, "comm_in": comm,
                                       "peak_value": 0.0, "opened_at": time.time()})
                    self.bot.save()
            else:
                await asyncio.sleep(self.delay())
                if random.random() < c["launch_fail_pct"] / 100:
                    self.stats["failed_fills"] += 1
                    self.ev(f"FAILED FILL {sym} (simulated: outbid / tx dropped)")
                    return
                l = self.last.get(mint)
                if not l:
                    return
                vsol, vtok = l[0], l[1]
                if (vsol / vtok / p0 - 1) * 100 > c["launch_max_slippage_pct"]:
                    self.skip_key("price ran away")
                    return
                with self.bot.lock:
                    if self.bot.state["paper_balance"] < sol + comm + tip:
                        self.skip_key("no paper balance")
                        return
                    self.bot.state["paper_balance"] -= sol + comm + tip
                    self.pos().append({"mint": mint, "symbol": sym, "dev": w["dev"], "mode": "paper",
                                       "tokens": self.tokens_for(sol, vsol, vtok), "entry_sol": sol,
                                       "entry_mcap": vsol / vtok * 1e9, "tip_in": tip, "comm_in": comm,
                                       "peak_value": 0.0, "opened_at": time.time()})
                    self.bot.save()
            self.stats["bought"] += 1
            self.session_buys += 1
            self.ev(f"BUY [{'LIVE' if live else 'demo'}] {sym} {sol} SOL @ mcap {vsol / vtok * 1e9:.1f} ({c['launch_strategy']})")
        finally:
            self.pending -= 1
            await self.drop(mint)

    def skip_key(self, key):
        self.skips[key] = self.skips.get(key, 0) + 1

    async def do_sell(self, p, reason):
        c = self.bot.cfg
        live = p.get("mode") == "live"
        if live:
            code, res = await self.live_trade({"action": "sell", "mint": p["mint"], "amount": "100%",
                                               "denominatedInSol": "false", "slippage": c["launch_max_slippage_pct"],
                                               "priorityFee": c["launch_tip_sol"]})
            if not (res or {}).get("signature"):
                p["selling"] = False
                p["sell_fails"] = p.get("sell_fails", 0) + 1
                self.ev(f"LIVE SELL FAILED {p['symbol']} ({p['sell_fails']}): {str((res or {}).get('error') or code)[:140]}")
                if p["sell_fails"] >= 5:
                    p["stuck"], p["force_close"] = True, False
                    self.ev(f"{p['symbol']} STUCK after 5 failed sells - sell it manually in your wallet / on pump.fun")
                return
            l = self.last.get(p["mint"])
            out = self.sol_for(p["tokens"], l[0], l[1], True) if l else 0.0
            proceeds = max(out - c["launch_tip_sol"], 0.0)
        else:
            await asyncio.sleep(self.delay())
            written_off = reason.startswith("no data")
            if not written_off and random.random() < c["launch_sell_fail_pct"] / 100:
                p["selling"] = False
                self.ev(f"sell failed {p['symbol']} (simulated), retrying")
                return
            l = self.last.get(p["mint"])
            out = 0.0 if (written_off or not l) else self.sol_for(p["tokens"], l[0], l[1])
            proceeds = max(out - c["launch_tip_sol"], 0.0) if out > 0 else 0.0
        cost = p["entry_sol"] + p.get("comm_in", 0.0) + p["tip_in"]
        pnl = proceeds - cost
        with self.bot.lock:
            if not live:
                self.bot.state["paper_balance"] += proceeds
            self.bot.state["launch_positions"] = [x for x in self.pos() if x is not p]
            self.bot.state["history"].append({
                "symbol": p["symbol"], "mint": p["mint"], "mode": "launch-live" if live else "launch-paper",
                "reason": reason, "entry_sol": cost, "exit_sol": proceeds, "pnl_sol": pnl,
                "opened_at": p["opened_at"], "closed_at": time.time()})
            self.bot.state["history"] = self.bot.state["history"][-500:]
            self.bot.save()
        self.consec = self.consec + 1 if pnl < 0 else 0
        lim = c["launch_max_consec_losses"]
        self.ev(f"SELL [{'LIVE' if live else 'demo'}] {p['symbol']} {reason} ({pnl:+.4f} SOL{' est.' if live else ''})")
        if lim and self.consec >= lim and not self.halted:
            self.halted = True
            self.ev(f"HALTED: {self.consec} losing trades in a row - press Resume to continue")
        await self.drop(p["mint"])


BOT = Bot()
LAUNCH = Launch(BOT)
TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _authed(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "mb_token" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path == "/":
            t = parse_qs(u.query).get("t", [""])[0]
            if t and secrets.compare_digest(t, TOKEN):
                return self._send(302, "", "text/plain", {"Location": "/", "Set-Cookie": f"mb_token={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
            if self._authed():
                return self._send(200, res("index.html").read_bytes(), "text/html; charset=utf-8")
            return self._send(403, "Open MemeBot using its launcher / desktop shortcut.", "text/plain")
        if u.path == "/api/state" and self._authed():
            return self._send(200, BOT.view())
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not (self._host_ok() and self._authed()):
            return self._send(403, "forbidden", "text/plain")
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._send(415, "json only", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        path = urlparse(self.path).path
        if path == "/api/config":
            BOT.update_config(body)
        elif path == "/api/launch_sell_all":
            for p in BOT.state.get("launch_positions", []):
                p["force_close"], p["stuck"], p["sell_fails"] = True, False, 0
            LAUNCH.ev("SELL ALL requested")
        elif path == "/api/launch_reset_spent":
            BOT.state["launch_live_spent"] = 0.0
            BOT.save()
            LAUNCH.ev("live spend counter reset")
        elif path == "/api/test_key":
            return self._send(200, LAUNCH.key_info())
        elif path == "/api/launch_resume":
            LAUNCH.reset_session()
            LAUNCH.ev("resumed")
        elif path == "/api/start":
            LAUNCH.reset_session()
            BOT.running = True
            BOT.log(f"Bot STARTED in {BOT.cfg['mode']} mode")
        elif path == "/api/stop":
            BOT.running = False
            BOT.log("Bot STOPPED - no new entries. Open sniper positions keep being managed until closed; DEX scanner positions are not.")
        elif path == "/api/close":
            for p in list(BOT.state["positions"]):
                if p["mint"] == body.get("mint"):
                    BOT.sell(p, "manual close")
            for p in BOT.state.get("launch_positions", []):
                if p["mint"] == body.get("mint"):
                    p["force_close"], p["stuck"], p["sell_fails"] = True, False, 0
        elif path == "/api/reset_paper":
            BOT.reset_paper()
        elif path == "/api/test_wallet":
            try:
                kp = BOT.keypair()
                BOT.refresh_live_balance()
                return self._send(200, {"ok": True, "address": str(kp.pubkey()), "balance": BOT.live_balance})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        else:
            return self._send(404, "not found", "text/plain")
        self._send(200, {"ok": True})


def main():
    global TOKEN
    if TOKEN_PATH.exists():
        TOKEN = TOKEN_PATH.read_text().strip()
    if not TOKEN:
        TOKEN = secrets.token_urlsafe(32)
        TOKEN_PATH.write_text(TOKEN)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is busy - is MemeBot already running?")
        input("Press Enter to close")
        return
    threading.Thread(target=BOT.loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(LAUNCH.run()), daemon=True).start()
    threading.Thread(target=BOT.price_loop, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    print(f"MemeBot running (local only). Dashboard: {url}\nClose this window to quit.")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
