from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, paper
from .candles import CandleStore
from .instruments import Instruments
from .levels import compute_levels
from .optionfinder import find_option

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("platform")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PAGE = config.SNAPSHOT_BARS

instruments = Instruments()
provider = None  # MockProvider or AngelProvider, set at startup

# one CandleStore per instrument that is open (token -> store), least recently used first
stores: "OrderedDict[str, CandleStore]" = OrderedDict()
# token -> {websocket: timeframe key}
subscribers: Dict[str, Dict[WebSocket, str]] = {}
locks: Dict[Tuple[str, str], asyncio.Lock] = {}
# token -> time of its last live tick (IST-shifted epoch seconds)
last_tick: Dict[str, float] = {}

account = paper.Account()
history_retry: Dict[str, float] = {}  # token -> when the paper bot may retry loading its history


def paper_pinned() -> set:
    """Stocks the paper account needs kept open even when no chart shows them."""
    return set(config.PAPER_WATCHLIST) | set(account.s["positions"])


def public(inst: dict) -> dict:
    return {k: inst[k] for k in ("token", "symbol", "name", "exchange", "kind")}


def lock_for(token: str, tf: str) -> asyncio.Lock:
    return locks.setdefault((token, tf), asyncio.Lock())


def activate(inst: dict) -> CandleStore:
    """Get (or create) the candle store for an instrument and start its live feed."""
    token = inst["token"]
    store = stores.get(token)
    if store is None:
        store = CandleStore()
        stores[token] = store
        provider.subscribe(inst)
    stores.move_to_end(token)
    while len(stores) > config.MAX_ACTIVE:
        pinned = paper_pinned()
        victim = next((t for t in stores if t != token and not subscribers.get(t) and t not in pinned), None)
        if victim is None:
            break
        stores.pop(victim)
        provider.unsubscribe(instruments.get(victim))
        for key in [k for k in locks if k[0] == victim]:
            locks.pop(key, None)
    return store


async def ensure_history(inst: dict, tf: str) -> Optional[str]:
    """Load the newest chunk of history for this timeframe. Returns an error message or None."""
    store = activate(inst)
    async with lock_for(inst["token"], tf):
        if store.loaded[tf]:
            return None
        try:
            candles = await asyncio.to_thread(provider.fetch_chunk, inst, tf, None)
        except Exception as exc:
            log.warning("history failed for %s %s: %s", inst["symbol"], tf, exc)
            return str(exc) or exc.__class__.__name__
        store.merge(tf, candles)
        store.loaded[tf] = True
        if not candles:
            store.exhausted[tf] = True
        return None


async def load_older(inst: dict, tf: str, before: int):
    """Candles older than `before`. Fetches another chunk from the provider when needed."""
    store = activate(inst)
    err = None
    async with lock_for(inst["token"], tf):
        older = [c for c in store.data[tf] if c["time"] < before]
        if len(older) < PAGE // 2 and not store.exhausted[tf]:
            series = store.data[tf]
            end_ts = series[0]["time"] if series else None
            try:
                candles = await asyncio.to_thread(provider.fetch_chunk, inst, tf, end_ts)
                store.merge(tf, candles)
                if not candles:
                    store.exhausted[tf] = True
            except Exception as exc:
                log.warning("older history failed for %s %s: %s", inst["symbol"], tf, exc)
                err = str(exc) or exc.__class__.__name__
            older = [c for c in store.data[tf] if c["time"] < before]
        page = older[-PAGE:]
        exhausted = store.exhausted[tf] and len(older) <= len(page)
    return page, exhausted, err


def levels_payload(token: str, tf: str) -> dict:
    store = stores.get(token)
    series = store.data[tf] if store else []
    if not series:
        return {"levels": [], "pivots": None, "atr": None}
    cfg = config.TIMEFRAMES[tf]
    today = int((time.time() + config.IST_OFFSET) // 86400)
    return compute_levels(
        series, series[-1]["close"], tol=cfg["tol"], lookback=cfg["lookback"], today_day=today
    )


def latest_price(token: str) -> Optional[float]:
    """Most recent price we have for an open instrument."""
    store = stores.get(token)
    if store is None:
        return None
    best_time, best = -1, None
    for series in store.data.values():
        if series and series[-1]["time"] > best_time:
            best_time, best = series[-1]["time"], series[-1]["close"]
    return best


def fresh_price(token: str) -> Optional[float]:
    """Latest price, but only if it is live (a tick in the last 2 minutes). Paper trades fill at this."""
    if config.FEED != "angel":
        return latest_price(token)
    if paper.ist_now() - last_tick.get(token, 0) > 120:
        return None
    return latest_price(token)


def ist_today() -> date:
    return datetime.fromtimestamp(time.time() + config.IST_OFFSET, timezone.utc).date()


async def consume(queue: asyncio.Queue) -> None:
    """Take ticks from the feed, update candles, push changes to browsers."""
    while True:
        token, ts, price = await queue.get()
        store = stores.get(token)
        if store is None:
            continue
        last_tick[token] = ts
        updated = store.add_tick(ts, price)
        subs = subscribers.get(token, {})
        for ws, tf in list(subs.items()):
            candle = updated.get(tf)
            if candle is None:
                continue
            try:
                await ws.send_json({"type": "candle", "candle": candle})
            except Exception:
                subs.pop(ws, None)


async def levels_loop() -> None:
    """Re-calculate support/resistance every few seconds for watched charts."""
    while True:
        await asyncio.sleep(5)
        for token, subs in list(subscribers.items()):
            if not subs:
                continue
            cache: Dict[str, dict] = {}
            for ws, tf in list(subs.items()):
                if tf not in cache:
                    cache[tf] = levels_payload(token, tf)
                try:
                    await ws.send_json({"type": "levels", **cache[tf]})
                except Exception:
                    subs.pop(ws, None)


async def paper_step() -> None:
    now = paper.ist_now()
    live = config.FEED == "angel"
    tf = config.PAPER_TF

    # keep the watched stocks streaming and their history loaded
    for token in paper_pinned():
        inst = instruments.get(token)
        if inst is None:
            continue
        store = activate(inst)
        if not store.loaded[tf] and now >= history_retry.get(token, 0):
            history_retry[token] = now + 60
            await ensure_history(inst, tf)

    # manage open trades: stop, target, break-even, end of day
    for token, p in list(account.s["positions"].items()):
        price = fresh_price(token)
        if price is None:
            continue
        reason = account.exit_reason(p, price, now, live)
        if reason:
            account.sell(token, price, reason)

    if account.entry_block(now, live):
        return
    equity = account.s["cash"] + sum(
        (latest_price(t) or p["entry"]) * p["qty"] for t, p in account.s["positions"].items()
    )
    for token in config.PAPER_WATCHLIST:
        inst = instruments.get(token)
        store = stores.get(token)
        if inst is None or store is None or token in account.s["positions"] or account.cooling(token, now):
            continue
        price = fresh_price(token)
        if price is None or not store.data[tf]:
            continue
        lv = levels_payload(token, tf)
        sig = paper.entry_signal(store.data[tf], lv["levels"], lv["atr"])
        if not sig:
            continue
        qty = account.size(price, sig["stop"], equity)
        if qty < 1:
            continue
        account.buy(inst, qty, price, sig["why"], sig["stop"], sig["target"])
        if account.entry_block(now, live):
            return


async def paper_loop() -> None:
    """The paper trading bot. Checks every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            await paper_step()
        except Exception:
            log.exception("paper trading step failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global provider
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def push(token: str, ts: float, price: float) -> None:
        # safe to call from any thread (Angel One's WebSocket runs in its own thread)
        loop.call_soon_threadsafe(queue.put_nowait, (token, ts, price))

    tasks = []
    if config.FEED == "angel":
        from .feeds.angel import AngelProvider

        provider = AngelProvider()
        log.info("Loading the stock list from Angel One...")
        await asyncio.to_thread(instruments.load_angel)
        log.info("Logging in to Angel One...")
        await asyncio.to_thread(provider.login)
        provider.start_stream(push)
    else:
        from .feeds.mock import MockProvider, build_mock_instruments

        log.info("Starting DEMO feed with simulated prices")
        instruments.load(build_mock_instruments())
        provider = MockProvider()
        tasks.append(asyncio.create_task(provider.run(push)))

    tasks.append(asyncio.create_task(consume(queue)))
    tasks.append(asyncio.create_task(levels_loop()))
    tasks.append(asyncio.create_task(paper_loop()))
    log.info("Ready: open http://127.0.0.1:8000")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Swing Chart Platform", lifespan=lifespan)


@app.get("/api/config")
def get_config() -> dict:
    default = instruments.get(config.DEFAULT_TOKEN) or (instruments.items[0] if instruments.items else None)
    return {
        "feed": config.FEED,
        "timeframes": [{"key": k, "label": v["label"]} for k, v in config.TIMEFRAMES.items()],
        "default_tf": config.DEFAULT_TF,
        "default": public(default) if default else None,
        "search_limited": instruments.limited,
        "instrument_count": len(instruments.items),
    }


@app.get("/api/search")
def search(q: str = "") -> list:
    return [public(i) for i in instruments.search(q)]


@app.get("/api/history")
async def history(token: str, tf: str, before: int):
    inst = instruments.get(token)
    if inst is None or tf not in config.TIMEFRAMES:
        return JSONResponse({"error": "unknown instrument or timeframe"}, status_code=400)
    page, exhausted, err = await load_older(inst, tf, before)
    return {"candles": page, "exhausted": exhausted, "error": err}


@app.get("/api/option")
async def option(
    token: str, side: str, entry: float, stop: float, t1: float, t2: Optional[float] = None,
    capital: float = 100000, risk: float = 1.0,
) -> dict:
    """Find an option contract for a trade setup. side is "bull" (CALL) or "bear" (PUT)."""
    inst = instruments.get(token)
    if inst is None or side not in ("bull", "bear"):
        return {"ok": False, "message": "Unknown instrument."}
    spot = latest_price(token)
    if not spot:
        return {"ok": False, "message": "No price yet for this chart. Wait a moment and try again."}
    if not (entry > 0 and stop > 0 and t1 > 0 and capital > 0 and 0 < risk <= 10):
        return {"ok": False, "message": "Check the capital and risk % boxes."}

    today = ist_today()
    live = config.FEED == "angel"
    if live:
        chain = instruments.option_chain(inst)
    else:
        from .feeds.mock import build_chain

        chain = build_chain(inst, spot, today)
    targets = [t1] + ([t2] if t2 else [])
    return await asyncio.to_thread(
        find_option, chain, side, spot, entry, stop, targets, provider.option_ltp, capital, risk, today, live
    )


# ---------- paper trading (dummy money; these are async so they run on the same loop as the bot) ----------
def paper_view() -> dict:
    return account.snapshot(latest_price, config.FEED == "angel")


@app.get("/api/paper")
async def paper_get() -> dict:
    return paper_view()


@app.post("/api/paper/buy")
async def paper_buy(token: str, qty: int, stop: Optional[float] = None, target: Optional[float] = None) -> dict:
    inst = instruments.get(token)
    if inst is None or inst.get("kind") != "EQ":
        return {"ok": False, "message": "Only stocks can be bought, not indices.", **paper_view()}
    activate(inst)
    price = fresh_price(token)
    if price is None:
        return {"ok": False, "message": "No live price right now (market closed?).", **paper_view()}
    err = account.buy(inst, qty, price, "manual buy", stop, target)
    return {"ok": err is None, "message": err or f"Bought {qty} {inst['name']} at {price:,.2f}", **paper_view()}


@app.post("/api/paper/sell")
async def paper_sell(token: str) -> dict:
    price = fresh_price(token)
    if price is None:
        return {"ok": False, "message": "No live price right now (market closed?).", **paper_view()}
    err = account.sell(token, price, "manual sell")
    return {"ok": err is None, "message": err or "Sold", **paper_view()}


@app.post("/api/paper/bot")
async def paper_bot(on: bool) -> dict:
    account.set_bot(on)
    return {"ok": True, **paper_view()}


@app.post("/api/paper/reset")
async def paper_reset(cash: float = config.PAPER_START_CASH) -> dict:
    if not 0 < cash <= 1e9:
        return {"ok": False, "message": "Cash must be more than 0.", **paper_view()}
    account.reset(cash)
    return {"ok": True, **paper_view()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str, tf: str = config.DEFAULT_TF) -> None:
    inst = instruments.get(token)
    if inst is None or tf not in config.TIMEFRAMES:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        error = await ensure_history(inst, tf)
        series = stores[token].data[tf] if token in stores else []
        await ws.send_json(
            {
                "type": "snapshot",
                "instrument": public(inst),
                "tf": tf,
                "candles": series[-PAGE:],
                "has_more": len(series) > PAGE or not stores[token].exhausted[tf],
                "error": error,
                **levels_payload(token, tf),
            }
        )
        subscribers.setdefault(token, {})[ws] = tf  # register after the snapshot so order is correct
        while True:
            await ws.receive_text()  # the browser sends nothing; this just detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.get(token, {}).pop(ws, None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
