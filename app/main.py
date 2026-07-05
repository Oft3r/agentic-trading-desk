#!/usr/bin/env python3
"""
FastAPI backend for the Agentic Trading Desk UI.
Proxies HTTP requests to the deterministic stdlib scripts in ../scripts/.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
SCRIPTS = BASE.parent / "scripts"

app = FastAPI(title="Agentic Trading Desk", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _run(script: str, payload: dict, extra: list[str] | None = None) -> dict:
    """Write payload to temp JSON, exec script with --json, return parsed output."""
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
        return {"error": f"{script} timed out after 120s"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/api/score")
async def api_score(request: Request):
    return JSONResponse(_run("score.py", await request.json()))


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
    if splits >= 2:
        extra += ["--splits", str(splits)]
    if sens:
        extra += ["--sensitivity"]
    if no_stop:
        extra += ["--no-stop"]
    return JSONResponse(_run("backtest.py", data, extra))
