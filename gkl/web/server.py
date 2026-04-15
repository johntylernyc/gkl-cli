"""GKL Web Server — FastAPI wrapper for serving the Textual TUI over the web.

Handles Yahoo OAuth login, session management, and spawns per-user Textual
subprocesses connected via WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
from pathlib import Path
from time import time
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from gkl.web.session import COOKIE_NAME, Session, SessionStore
from gkl.yahoo_auth import AUTH_URL, TOKEN_URL, get_redirect_uri

logger = logging.getLogger("gkl.web")

# --- App setup ---

app = FastAPI(title="GKL Baseball", docs_url=None, redoc_url=None)

_store: SessionStore | None = None
_templates: Environment | None = None

# Track active subprocess count for resource limits
_active_sessions: int = 0
MAX_CONCURRENT_SESSIONS = int(os.environ.get("GKL_MAX_SESSIONS", "25"))


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


def get_templates() -> Environment:
    global _templates
    if _templates is None:
        template_dir = Path(__file__).parent / "templates"
        _templates = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True,
        )
    return _templates


def _get_session_from_request(request: Request) -> Session | None:
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return None
    store = get_store()
    session_id = store.unsign_session_id(signed)
    if not session_id:
        return None
    return store.get_session(session_id)


def _yahoo_client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("GKL_YAHOO_CLIENT_ID", "")
    client_secret = os.environ.get("GKL_YAHOO_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GKL_YAHOO_CLIENT_ID and GKL_YAHOO_CLIENT_SECRET must be set"
        )
    return client_id, client_secret


# --- Static assets from textual-serve ---


@app.on_event("startup")
async def _mount_statics() -> None:
    try:
        import textual_serve

        serve_pkg = Path(textual_serve.__file__).parent
        statics = serve_pkg / "static"
        if statics.is_dir():
            app.mount("/static", StaticFiles(directory=str(statics)), name="static")
            logger.info("Mounted textual-serve statics from %s", statics)
    except ImportError:
        logger.warning("textual-serve not installed; static assets unavailable")


# --- Auth routes ---


@app.get("/", response_model=None)
async def index(request: Request) -> HTMLResponse | RedirectResponse:
    session = _get_session_from_request(request)
    if session:
        return RedirectResponse("/app")
    template = get_templates().get_template("login.html")
    return HTMLResponse(template.render())


@app.get("/auth/yahoo")
async def auth_yahoo() -> RedirectResponse:
    client_id, _ = _yahoo_client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": get_redirect_uri(),
        "response_type": "code",
        "scope": "openid fspt-r",
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}")


@app.get("/auth/yahoo/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse("/?error=no_code")

    client_id, client_secret = _yahoo_client_credentials()
    import base64

    basic_auth = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": get_redirect_uri(),
            },
        )
        if resp.status_code != 200:
            logger.error("Token exchange failed: %s", resp.text)
            return RedirectResponse("/?error=token_exchange_failed")
        token_data = resp.json()

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_at = time() + token_data["expires_in"]
    logger.info("Token response keys: %s", list(token_data.keys()))

    # Primary user identifier from token response (always present in Yahoo OAuth2)
    yahoo_guid = token_data.get("xoauth_yahoo_guid", "")
    yahoo_email = ""
    yahoo_name = ""

    if not yahoo_guid:
        # Last resort: hash the access token for a stable-per-session identifier
        import hashlib
        yahoo_guid = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        logger.warning("No xoauth_yahoo_guid in token response, using hash fallback")

    # Optionally fetch profile for display name (non-critical)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.login.yahoo.com/openid/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                profile = resp.json()
                yahoo_email = profile.get("email", "")
                yahoo_name = profile.get("name", profile.get("nickname", ""))
    except Exception:
        pass  # Profile fetch is best-effort

    logger.info("User authenticated: guid=%s email=%s name=%s", yahoo_guid, yahoo_email, yahoo_name)

    store = get_store()
    session = store.create_session(
        yahoo_guid=yahoo_guid,
        yahoo_email=yahoo_email,
        yahoo_name=yahoo_name,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=expires_at,
    )

    response = RedirectResponse("/app")
    signed = store.sign_session_id(session.session_id)
    response.set_cookie(
        COOKIE_NAME,
        signed,
        max_age=7 * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    session = _get_session_from_request(request)
    if session:
        get_store().delete_session(session.session_id)
    response = RedirectResponse("/")
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/settings/anthropic-key")
async def set_anthropic_key(request: Request) -> RedirectResponse:
    session = _get_session_from_request(request)
    if not session:
        return RedirectResponse("/")
    form = await request.form()
    key = str(form.get("api_key", "")).strip()
    if key:
        get_store().set_anthropic_key(session.session_id, key)
    return RedirectResponse("/app", status_code=303)


# --- App page (terminal in browser) ---


@app.get("/app", response_model=None)
async def app_page(request: Request) -> HTMLResponse | RedirectResponse:
    session = _get_session_from_request(request)
    if not session:
        return RedirectResponse("/")

    template = get_templates().get_template("app.html")
    host = request.headers.get("host", "gklbaseball.com")
    return HTMLResponse(template.render(
        user_name=session.yahoo_name or session.yahoo_email or "User",
        has_anthropic_key=session.anthropic_key is not None,
        host=host,
    ))


# --- WebSocket endpoint (subprocess management) ---


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    global _active_sessions

    # Authenticate from cookie
    signed = websocket.cookies.get(COOKIE_NAME)
    if not signed:
        await websocket.close(code=4001, reason="Not authenticated")
        return

    store = get_store()
    session_id = store.unsign_session_id(signed)
    if not session_id:
        await websocket.close(code=4001, reason="Invalid session")
        return

    session = store.get_session(session_id)
    if not session:
        await websocket.close(code=4001, reason="Session expired")
        return

    if _active_sessions >= MAX_CONCURRENT_SESSIONS:
        await websocket.close(code=4003, reason="Server at capacity")
        return

    await websocket.accept()
    _active_sessions += 1

    try:
        await _run_textual_subprocess(websocket, session)
    finally:
        _active_sessions -= 1


async def _run_textual_subprocess(
    websocket: WebSocket, session: Session
) -> None:
    """Spawn a Textual app subprocess and bridge it to the WebSocket."""
    env = os.environ.copy()
    env.update({
        "GKL_MODE": "web",
        "GKL_YAHOO_CLIENT_ID": os.environ.get("GKL_YAHOO_CLIENT_ID", ""),
        "GKL_YAHOO_CLIENT_SECRET": os.environ.get("GKL_YAHOO_CLIENT_SECRET", ""),
        "GKL_YAHOO_ACCESS_TOKEN": session.access_token,
        "GKL_YAHOO_REFRESH_TOKEN": session.refresh_token,
        "GKL_YAHOO_TOKEN_EXPIRES_AT": str(session.token_expires_at),
        "GKL_USER_ID": session.yahoo_guid,
        "TEXTUAL_DRIVER": "textual.drivers.web_driver:WebDriver",
        "TEXTUAL_FPS": "60",
        "TEXTUAL_COLOR_SYSTEM": "truecolor",
        "TERM_PROGRAM": "textual",
        "COLUMNS": "120",
        "ROWS": "40",
    })

    if session.anthropic_key:
        env["GKL_ANTHROPIC_KEY"] = session.anthropic_key

    db_path = os.environ.get("GKL_DB_PATH", "/data/gkl.db")
    env["GKL_DB_PATH"] = db_path
    env["GKL_CACHE_DB"] = os.environ.get("GKL_CACHE_DB", "/data/cache.db")

    proc = await asyncio.create_subprocess_shell(
        f"{sys.executable} -m gkl.app",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    # Log stderr in background so we can see crash messages
    async def _log_stderr() -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            logger.error("subprocess stderr: %s", line.decode(errors="replace").rstrip())

    stderr_task = asyncio.create_task(_log_stderr())

    try:
        # Wait for the subprocess to signal readiness via __GANGLION__
        # Read lines until we get it or hit binary data (packet header)
        ready = False
        for _ in range(20):  # max 20 lines of preamble
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("Subprocess did not start within 30 seconds")
                return

            if not line:
                # Process exited
                logger.error("Subprocess exited before sending ready signal")
                return

            if b"__GANGLION__" in line:
                ready = True
                break

            # If first byte looks like a binary packet header, put it back
            if line[0:1] in (b"D", b"M", b"P") and len(line) >= 5:
                logger.warning("Got binary data before ready signal, proceeding")
                ready = True
                break

            logger.info("subprocess preamble: %s", line.decode(errors="replace").rstrip())

        if not ready:
            logger.error("Never received ready signal from subprocess")
            return

        # Bridge WebSocket <-> subprocess
        ws_to_proc = asyncio.create_task(_ws_to_process(websocket, proc))
        proc_to_ws = asyncio.create_task(_process_to_ws(websocket, proc))

        done, pending = await asyncio.wait(
            [ws_to_proc, proc_to_ws],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        pass
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


async def _ws_to_process(
    websocket: WebSocket, proc: asyncio.subprocess.Process
) -> None:
    """Forward WebSocket messages to the subprocess stdin."""
    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)

            if not isinstance(message, list) or len(message) < 1:
                continue

            msg_type = message[0]

            if msg_type == "stdin" and len(message) >= 2:
                data = message[1].encode("utf-8", errors="replace")
                packet = b"D" + struct.pack(">I", len(data)) + data
                proc.stdin.write(packet)
                await proc.stdin.drain()

            elif msg_type == "resize" and len(message) >= 2:
                resize_data = json.dumps({
                    "type": "resize",
                    "width": message[1].get("width", 120),
                    "height": message[1].get("height", 40),
                }).encode("utf-8")
                packet = b"M" + struct.pack(">I", len(resize_data)) + resize_data
                proc.stdin.write(packet)
                await proc.stdin.drain()

            elif msg_type == "ping":
                await websocket.send_text(
                    json.dumps(["pong", message[1] if len(message) > 1 else None])
                )

            elif msg_type in ("blur", "focus"):
                meta = json.dumps({"type": msg_type}).encode("utf-8")
                packet = b"M" + struct.pack(">I", len(meta)) + meta
                proc.stdin.write(packet)
                await proc.stdin.drain()

    except (WebSocketDisconnect, ConnectionError):
        pass


async def _process_to_ws(
    websocket: WebSocket, proc: asyncio.subprocess.Process
) -> None:
    """Forward subprocess stdout to the WebSocket."""
    try:
        while True:
            # Read packet header: 1 byte type + 4 byte length
            header = await proc.stdout.readexactly(5)
            packet_type = chr(header[0])
            length = struct.unpack(">I", header[1:5])[0]

            payload = await proc.stdout.readexactly(length)

            if packet_type == "D":
                # Terminal data — send as binary
                await websocket.send_bytes(payload)

            elif packet_type == "M":
                # Meta message — parse and forward
                meta = json.loads(payload)
                meta_type = meta.get("type", "")

                if meta_type == "open_url":
                    await websocket.send_text(json.dumps([
                        "open_url",
                        {"url": meta["url"], "new_tab": meta.get("new_tab", True)},
                    ]))

            elif packet_type == "P":
                # Packed binary data — forward as-is
                await websocket.send_bytes(payload)

    except (asyncio.IncompleteReadError, WebSocketDisconnect, ConnectionError):
        pass


# --- Health check ---


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "active_sessions": _active_sessions,
        "max_sessions": MAX_CONCURRENT_SESSIONS,
    }


# --- Entry point ---


def main() -> None:
    # Validate required env vars
    required = [
        "GKL_YAHOO_CLIENT_ID",
        "GKL_YAHOO_CLIENT_SECRET",
        "GKL_ENCRYPTION_KEY",
        "GKL_SESSION_SECRET",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(
            f"Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    os.environ.setdefault("GKL_MODE", "web")

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "gkl.web.server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
