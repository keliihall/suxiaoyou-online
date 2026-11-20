"""Shared human-only captcha login for ACP and operator smoke scripts.

An interactive terminal opens a short-lived loopback puzzle page. The page knows
only the images and submitted x, never credentials, API cookies or login proofs.
Noninteractive callers must already have a valid session, or may explicitly set
SUXIAOYOU_CLOUD_SESSION_FILE to an existing owner-only ACP {origin,cookie} file.
No session/configuration is written here and no captcha-solving bypass exists.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

PREFIX = "/api/cloud"
_PNG = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/=]+$")


class HumanCaptchaRequired(RuntimeError):
    """Safe operator-facing error; contains no request bodies or credentials."""


def _origin(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise HumanCaptchaRequired("登录地址必须是一个完整的 HTTP(S) origin。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HumanCaptchaRequired("远程登录必须使用 HTTPS。")
    return value.rstrip("/")


def _load_saved_session(client, session_file: Path | None = None) -> None:
    configured = session_file if session_file is not None else os.environ.get("SUXIAOYOU_CLOUD_SESSION_FILE")
    if not configured:
        return
    try:
        fd = os.open(Path(configured), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()
                    or info.st_size > 16 * 1024):
                raise ValueError()
            data = json.load(source)
        if _origin(data["origin"]) != _origin(str(client.base_url)):
            raise ValueError()
        cookie = data.get("cookie")
        if not isinstance(cookie, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cookie):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise HumanCaptchaRequired("保存的会话无效，请提供当前地址的本人 0600 ACP 会话文件。") from None
    # Kept exclusively in the original HTTP client; never sent to loopback HTML.
    client.cookies.set("cloud_session", cookie)


def _checked(response) -> dict:
    if response.status_code != 200:
        raise HumanCaptchaRequired(f"登录请求未完成（HTTP {response.status_code}），请重试人工登录。")
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise HumanCaptchaRequired("登录响应无效，请稍后重试。") from None


def _same_account(auth: dict, email: str) -> dict:
    actual = auth.get("user", {}).get("email") if isinstance(auth.get("user"), dict) else None
    if not isinstance(actual, str) or actual.strip().lower() != email.strip().lower() or not isinstance(auth.get("csrf_token"), str) or not auth["csrf_token"]:
        raise HumanCaptchaRequired("保存的会话账户不匹配，请使用目标账户的会话或重新人工登录。")
    return auth


def _require_interactive(interactive: bool | None) -> None:
    if not (sys.stdin.isatty() if interactive is None else interactive):
        raise HumanCaptchaRequired("登录需要人工拼图验证：请在交互终端运行，或通过 SUXIAOYOU_CLOUD_SESSION_FILE 提供有效的本人私有会话。")


@dataclass
class CallbackState:
    """One-use local callback state, independently testable without a server."""
    nonce: str
    port: int
    deadline: float
    max_x: int = 272
    result: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def accept(self, *, host: str, origin: str, path: str, content_type: str, payload: bytes) -> float:
        with self.lock:
            if self.result is not None or time.monotonic() >= self.deadline:
                raise ValueError("Callback expired")
            if (host != f"127.0.0.1:{self.port}" or origin != self.origin
                    or not secrets.compare_digest(path.encode(), ("/complete/" + self.nonce).encode())
                    or content_type.split(";", 1)[0].strip().lower() != "application/json"
                    or len(payload) > 1024):
                raise ValueError("Callback rejected")
            try:
                body = json.loads(payload)
                x = body["x"]
                if (not isinstance(body, dict) or set(body) != {"x"} or isinstance(x, bool)
                        or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= self.max_x):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, OverflowError):
                raise ValueError("Invalid position") from None
            self.result = float(x)
            return self.result


def _page(challenge: dict, nonce: str) -> bytes:
    # Select and validate only display fields: challenge ID/proof/account/cookie
    # cannot accidentally enter the document even if more API fields are added.
    for field_name in ("background_image", "piece_image"):
        image = challenge.get(field_name)
        if not isinstance(image, str) or len(image) > 512 * 1024 or not _PNG.fullmatch(image):
            raise HumanCaptchaRequired("拼图图片无效，请重试。")
    dimensions = {key: challenge.get(key) for key in ("image_width", "image_height", "piece_width", "piece_height", "piece_y")}
    if (dimensions["image_width"] != 320 or dimensions["image_height"] != 160
            or dimensions["piece_width"] != 48 or dimensions["piece_height"] != 48
            or type(dimensions["piece_y"]) is not int or not 0 <= dimensions["piece_y"] <= 112):
        raise HumanCaptchaRequired("拼图尺寸无效，请重试。")
    # Image data URLs contain only the restricted alphabet above; no external
    # URLs, executable HTML, API endpoints or authentication material are used.
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>拼图验证</title>
<style nonce="NONCE">*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;background:#f3f5f8;color:#172033;font:16px system-ui}main{width:min(368px,100%);padding:24px}h1{font-size:20px;margin:0 0 20px}.puzzle{position:relative;width:100%;aspect-ratio:2;overflow:hidden;border-radius:10px}.background{display:block;width:100%}.piece{position:absolute;width:15%;height:30%;left:0;top:PIECEY%}input{display:block;width:100%;min-height:44px;margin:20px 0;touch-action:pan-y}button{width:100%;min-height:44px;border:0;border-radius:10px;background:#2563eb;color:white;font:inherit}button:disabled{background:#dce2eb;color:#526176}input:focus-visible,button:focus-visible{outline:3px solid #2563eb;outline-offset:3px}p{min-height:24px}</style>
<main><h1>拖动拼图完成验证</h1><div class="puzzle"><img class="background" src="BACKGROUND" alt="带缺口的拼图背景"><img class="piece" src="PIECE" alt="待移动拼块"></div><label for="position">拼块位置</label><input id="position" type="range" min="0" max="272" step="1" value="0"><button id="verify">验证</button><p id="status" role="status"></p></main>
<script nonce="NONCE">const slider=document.querySelector('input'),piece=document.querySelector('.piece'),button=document.querySelector('button'),status=document.querySelector('#status');let pending=false;slider.addEventListener('input',()=>piece.style.left=(Number(slider.value)/320*100)+'%');async function submit(){if(pending)return;pending=true;button.disabled=true;slider.disabled=true;try{const response=await fetch('/complete/NONCE',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x:Number(slider.value)})});if(!response.ok)throw Error();status.textContent='已提交，请返回终端。';}catch{status.textContent='提交未完成，请返回终端重新登录。';}}slider.addEventListener('pointerup',submit);slider.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();submit();}});button.addEventListener('click',submit);</script></html>"""
    return (page.replace("NONCE", nonce).replace("PIECEY", str(dimensions["piece_y"] / 160 * 100))
            .replace("BACKGROUND", challenge["background_image"]).replace("PIECE", challenge["piece_image"])).encode()


def _human_position(challenge: dict, stop: threading.Event | None = None) -> float:
    ttl = challenge.get("expires_in")
    if type(ttl) is not int or not 1 <= ttl <= 300:
        raise HumanCaptchaRequired("拼图有效期无效，请重试。")
    nonce = secrets.token_urlsafe(32)
    page = _page(challenge, nonce)
    state = CallbackState(nonce=nonce, port=0, deadline=time.monotonic() + min(ttl, 120))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # URLs contain local nonces; no access/request logs are written.

        def reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; script-src 'nonce-" + nonce + "'; style-src 'nonce-" + nonce + "'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if (self.headers.get("Host") != f"127.0.0.1:{state.port}"
                    or self.headers.get("Origin") not in (None, state.origin)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or not secrets.compare_digest(self.path.encode(), ("/captcha/" + nonce).encode())
                    or state.result is not None or time.monotonic() >= state.deadline):
                return self.reply(403)
            self.reply(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                state.accept(host=self.headers.get("Host", ""), origin=self.headers.get("Origin", ""),
                             path=self.path, content_type=self.headers.get("Content-Type", ""),
                             payload=self.rfile.read(length))
            except (ValueError, OSError):
                return self.reply(403)
            self.reply(200, b"{}", "application/json")

    class LoopbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

        def handle_error(self, *_args):
            pass  # No nonce, HTTP body or traceback is emitted by the local server.

    with LoopbackServer(("127.0.0.1", 0), Handler) as server:
        state.port = server.server_port
        server.timeout = .25
        print("请在打开的浏览器中完成人工拼图验证，然后返回终端。", file=sys.stderr)
        threading.Thread(target=webbrowser.open, args=(state.origin + "/captcha/" + nonce,), daemon=True).start()
        while time.monotonic() < state.deadline and not (stop and stop.is_set()):
            server.handle_request()
            if state.result is not None:
                return state.result
    raise HumanCaptchaRequired("人工拼图验证已超时或取消，请重新登录。")


def _login_body(email, password, proof):
    password = password() if callable(password) else password
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        raise HumanCaptchaRequired("请输入有效密码。")
    return {"email": email, "password": password, "captcha_proof": proof}


def login_sync(client, email: str, password, *, interactive: bool | None = None, session_file: Path | None = None) -> dict:
    _origin(str(client.base_url))
    _load_saved_session(client, session_file)
    current = client.get(PREFIX + "/auth/me", timeout=10)
    if current.status_code == 200:
        return _same_account(_checked(current), email)
    if current.status_code != 401:
        _checked(current)
    _require_interactive(interactive)
    challenge = _checked(client.get(PREFIX + "/auth/captcha/challenge", timeout=10))
    x = _human_position(challenge)
    verified = _checked(client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": challenge["challenge_id"], "x": x}, timeout=10))
    auth = _checked(client.post(PREFIX + "/auth/login", json=_login_body(email, password, verified["proof"]), timeout=10))
    return _same_account(auth, email)


async def login(client, email: str, password, *, interactive: bool | None = None, session_file: Path | None = None) -> dict:
    _origin(str(client.base_url))
    _load_saved_session(client, session_file)
    current = await client.get(PREFIX + "/auth/me", timeout=10)
    if current.status_code == 200:
        return _same_account(_checked(current), email)
    if current.status_code != 401:
        _checked(current)
    _require_interactive(interactive)
    challenge = _checked(await client.get(PREFIX + "/auth/captcha/challenge", timeout=10))
    stop = threading.Event()
    try:
        x = await asyncio.to_thread(_human_position, challenge, stop)
    finally:
        stop.set()
    verified = _checked(await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": challenge["challenge_id"], "x": x}, timeout=10))
    auth = _checked(await client.post(PREFIX + "/auth/login", json=_login_body(email, password, verified["proof"]), timeout=10))
    return _same_account(auth, email)
