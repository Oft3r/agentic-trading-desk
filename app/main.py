#!/usr/bin/env python3
"""
FastAPI backend for the Agentic Trading Desk UI.
Proxies HTTP requests to the deterministic stdlib scripts in ../scripts/.
Auto mode: background asyncio loop runs auto_engine.py on cached ticker data.
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE    = Path(__file__).parent
SCRIPTS = BASE.parent / "scripts"
STATE   = BASE.parent / "state"
CONFIG_PATH = STATE / "auto_config.json"
STATUS_PATH = STATE / "auto_status.json"
DATA_DIR    = STATE / "data"

# ── State helpers ─────────────────────────────────────────────

def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Script runner ─────────────────────────────────────────────

def _run(script: str, payload: dict, extra: list[str] | None = None) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        cmd = [sys.executable, str(SCRIPTS / script), tmp, "--json"] + (extra or [])
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, cwd=str(SCRIPTS))
        if r.returncode != 0:
            return {"error": r.stderr.strip() or f"{script} exited {r.returncode}"}
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"error": f"{script} timed out"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Auto mode background loop ─────────────────────────────────

async def _auto_loop() -> None:
    """
    Runs continuously. Every `scan_interval_sec` seconds (when enabled):
    1. Reads all cached ticker files from state/data/<ticker>.json
    2. Calls auto_engine.py with the batch
    3. Writes results to state/auto_status.json
    4. Appends to execution log for any signals marked execute=True
    """
    while True:
        try:
            config = _load(CONFIG_PATH)
            interval = max(int(config.get("scan_interval_sec", 300)), 10)

            if config.get("enabled"):
                await _run_auto_scan(config)

        except Exception as e:
            # Never crash the loop
            status = _load(STATUS_PATH)
            status.setdefault("log", []).append(
                {"ts": _now(), "event": "LOOP_ERROR", "detail": str(e)})
            _save(STATUS_PATH, status)

        await asyncio.sleep(interval)


async def _run_auto_scan(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_files = sorted(DATA_DIR.glob("*.json"))
    if not data_files:
        status = _load(STATUS_PATH)
        status.update({"last_scan": _now(), "scan_count": status.get("scan_count", 0) + 1,
                       "signals": [], "blocked": [],
                       "_info": "no cached ticker data in state/data/"})
        _save(STATUS_PATH, status)
        return

    # Build batch from cached data files
    tickers: dict[str, dict] = {}
    watchlist = [s.upper() for s in config.get("watchlist", [])]
    for f in data_files:
        sym = f.stem.upper()
        if watchlist and sym not in watchlist:
            continue
        try:
            tdata = json.loads(f.read_text())
            tickers[sym] = tdata
        except Exception:
            pass

    if not tickers:
        return

    batch = {
        "as_of": _now(),
        "macro_score": int(config.get("macro_score", 0)),
        "portfolio_value": float(config.get("portfolio_value", 0)),
        "cash_available": float(config.get("cash_available", 0)),
        "tickers": tickers,
    }

    # Run auto_engine.py; it reads/writes state directly via --save-state
    extra = ["--config", str(CONFIG_PATH), "--state", str(STATUS_PATH), "--save-state"]
    if config.get("dry_run", True):
        extra.append("--dry-run")

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run("auto_engine.py", batch, extra))

    # Merge result into status (auto_engine already saved via --save-state,
    # but we annotate with next_scan time)
    if "error" not in result:
        status = _load(STATUS_PATH)
        interval = max(int(config.get("scan_interval_sec", 300)), 10)
        import time
        status["next_scan"] = datetime.fromtimestamp(
            time.time() + interval, tz=timezone.utc).isoformat(timespec="seconds")
        status["enabled"] = True
        _save(STATUS_PATH, status)


# ── Lifespan (background task) ────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_auto_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── App ───────────────────────────────────────────────────────

app = FastAPI(title="Agentic Trading Desk", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ── UI ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Script proxy endpoints ─────────────────────────────────────

@app.post("/api/score")
async def api_score(request: Request):
    data = await request.json()
    result = _run("score.py", data)
    # Cache ticker data for auto mode
    if "error" not in result and data.get("close"):
        sym = (data.get("symbol") or "UNKNOWN").upper()
        cache = {"close": data["close"], "holding": data.get("holding", False)}
        if data.get("high"): cache["high"] = data["high"]
        if data.get("low"):  cache["low"]  = data["low"]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _save(DATA_DIR / f"{sym}.json", cache)
    return JSONResponse(result)


@app.post("/api/macro")
async def api_macro(request: Request):
    return JSONResponse(_run("macro_pillar.py", await request.json()))


@app.post("/api/volatility")
async def api_volatility(request: Request):
    return JSONResponse(_run("volatility.py", await request.json()))


@app.post("/api/plan")
async def api_plan(request: Request):
    return JSONResponse(_run("execution_plan.py", await request.json()))


@app.post("/api/backtest")
async def api_backtest(request: Request):
    data = dict(await request.json())
    extra: list[str] = []
    splits   = int(data.pop("splits", 0))
    sens     = bool(data.pop("sensitivity", False))
    lag      = int(data.pop("lag", 1))
    cost_bps = float(data.pop("cost_bps", 5.0))
    warmup   = int(data.pop("warmup", 220))
    no_stop  = bool(data.pop("no_stop", False))
    extra += ["--lag", str(lag), "--cost-bps", str(cost_bps), "--warmup", str(warmup)]
    if splits >= 2: extra += ["--splits", str(splits)]
    if sens:         extra += ["--sensitivity"]
    if no_stop:      extra += ["--no-stop"]
    return JSONResponse(_run("backtest.py", data, extra))


# ── Auto mode endpoints ────────────────────────────────────────

@app.get("/api/auto/status")
async def auto_status():
    status = _load(STATUS_PATH)
    config = _load(CONFIG_PATH)
    # Add watchlist cache info
    cached = sorted(f.stem.upper() for f in DATA_DIR.glob("*.json")) if DATA_DIR.exists() else []
    return JSONResponse({**status, "cached_tickers": cached,
                         "dry_run": config.get("dry_run", True),
                         "enabled": config.get("enabled", False)})


@app.get("/api/auto/config")
async def get_auto_config():
    return JSONResponse(_load(CONFIG_PATH))


@app.post("/api/auto/config")
async def set_auto_config(request: Request):
    data   = await request.json()
    config = _load(CONFIG_PATH)
    config.update(data)
    _save(CONFIG_PATH, config)
    return JSONResponse({"ok": True, "config": config})


@app.post("/api/auto/toggle")
async def toggle_auto(request: Request):
    body   = await request.json()
    enable = bool(body.get("enabled", False))
    config = _load(CONFIG_PATH)
    config["enabled"] = enable
    _save(CONFIG_PATH, config)

    status = _load(STATUS_PATH)
    status["enabled"] = enable
    if not enable:
        status["next_scan"] = None
        status.setdefault("log", []).append(
            {"ts": _now(), "event": "AUTO_STOPPED", "detail": "user toggled off"})
    else:
        status.setdefault("log", []).append(
            {"ts": _now(), "event": "AUTO_STARTED",
             "detail": f"dry_run={config.get('dry_run',True)}"})
    _save(STATUS_PATH, status)
    return JSONResponse({"ok": True, "enabled": enable})


@app.post("/api/auto/scan")
async def manual_scan():
    """Immediately trigger one auto scan cycle (regardless of interval)."""
    config = _load(CONFIG_PATH)
    if not DATA_DIR.exists() or not list(DATA_DIR.glob("*.json")):
        return JSONResponse({"error": "No cached ticker data. Scan tickers via Portfolio tab first."})
    await _run_auto_scan(config)
    return JSONResponse({"ok": True, "status": _load(STATUS_PATH)})


@app.post("/api/auto/stop")
async def emergency_stop():
    """Hard-stop: disable auto mode and mark hard_stopped in state."""
    config = _load(CONFIG_PATH)
    config["enabled"] = False
    _save(CONFIG_PATH, config)

    status = _load(STATUS_PATH)
    status["enabled"] = False
    status["next_scan"] = None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    status.setdefault("daily_stats", {}).update(
        {"hard_stopped": True, "date": today})
    status.setdefault("log", []).append(
        {"ts": _now(), "event": "EMERGENCY_STOP", "detail": "user triggered"})
    _save(STATUS_PATH, status)
    return JSONResponse({"ok": True, "stopped": True})


@app.post("/api/auto/reset")
async def reset_auto():
    """Clear daily stats, cooldowns, and hard_stopped — start fresh."""
    status = _load(STATUS_PATH)
    status.update({"daily_stats": {}, "cooldowns": {}, "signals": [], "blocked": []})
    status["daily_stats"] = {"date": None, "trades": 0, "pnl_pct": 0.0, "hard_stopped": False}
    status.setdefault("log", []).append({"ts": _now(), "event": "RESET", "detail": ""})
    _save(STATUS_PATH, status)
    return JSONResponse({"ok": True})
