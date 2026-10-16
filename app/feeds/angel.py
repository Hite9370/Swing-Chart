"""Live data and history from Angel One SmartAPI.

- login(): API key + client code + PIN + TOTP.
- fetch_chunk(): downloads candles for any instrument and timeframe, a chunk at
  a time, so you can scroll back through history. It slows down to respect
  Angel One's request limits, retries when rate limited, shrinks the request
  window if Angel One rejects it, and logs in again if the session expired.
- start_stream() / subscribe(): live prices over the SmartAPI WebSocket.

Only market data is used. This app never places orders.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from . import FeedError, RateLimited
from ..config import IST_OFFSET, TIMEFRAMES

log = logging.getLogger("angel")

IST = timezone(timedelta(seconds=IST_OFFSET))
MIN_GAP_SECONDS = 0.4            # Angel One allows only a few history requests per second
TOKEN_ERRORS = {"AG8001", "AG8002", "AG8003"}
TRANSIENT_CODE = "AB1004"        # "Something went wrong, please try after sometime"
CORRELATION_ID = "swingplat1"    # SmartAPI wants a short alphanumeric id


def _is_rate_limit(msg: str) -> bool:
    low = msg.lower()
    return any(s in low for s in ("access rate", "too many request", "rate limit"))


def _is_session_error(msg: str, code: str) -> bool:
    low = msg.lower()
    return code in TOKEN_ERRORS or any(s in low for s in ("invalid token", "token expired", "session expired"))


class AngelProvider:
    name = "angel"

    def __init__(self) -> None:
        self.api_key = os.getenv("ANGEL_API_KEY", "").strip()
        self.client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()
        self.pin = os.getenv("ANGEL_PIN", "").strip()
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

        missing = [
            name
            for name, val in {
                "ANGEL_API_KEY": self.api_key,
                "ANGEL_CLIENT_CODE": self.client_code,
                "ANGEL_PIN": self.pin,
                "ANGEL_TOTP_SECRET": self.totp_secret,
            }.items()
            if not val
        ]
        if missing:
            raise RuntimeError(
                "FEED=angel but these values are empty in your .env file: " + ", ".join(missing)
            )

        self.api = None
        self.auth_token = None
        self.feed_token = None
        self.sws = None
        self._ws_open = False
        self._tokens: Dict[str, dict] = {}
        self._tok_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_call = 0.0

    # ------------------------------------------------------------------ login
    def login(self) -> None:
        """Blocking. Safe to call again when the session expires."""
        import pyotp

        try:
            from SmartApi import SmartConnect
        except ImportError:
            from SmartApi.smartConnect import SmartConnect

        api = SmartConnect(api_key=self.api_key)
        totp = pyotp.TOTP(self.totp_secret).now()
        session = api.generateSession(self.client_code, self.pin, totp)
        if not session or not session.get("status"):
            msg = session.get("message") if isinstance(session, dict) else "no response"
            raise RuntimeError(f"Angel One login failed: {msg}")
        self.api = api
        self.auth_token = session["data"]["jwtToken"]
        self.feed_token = api.getfeedToken()
        log.info("Angel One login OK")

    # ------------------------------------------------------------------ history
    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._last_call + MIN_GAP_SECONDS - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    @staticmethod
    def _fmt(ts: float, daily: bool, end: bool) -> str:
        d = datetime.fromtimestamp(ts, timezone.utc)  # ts is IST-shifted, so this is IST wall-clock
        if daily:
            return d.strftime("%Y-%m-%d") + (" 15:30" if end else " 09:15")
        return d.strftime("%Y-%m-%d %H:%M")

    def _get_rows(self, inst: dict, cfg: dict, frm: float, to: float) -> list:
        params = {
            "exchange": inst["exchange"],
            "symboltoken": inst["token"],
            "interval": cfg["angel"],
            "fromdate": self._fmt(frm, cfg["seconds"] >= 86400, False),
            "todate": self._fmt(to, cfg["seconds"] >= 86400, True),
        }
        relogged = False
        transient = 0
        for attempt in range(4):
            self._throttle()
            try:
                resp = self.api.getCandleData(params)
            except Exception as exc:  # the SDK raises on some HTTP errors
                resp = {"status": False, "message": str(exc), "errorcode": ""}
            if resp and resp.get("status"):
                return resp.get("data") or []

            msg = str((resp or {}).get("message") or "no response")
            code = str((resp or {}).get("errorcode") or "")
            if _is_rate_limit(msg):
                time.sleep(1.5 * (attempt + 1))  # too many requests, wait and retry
                continue
            if not relogged and _is_session_error(msg, code):
                log.info("session looks expired, logging in again")
                self.login()
                relogged = True
                continue
            if code == TRANSIENT_CODE and transient < 2:
                transient += 1                   # Angel One's generic hiccup, often gone a second later
                time.sleep(1.0)
                continue
            raise FeedError(f"{msg} ({code})" if code else msg)
        raise RateLimited("Angel One is limiting requests right now. Wait a minute and try again.")

    @staticmethod
    def _to_candles(rows: list) -> List[dict]:
        out = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r[0]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST)
                out.append({
                    "time": int(dt.timestamp() + IST_OFFSET),
                    "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                })
            except (ValueError, TypeError, IndexError):
                continue
        out.sort(key=lambda c: c["time"])
        return out

    def fetch_chunk(self, inst: dict, tf: str, end_ts: Optional[float]) -> List[dict]:
        """Blocking. Candles just before end_ts (IST-shifted), or the latest when end_ts is None.

        Returns [] when there is nothing older (the start of available data).
        """
        cfg = TIMEFRAMES[tf]
        to_ts = time.time() + IST_OFFSET if end_ts is None else end_ts - 1
        window = cfg["chunk_days"]
        errors = 0
        empty_windows = 0
        while empty_windows < 3:
            frm = to_ts - window * 86400
            try:
                rows = self._get_rows(inst, cfg, frm, to_ts)
            except RateLimited:
                raise                            # a smaller window will not help, the user just has to wait
            except FeedError:
                errors += 1
                if errors > 3 or window <= 2:
                    raise
                window = max(2, window // 2)  # window may be too big for this interval, try smaller
                log.info("history request rejected, retrying with %d days", window)
                continue
            if rows:
                return self._to_candles(rows)
            empty_windows += 1               # weekend/holiday gap or before listing, look further back
            to_ts = frm - 1
        return []

    # ------------------------------------------------------------------ option price
    def option_ltp(self, contract: dict, spot: float = 0.0) -> Optional[float]:
        """Blocking. Last traded price of an option contract (the last close when the market is shut)."""
        relogged = False
        for attempt in range(3):
            self._throttle()
            try:
                resp = self.api.ltpData(contract["exchange"], contract["symbol"], contract["token"])
            except Exception as exc:
                resp = {"status": False, "message": str(exc), "errorcode": ""}
            if resp and resp.get("status"):
                ltp = (resp.get("data") or {}).get("ltp")
                return float(ltp) if ltp else None
            msg = str((resp or {}).get("message") or "no response")
            code = str((resp or {}).get("errorcode") or "")
            if _is_rate_limit(msg):
                time.sleep(1.5 * (attempt + 1))
                continue
            if not relogged and _is_session_error(msg, code):
                self.login()
                relogged = True
                continue
            log.warning("could not get the price of %s: %s", contract["symbol"], msg)
            return None
        return None

    # ------------------------------------------------------------------ live stream
    @staticmethod
    def _groups(instruments: List[dict]) -> List[dict]:
        by_exchange: Dict[int, List[str]] = {}
        for inst in instruments:
            by_exchange.setdefault(inst["exchange_type"], []).append(inst["token"])
        return [{"exchangeType": et, "tokens": toks} for et, toks in by_exchange.items()]

    def start_stream(self, on_tick: Callable[[str, float, float], None]) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        sws = SmartWebSocketV2(self.auth_token, self.api_key, self.client_code, self.feed_token)
        self.sws = sws

        def on_open(_ws):
            log.info("Angel One WebSocket connected")
            self._ws_open = True
            with self._tok_lock:
                groups = self._groups(list(self._tokens.values()))
            if groups:
                sws.subscribe(CORRELATION_ID, 1, groups)  # mode 1 = LTP

        def on_data(_ws, message):
            try:
                token = str(message.get("token", "")).strip('"')
                price = message["last_traded_price"] / 100  # paise -> rupees
                ms = message.get("exchange_timestamp")
                ts = (ms / 1000 if ms else time.time()) + IST_OFFSET
                on_tick(token, ts, round(price, 2))
            except Exception:
                log.exception("could not read tick: %s", message)

        def on_close(_ws):
            self._ws_open = False
            log.warning("WebSocket closed")

        sws.on_open = on_open
        sws.on_data = on_data
        sws.on_error = lambda _ws, err: log.error("WebSocket error: %s", err)
        sws.on_close = on_close
        threading.Thread(target=sws.connect, daemon=True).start()

    def subscribe(self, inst: dict) -> None:
        with self._tok_lock:
            self._tokens[inst["token"]] = inst
        if self._ws_open and self.sws:
            try:
                self.sws.subscribe(CORRELATION_ID, 1, self._groups([inst]))
            except Exception:
                log.exception("live subscribe failed for %s", inst["symbol"])

    def unsubscribe(self, inst: Optional[dict]) -> None:
        if not inst:
            return
        with self._tok_lock:
            self._tokens.pop(inst["token"], None)
        if self._ws_open and self.sws:
            try:
                self.sws.unsubscribe(CORRELATION_ID, 1, self._groups([inst]))
            except Exception:
                log.exception("live unsubscribe failed for %s", inst["symbol"])
