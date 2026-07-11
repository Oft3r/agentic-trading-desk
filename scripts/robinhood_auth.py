#!/usr/bin/env python3
"""
robinhood_auth.py — OAuth 2.0 (PKCE) client for the Robinhood Agent MCP.
================================================================================
Handles the full auth lifecycle for https://agent.robinhood.com/mcp/trading :

  * Dynamic client registration (RFC 7591)   -> client_id
  * Authorization Code + PKCE (S256) login    -> auth code (browser)
  * Token exchange & refresh                  -> access/refresh tokens
  * Persistent, auto-refreshing token store   -> ~/.hermes/state/rh_mcp_tokens.json

CLI:
  python3 robinhood_auth.py login     # interactive: opens browser, catches code
  python3 robinhood_auth.py token     # prints a valid access token (refreshes)
  python3 robinhood_auth.py status    # shows token state without leaking secrets

Networking uses `curl` subprocess (system python3 urllib has broken SSL here).
The localhost callback server uses stdlib http.server (no TLS needed on loopback).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --- Endpoints (from /.well-known discovery) -------------------------------
REGISTRATION_EP = "https://agent.robinhood.com/oauth/trading/register"
AUTHORIZE_EP = "https://robinhood.com/oauth"
TOKEN_EP = "https://api.robinhood.com/oauth2/token/"
RESOURCE = "https://agent.robinhood.com/mcp/trading"
SCOPE = "internal"

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

STATE_DIR = Path(os.environ.get("DESK_STATE_DIR",
                                str(Path.home() / ".hermes" / "state")))
CLIENT_FILE = STATE_DIR / "rh_mcp_client.json"
TOKEN_FILE = STATE_DIR / "rh_mcp_tokens.json"
PENDING_FILE = STATE_DIR / "rh_mcp_pending.json"

REFRESH_SKEW = 120  # refresh this many seconds before expiry


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------
# curl helpers
# --------------------------------------------------------------------------
def _curl_json(args: list[str], what: str) -> dict:
    r = subprocess.run(["curl", "-s", "--max-time", "30", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AuthError(f"{what}: curl rc={r.returncode} {r.stderr.strip()[:160]}")
    body = r.stdout.strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise AuthError(f"{what}: non-JSON response: {body[:200]}")


def _secure_write(path: Path, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Client registration
# --------------------------------------------------------------------------
def register_client(force: bool = False) -> dict:
    if CLIENT_FILE.exists() and not force:
        return json.loads(CLIENT_FILE.read_text())
    payload = {
        "client_name": "Hermes Agentic Trading Desk",
        "redirect_uris": [REDIRECT_URI, f"http://127.0.0.1:{CALLBACK_PORT}/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    client = _curl_json(
        ["-X", "POST", REGISTRATION_EP, "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        "client registration",
    )
    if "client_id" not in client:
        raise AuthError(f"registration missing client_id: {client}")
    _secure_write(CLIENT_FILE, client)
    return client


# --------------------------------------------------------------------------
# PKCE
# --------------------------------------------------------------------------
def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# --------------------------------------------------------------------------
# Local callback catcher
# --------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in qs.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _CallbackHandler.result
        msg = ("Robinhood authorization received. You can close this tab and "
               "return to Hermes.") if ok else \
              ("Authorization failed: " + json.dumps(_CallbackHandler.result))
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, format, *args):  # noqa: A002 — silence
        pass


def _catch_code(timeout: int = 300) -> dict:
    _CallbackHandler.result = {}
    srv = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    srv.timeout = 1
    deadline = time.time() + timeout
    while time.time() < deadline and not _CallbackHandler.result:
        srv.handle_request()
    srv.server_close()
    if not _CallbackHandler.result:
        raise AuthError("timed out waiting for the OAuth redirect")
    return _CallbackHandler.result


# --------------------------------------------------------------------------
# Token exchange / refresh
# --------------------------------------------------------------------------
def _store_tokens(tok: dict) -> dict:
    now = int(time.time())
    expires_in = int(tok.get("expires_in", 3600))
    rec = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "token_type": tok.get("token_type", "Bearer"),
        "scope": tok.get("scope", SCOPE),
        "expires_at": now + expires_in,
        "obtained_at": now,
    }
    _secure_write(TOKEN_FILE, rec)
    return rec


def exchange_code(client_id: str, code: str, verifier: str) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    args = ["-X", "POST", TOKEN_EP,
            "-H", "Content-Type: application/x-www-form-urlencoded"]
    for k, v in form.items():
        args += ["--data-urlencode", f"{k}={v}"]
    tok = _curl_json(args, "token exchange")
    if "access_token" not in tok:
        raise AuthError(f"token exchange failed: {tok}")
    return _store_tokens(tok)


def refresh_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise AuthError("no stored tokens; run `login` first")
    rec = json.loads(TOKEN_FILE.read_text())
    client = register_client()
    if not rec.get("refresh_token"):
        raise AuthError("no refresh_token stored; re-run `login`")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": rec["refresh_token"],
        "client_id": client["client_id"],
        "scope": rec.get("scope", SCOPE),
    }
    args = ["-X", "POST", TOKEN_EP,
            "-H", "Content-Type: application/x-www-form-urlencoded"]
    for k, v in form.items():
        args += ["--data-urlencode", f"{k}={v}"]
    tok = _curl_json(args, "token refresh")
    if "access_token" not in tok:
        raise AuthError(f"refresh failed: {tok}")
    # Robinhood may rotate the refresh token; keep old if absent.
    if not tok.get("refresh_token"):
        tok["refresh_token"] = rec["refresh_token"]
    return _store_tokens(tok)


def get_access_token() -> str:
    """Return a valid access token, refreshing if near expiry."""
    if not TOKEN_FILE.exists():
        raise AuthError("not authenticated; run `python3 robinhood_auth.py login`")
    rec = json.loads(TOKEN_FILE.read_text())
    if int(time.time()) >= rec.get("expires_at", 0) - REFRESH_SKEW:
        rec = refresh_tokens()
    return rec["access_token"]


# --------------------------------------------------------------------------
# Interactive login
# --------------------------------------------------------------------------
def login() -> dict:
    client = register_client()
    client_id = client["client_id"]
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    auth_url = AUTHORIZE_EP + "?" + urllib.parse.urlencode(params)

    print("\n" + "=" * 70)
    print("ROBINHOOD MCP — AUTHORIZATION REQUIRED")
    print("=" * 70)
    print("1. A browser window will open to Robinhood's login/consent page.")
    print("2. Log in and approve access for 'Hermes Agentic Trading Desk'.")
    print("3. You'll be redirected back automatically; return here when done.")
    print("\nIf the browser does not open, paste this URL manually:\n")
    print(auth_url)
    print("=" * 70 + "\n", flush=True)

    # Start callback server BEFORE opening browser to avoid a race.
    holder: dict = {}
    err: dict = {}

    def _serve():
        try:
            holder.update(_catch_code(timeout=300))
        except AuthError as e:
            err["e"] = str(e)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    t.join(timeout=310)

    if err:
        raise AuthError(err["e"])
    if holder.get("state") != state:
        raise AuthError("state mismatch (possible CSRF); aborting")
    if "code" not in holder:
        raise AuthError(f"no code returned: {holder}")
    rec = exchange_code(client_id, holder["code"], verifier)
    print("✅ Authenticated. Tokens stored at", TOKEN_FILE)
    return rec


def status() -> None:
    print("Registration:", "present" if CLIENT_FILE.exists() else "absent")
    if not TOKEN_FILE.exists():
        print("Tokens: none — run `login`.")
        return
    rec = json.loads(TOKEN_FILE.read_text())
    now = int(time.time())
    ttl = rec.get("expires_at", 0) - now
    print(f"Tokens: present  ttl={ttl}s  "
          f"refresh_token={'yes' if rec.get('refresh_token') else 'no'}  "
          f"scope={rec.get('scope')}")


def authurl() -> str:
    """Print the authorization URL and persist PKCE state for `catch`."""
    client = register_client()
    client_id = client["client_id"]
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    auth_url = AUTHORIZE_EP + "?" + urllib.parse.urlencode(params)
    _secure_write(PENDING_FILE, {"client_id": client_id, "verifier": verifier,
                                 "state": state})
    print(auth_url)
    return auth_url


def catch() -> dict:
    """Run the local callback server, then exchange the returned code."""
    if not PENDING_FILE.exists():
        raise AuthError("no pending auth; run `authurl` first")
    pend = json.loads(PENDING_FILE.read_text())
    got = _catch_code(timeout=300)
    if got.get("state") != pend["state"]:
        raise AuthError("state mismatch (possible CSRF); aborting")
    if "code" not in got:
        raise AuthError(f"no code returned: {got}")
    rec = exchange_code(pend["client_id"], got["code"], pend["verifier"])
    try:
        PENDING_FILE.unlink()
    except OSError:
        pass
    print("✅ Authenticated. Tokens stored at", TOKEN_FILE)
    return rec


def catch_daemon() -> None:
    """Double-fork into a detached daemon that runs `catch` independently.

    This survives the parent process exiting (and Hermes turn boundaries),
    writing the token file on success and a status line to a log file.
    Returns immediately in the parent.
    """
    log = STATE_DIR / "rh_catch.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # First fork
    if os.fork() > 0:
        print(f"catcher daemonized; log -> {log}")
        return
    os.setsid()
    # Second fork
    if os.fork() > 0:
        os._exit(0)
    # Grandchild: fully detached. Redirect std streams to the log.
    with open(log, "a") as f:
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
    try:
        with open(os.devnull) as dn:
            os.dup2(dn.fileno(), 0)
    except OSError:
        pass
    try:
        print(f"[{time.strftime('%H:%M:%S')}] catcher listening on {REDIRECT_URI}",
              flush=True)
        catch()
        print(f"[{time.strftime('%H:%M:%S')}] catcher done OK", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{time.strftime('%H:%M:%S')}] catcher error: {e}", flush=True)
    os._exit(0)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "login":
            login()
        elif cmd == "authurl":
            authurl()
        elif cmd == "catch":
            catch()
        elif cmd == "catch-daemon":
            catch_daemon()
        elif cmd == "token":
            print(get_access_token())
        elif cmd == "refresh":
            refresh_tokens()
            print("refreshed")
        elif cmd == "status":
            status()
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except AuthError as e:
        print(f"AUTH ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
