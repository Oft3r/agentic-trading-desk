#!/usr/bin/env python3
"""
robinhood_mcp.py — minimal StreamableHTTP JSON-RPC client for Robinhood MCP.
================================================================================
Speaks the MCP wire protocol to https://agent.robinhood.com/mcp/trading using
`curl` (SSL-reliable on this host). Handles:

  * initialize handshake (captures Mcp-Session-Id)
  * notifications/initialized
  * tools/list  (discover available tool names/schemas)
  * tools/call  (invoke a tool, parse text/JSON content)

Auth: bearer access token from robinhood_auth.get_access_token() (auto-refresh).

The Robinhood MCP tool names are discovered at runtime (they can change). This
module exposes helpers that search the discovered tool list for the
historicals / quote / positions tools by keyword, so we do not hard-code names
that might drift.

CLI:
  python3 robinhood_mcp.py list                 # list discovered tools
  python3 robinhood_mcp.py closes SPY           # print daily closes for SPY
  python3 robinhood_mcp.py quote SPY            # print live quote
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from typing import Any, Optional

import robinhood_auth as auth

MCP_URL = "https://agent.robinhood.com/mcp/trading"
PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


def _parse_sse_or_json(body: str) -> dict:
    """Robinhood may reply as SSE (event-stream) or plain JSON. Handle both."""
    body = body.strip()
    if not body:
        raise MCPError("empty response body")
    # SSE: lines like 'event: message' / 'data: {...}'
    if body.startswith("event:") or "\ndata:" in body or body.startswith("data:"):
        chunks = []
        for line in body.splitlines():
            if line.startswith("data:"):
                chunks.append(line[len("data:"):].strip())
        joined = "".join(chunks)
        if joined:
            return json.loads(joined)
    return json.loads(body)


class RobinhoodMCP:
    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.token = auth.get_access_token()

    # -- low-level POST ----------------------------------------------------
    def _post(self, payload: dict, capture_headers: bool = False) -> tuple[dict, dict]:
        args = [
            "curl", "-s", "--max-time", "45", "-X", "POST", MCP_URL,
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json, text/event-stream",
            "-H", f"Authorization: Bearer {self.token}",
        ]
        if self.session_id:
            args += ["-H", f"Mcp-Session-Id: {self.session_id}"]
        if capture_headers:
            args += ["-D", "-"]  # dump headers to stdout
        args += ["-d", json.dumps(payload)]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise MCPError(f"curl rc={r.returncode}: {r.stderr.strip()[:160]}")
        out = r.stdout
        headers: dict[str, str] = {}
        body = out
        if capture_headers:
            # Split header block(s) from body. Headers end at a blank line.
            parts = out.split("\r\n\r\n")
            # Last part is the body; earlier parts are header blocks (may be >1).
            body = parts[-1]
            for block in parts[:-1]:
                for line in block.splitlines():
                    if ":" in line and not line.startswith("HTTP/"):
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
        return _parse_sse_or_json(body) if body.strip() else {}, headers

    # -- handshake ---------------------------------------------------------
    def initialize(self) -> dict:
        payload = {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hermes-agentic-trading-desk",
                               "version": "1.0"},
            },
        }
        resp, headers = self._post(payload, capture_headers=True)
        sid = headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if "error" in resp:
            raise MCPError(f"initialize error: {resp['error']}")
        # notifications/initialized (no id -> notification)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}})
        return resp.get("result", {})

    # -- rpc ---------------------------------------------------------------
    def _rpc(self, method: str, params: dict) -> Any:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()),
                   "method": method, "params": params}
        resp, _ = self._post(payload)
        if "error" in resp:
            raise MCPError(f"{method} error: {resp['error']}")
        return resp.get("result")

    def list_tools(self) -> list[dict]:
        result = self._rpc("tools/list", {})
        return result.get("tools", []) if result else []

    def call_tool(self, name: str, arguments: dict) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not result:
            return None
        # Prefer structuredContent; else parse text content blocks as JSON.
        if "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content", [])
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        blob = "\n".join(texts).strip()
        if not blob:
            return result
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return blob


# --------------------------------------------------------------------------
# Convenience: discover the right tool by keyword and normalize outputs
# --------------------------------------------------------------------------
def _find_tool(tools: list[dict], must_have: list[str],
               nice: Optional[list[str]] = None) -> Optional[str]:
    nice = nice or []
    best, best_score = None, -1
    for t in tools:
        name = t.get("name", "").lower()
        if not all(k in name for k in must_have):
            continue
        score = sum(1 for k in nice if k in name)
        if score > best_score:
            best, best_score = t.get("name"), score
    return best


def connect() -> "RobinhoodMCP":
    m = RobinhoodMCP()
    m.initialize()
    return m


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    m = connect()
    tools = m.list_tools()
    if cmd == "list":
        for t in tools:
            print(f"- {t.get('name')}: {t.get('description','')[:90]}")
        return 0
    if cmd in ("closes", "quote") and len(sys.argv) >= 3:
        sym = sys.argv[2].upper()
        kw = ["historical"] if cmd == "closes" else ["quote"]
        tool = _find_tool(tools, kw, ["equity", "stock"])
        print("using tool:", tool, file=sys.stderr)
        if not tool:
            print("no matching tool discovered", file=sys.stderr)
            return 1
        print(json.dumps(m.call_tool(tool, {"symbol": sym}), indent=2)[:2000])
        return 0
    print("usage: robinhood_mcp.py [list|closes SYM|quote SYM]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
