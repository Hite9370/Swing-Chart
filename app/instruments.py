"""The list of stocks and indices you can search.

With Angel One this is their public "instrument master" file (thousands of
instruments). It is downloaded once a day and cached in the data/ folder.
The same file also lists every option contract, so the nearby-expiry ones are
kept too, for the "Find option contract" button in the trade planner.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import config

log = logging.getLogger("instruments")

MASTER_URLS = [
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
]

CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "instruments_cache.json"
OPTIONS_FILE = CACHE_FILE.with_name("options_cache.json")
CACHE_MAX_AGE = 72000  # seconds (20 hours)
OPTION_DAYS_AHEAD = 75  # only option contracts expiring within this many days are kept

# index name in the stock list -> name used for its option contracts
INDEX_ALIASES = {
    "nifty50": "NIFTY",
    "niftybank": "BANKNIFTY",
    "niftyfinservice": "FINNIFTY",
    "niftymidselect": "MIDCPNIFTY",
    "niftynext50": "NIFTYNXT50",
}


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def parse_master(rows: Iterable[dict]) -> List[dict]:
    """Keep NSE stocks (EQ / BE series) and NSE/BSE indices from the master file."""
    out = []
    seen = set()
    for r in rows:
        seg = str(r.get("exch_seg", "")).upper().replace("_CM", "")
        symbol = str(r.get("symbol", "")).strip()
        itype = str(r.get("instrumenttype", "")).upper()
        token = str(r.get("token", "")).strip()
        if not token or token in seen:
            continue
        if seg == "NSE" and symbol.upper().endswith(("-EQ", "-BE")):
            kind = "EQ"
            name = str(r.get("name") or symbol.rsplit("-", 1)[0]).strip()
        elif seg in ("NSE", "BSE") and itype == "AMXIDX":
            kind = "INDEX"
            name = str(r.get("name") or symbol).strip()
        else:
            continue
        seen.add(token)
        out.append({
            "token": token,
            "symbol": symbol,
            "name": name,
            "exchange": seg,
            "exchange_type": 1 if seg == "NSE" else 3,
            "kind": kind,
        })
    return out


def parse_options(rows: Iterable[dict], today: Optional[date] = None) -> Dict[str, list]:
    """Option contracts expiring soon, grouped by underlying:
    {"NIFTY": [[token, symbol, "2026-10-28", strike, "CE", lot], ...]}"""
    today = today or date.today()
    limit = today + timedelta(days=OPTION_DAYS_AHEAD)
    out: Dict[str, list] = {}
    for r in rows:
        if str(r.get("exch_seg", "")).upper() != "NFO":
            continue
        if str(r.get("instrumenttype", "")).upper() not in ("OPTIDX", "OPTSTK"):
            continue
        symbol = str(r.get("symbol", "")).strip()
        kind = symbol[-2:].upper()
        if kind not in ("CE", "PE"):
            continue
        try:
            expiry = datetime.strptime(str(r.get("expiry", "")).strip().upper(), "%d%b%Y").date()
            strike = float(r.get("strike", "")) / 100.0  # the master stores strike x 100
            lot = int(float(r.get("lotsize") or 1))
        except (ValueError, TypeError):
            continue
        if expiry < today or expiry > limit:
            continue
        name = str(r.get("name", "")).strip().upper()
        out.setdefault(name, []).append([str(r.get("token", "")).strip(), symbol, expiry.isoformat(), strike, kind, lot])
    return out


def download_master() -> Optional[list]:
    for url in MASTER_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.load(resp)
        except Exception as exc:
            log.warning("could not download instrument list from %s: %s", url, exc)
    return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) or None
    except Exception:
        return None


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        log.warning("could not save %s: %s", path.name, exc)


def _read_cache() -> Optional[List[dict]]:
    return _read_json(CACHE_FILE)


def _write_cache(items: List[dict]) -> None:
    _write_json(CACHE_FILE, items)


class Instruments:
    def __init__(self) -> None:
        self.items: List[dict] = []
        self.by_token: Dict[str, dict] = {}
        self.limited = False
        self._keys: list = []
        self.options: Dict[str, list] = {}

    def load(self, items: Iterable[dict]) -> None:
        self.items = list(items)
        self.by_token = {i["token"]: i for i in self.items}
        self._keys = [(_norm(i["name"]), _norm(i["symbol"]), i) for i in self.items]

    def load_angel(self) -> None:
        """Blocking. Uses today's cache, else downloads, else an old cache, else a tiny list."""
        items = None
        options = None
        files = (CACHE_FILE, OPTIONS_FILE)
        fresh = all(f.exists() for f in files) and time.time() - min(f.stat().st_mtime for f in files) < CACHE_MAX_AGE
        if fresh:
            items = _read_cache()
            options = _read_json(OPTIONS_FILE)
        if not items:
            rows = download_master()
            if rows:
                items = parse_master(rows) or None
                options = parse_options(rows)
                if items:
                    _write_cache(items)
                    _write_json(OPTIONS_FILE, options)
        if not items:
            items = _read_cache()
            options = _read_json(OPTIONS_FILE)
        if not items:
            log.warning("Instrument list unavailable, using the small built-in list. Search is limited.")
            items = config.FALLBACK_INSTRUMENTS
            self.limited = True
        self.options = options or {}
        self.load(items)
        log.info("%d instruments available for search", len(self.items))

    def get(self, token: str) -> Optional[dict]:
        return self.by_token.get(str(token))

    def search(self, q: str, limit: int = 20) -> List[dict]:
        qn = _norm(q)
        if not qn:
            return []
        scored = []
        for name, sym, inst in self._keys:
            if name == qn or sym == qn or sym == qn + "eq":
                s = 0
            elif name.startswith(qn):
                s = 1
            elif sym.startswith(qn):
                s = 2
            elif qn in name:
                s = 3
            elif qn in sym:
                s = 4
            else:
                continue
            scored.append((s, 0 if inst["kind"] == "INDEX" else 1, len(name), name, inst))
        scored.sort(key=lambda x: x[:4])
        return [x[4] for x in scored[:limit]]

    def option_chain(self, inst: dict) -> dict:
        """Option contracts for one stock or index:
        {expiry (date): {strike: {"CE": contract, "PE": contract}}}. Empty if it has none."""
        rows: list = []
        for cand in (inst.get("name", ""), inst.get("symbol", "").rsplit("-", 1)[0], inst.get("symbol", "")):
            hit = self.options.get(str(cand).strip().upper())
            if hit:
                rows = hit
                break
        if not rows:
            alias = INDEX_ALIASES.get(_norm(inst.get("name", ""))) or INDEX_ALIASES.get(_norm(inst.get("symbol", "")))
            rows = self.options.get(alias or "", [])
        chain: dict = {}
        for token, symbol, expiry, strike, kind, lot in rows:
            contract = {
                "token": token, "symbol": symbol, "exchange": "NFO", "exchange_type": 2,
                "lot": lot, "expiry": date.fromisoformat(expiry), "strike": strike, "type": kind,
            }
            chain.setdefault(contract["expiry"], {}).setdefault(strike, {})[kind] = contract
        return chain
