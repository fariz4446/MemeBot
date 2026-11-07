# MemeBot

A personal, local-only Solana meme-coin trading bot for Windows with a browser dashboard.
It has two engines, a **Pump.fun launch sniper** and a **DexScreener/Jupiter scanner**.
Both default to simulated trading. Nothing is hosted, there are no accounts or subscriptions, and
the dashboard is only reachable from the PC it runs on.

![Dashboard](docs/dashboard-preview.png)

> **Not financial advice. Meme coins are extremely risky, and most launch tokens go to zero within minutes.**
> Demo (paper) results are always more optimistic than real trading: they cannot fully model
> faster competing bots, failed transactions, order delay or rug-pull slippage. Test in DEMO
> for a long time, and start LIVE with tiny amounts you can afford to lose. This software comes with no warranty.

## What it does

### 1. Pump.fun sniper (the SNIPER tab)
* Streams new Pump.fun launches in real time from the PumpDev WebSocket feed (`wss://pumpdev.io/ws`).
* **Strategies:** `snipe` (buy at launch, filtered on dev buy and market cap) or `momentum`
  (watch each token first, buy only on buyer count, net buying, market-cap gain and sell ratio).
* **Exits:** take profit, stop loss, timeout, trailing stop, stall exit, optional exit when the dev sells,
  "marry mode" (never auto-sell), a loss-streak circuit breaker and a yolo toggle.
* **DEMO mode:** simulated fills using the Pump.fun bonding-curve formula (constant product) plus
  commission, price impact, fill delay and failed fills. No money is touched.
* **LIVE mode:** real trades through the PumpDev Lightning API (`/api/trade-lightning`) with your own key.
  Guard rails: typed confirmation, a hard cap per trade, a **max total spend** cap, a loss-streak halt,
  a **Sell all** button, and positions keep being managed after you press Stop until they are closed.
* Dashboard: open positions with live charts, watch list, a decisions table showing why every launch
  was bought or skipped, closed trades and a log.

### 2. DEX scanner (the SCANNER tab)
* Finds new Solana tokens through DexScreener, filters them (liquidity, volume, market cap, age),
  rejects anything RugCheck flags as dangerous, and trades through real Jupiter quotes.
* Demo uses real quotes for fills. Live signs swaps locally with your own keypair file.

Both engines have presets and settings in the SETTINGS tab. The Sniper presets are
**Sniper**, **Sniper + changes** and **Momentum**.

## Download and run

1. Install **Python 3.10+** from python.org (tick "Add Python to PATH").
2. Clone or download this repo.
3. Double-click **`build.bat`**. It creates `dist\MemeBot\MemeBot.exe`.
4. Optional installer: install [Inno Setup](https://jrsoftware.org/isinfo.php), open `installer.iss` and press Compile.
5. Run `MemeBot.exe`. A console window opens and your browser loads the dashboard at `http://127.0.0.1:8787`.
   Close the console window to quit.

You can also run it without building: `pip install -r requirements.txt` then `python memebot.py`.

## Using LIVE mode (sniper)

1. Create or fund a Lightning wallet at [pumpdev.io](https://pumpdev.io). The trading key starts with `pk_`.
2. Put the key in a text file (for example `pumpdev.env`) as `PUMPDEV_API_KEY=pk_...`. If you already have
   a `pumpbot.env` for another bot, you can point MemeBot at it instead.
3. In **SETTINGS > Pump.fun sniper**, set the key file path, press **Check PumpDev key**, and set
   **max total SOL to spend** and the buy amount to small numbers.
4. Switch the mode to **LIVE**, Save, and type the confirmation when asked.
5. The key is only read from your file. It is never written to `config.json`, never shown in full
   and never put in the log.

Live profit/loss on the dashboard is **estimated** from the bonding-curve formula. Your PumpDev wallet is the real source of truth.

## Security notes
* The dashboard binds to `127.0.0.1` only and needs a random token cookie created on first run.
  It rejects requests with a foreign `Host` header.
* **Never commit** key files, `config.json`, `state.json` or wallet files. The included `.gitignore` excludes them.
* Settings and state are stored in `%APPDATA%\MemeBot`.

## Data sources
PumpDev (Pump.fun feed + Lightning trades), DexScreener (discovery and prices), RugCheck (risk flags),
Jupiter (quotes and swaps), Solana RPC. Check each service's terms and rate limits.

## Project layout
| File | Purpose |
|---|---|
| `memebot.py` | Bots and the local web server |
| `index.html` | Dashboard |
| `requirements.txt` | `requests`, `solders`, `websockets` |
| `build.bat` | Builds `MemeBot.exe` with PyInstaller |
| `installer.iss` | Inno Setup script for a Windows installer |

## Status
Experimental hobby project. The demo engine is covered by simulated-feed tests, but LIVE mode has **not**
been verified against the real PumpDev trading API by the author of this README, so
start with very small amounts and watch the log.
