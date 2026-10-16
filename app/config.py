import os

from dotenv import load_dotenv

load_dotenv()

# "mock" = simulated prices (works instantly, no account needed)
# "angel" = live data from Angel One SmartAPI
FEED = os.getenv("FEED", "mock").strip().lower()

# Chart times are sent as "IST pretending to be UTC" so the chart shows Indian time.
IST_OFFSET = 19800

DEFAULT_TOKEN = "99926000"  # Nifty 50, shown when the page first opens
DEFAULT_TF = "5"
SNAPSHOT_BARS = 2500        # candles sent to the browser at a time
MAX_ACTIVE = 25             # stocks kept open at once (live feed + candles in memory)

# One entry per timeframe. The key is what the browser sends.
#   seconds     candle size
#   angel       Angel One's name for that interval
#   chunk_days  how many days of history one Angel One request asks for (their limit per interval)
#   mock_bars   candles per chunk in demo mode
#   tol         how close (as a fraction of price) swing points must be to count as one level
#   lookback    how many recent candles are used to find support/resistance
TIMEFRAMES = {
    "1":  {"label": "1m",  "seconds": 60,    "angel": "ONE_MINUTE",     "chunk_days": 30,   "mock_bars": 4000, "tol": 0.0015, "lookback": 1200},
    "5":  {"label": "5m",  "seconds": 300,   "angel": "FIVE_MINUTE",    "chunk_days": 100,  "mock_bars": 3000, "tol": 0.002,  "lookback": 1500},
    "15": {"label": "15m", "seconds": 900,   "angel": "FIFTEEN_MINUTE", "chunk_days": 200,  "mock_bars": 2000, "tol": 0.003,  "lookback": 1500},
    "60": {"label": "1h",  "seconds": 3600,  "angel": "ONE_HOUR",       "chunk_days": 400,  "mock_bars": 1500, "tol": 0.005,  "lookback": 1200},
    "D":  {"label": "1D",  "seconds": 86400, "angel": "ONE_DAY",        "chunk_days": 2000, "mock_bars": 1500, "tol": 0.008,  "lookback": 500},
}

# ---------- paper trading (dummy money, never sends real orders; see app/paper.py) ----------
PAPER_START_CASH = float(os.getenv("PAPER_START_CASH", "10000"))
# Angel One tokens of the stocks the bot may trade: RELIANCE, TCS, INFY, HDFCBANK, SBIN
PAPER_WATCHLIST = [t.strip() for t in os.getenv("PAPER_WATCHLIST", "2885,11536,1594,1333,3045").split(",") if t.strip()]
PAPER_TF = "5"              # candles the bot looks at
PAPER_RISK_PCT = 1.0        # account % lost if a trade hits its stop
PAPER_MAX_POSITIONS = 2
PAPER_FEE_PCT = 0.05        # rough brokerage + taxes, charged on each buy and each sell
PAPER_DAILY_LOSS_PCT = 3.0  # no new trades for the day after losing this much
PAPER_COOLDOWN_MIN = 15     # wait after selling a stock before buying it again

# Used only if the full instrument list cannot be downloaded.
FALLBACK_INSTRUMENTS = [
    {"token": "99926000", "symbol": "Nifty 50",    "name": "Nifty 50",   "exchange": "NSE", "exchange_type": 1, "kind": "INDEX"},
    {"token": "99926009", "symbol": "Nifty Bank",  "name": "Nifty Bank", "exchange": "NSE", "exchange_type": 1, "kind": "INDEX"},
    {"token": "2885",     "symbol": "RELIANCE-EQ", "name": "RELIANCE",   "exchange": "NSE", "exchange_type": 1, "kind": "EQ"},
    {"token": "11536",    "symbol": "TCS-EQ",      "name": "TCS",        "exchange": "NSE", "exchange_type": 1, "kind": "EQ"},
    {"token": "1594",     "symbol": "INFY-EQ",     "name": "INFY",       "exchange": "NSE", "exchange_type": 1, "kind": "EQ"},
    {"token": "1333",     "symbol": "HDFCBANK-EQ", "name": "HDFCBANK",   "exchange": "NSE", "exchange_type": 1, "kind": "EQ"},
    {"token": "3045",     "symbol": "SBIN-EQ",     "name": "SBIN",       "exchange": "NSE", "exchange_type": 1, "kind": "EQ"},
]
