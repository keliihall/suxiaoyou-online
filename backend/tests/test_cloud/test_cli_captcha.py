"""Operator captcha helper contracts; no browser, socket server or live API."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import time

import httpx
import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
_SPEC = importlib.util.spec_from_file_location("cloud_login", _SCRIPTS / "cloud_login.py")
helper = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = helper
_SPEC.loader.exec_module(helper)

NONCE = "n" * 43
AUTH = {"user": {"email": "operator@example.com"}, "csrf_token": "synthetic-csrf"}
CHALLENGE = {"challenge_id": "synthetic-challenge", "background_image": "data:image/png;base64,YWJj",
             "piece_image": "data:image/png;base64,ZGVm", "image_width": 320, "image_height": 160,
             "piece_width": 48, "piece_height": 48, "piece_y": 52, "expires_in": 120}


@pytest.fixture(autouse=True)
def no_real_session_or_browser(monkeypatch):
    monkeypatch.delenv("SUXIAOYOU_CLOUD_SESSION_FILE", raising=False)
    monkeypatch.setattr(helper.webbrowser, "open", lambda *_args, **_kwargs: pytest.fail("A real browser must never open"))


def state():
    return helper.CallbackState(nonce=NONCE, port=34567, deadline=time.monotonic() + 120)


def callback(**changes):
    return {"host": "127.0.0.1:34567", "origin": "http://127.0.0.1:34567",
            "path": "/complete/" + NONCE, "content_type": "application/json", "payload": b'{"x":137.5}', **changes}


def test_callback_is_one_use_and_keeps_only_x():
    current = state()
    assert current.accept(**callback()) == 137.5
    assert current.result == 137.5
    with pytest.raises(ValueError):
        current.accept(**callback())


@pytest.mark.parametrize("changes", [
    {"host": "attacker.invalid:34567"}, {"host": "127.0.0.1:1"}, {"host": "localhost:34567"},
    {"origin": "https://attacker.invalid"}, {"origin": "null"}, {"origin": ""},
    {"origin": "http://127.0.0.1:1"}, {"path": "/complete/wrong"}, {"path": "/complete/" + NONCE + "?x=1"},
    {"path": "/complete/错"}, {"content_type": "text/plain"}, {"payload": b"[]"},
    {"payload": b'{"x":true}'}, {"payload": b'{"x":-1}'}, {"payload": b'{"x":273}'},
    {"payload": b'{"x":"137"}'}, {"payload": b'{"x":NaN}'}, {"payload": b'{"x":Infinity}'},
    {"payload": b'{"x":137,"cookie":"forged"}'}, {"payload": b"a" * 1025}, {"payload": b"malformed"},
])
def test_callback_rejects_rebinding_cross_origin_wrong_nonce_and_invalid_x(changes):
    current = state()
    with pytest.raises(ValueError):
        current.accept(**callback(**changes))
    assert current.result is None


def test_callback_deadline_and_concurrent_replay():
    expired = state()
    expired.deadline = time.monotonic() - 1
    with pytest.raises(ValueError):
        expired.accept(**callback())
    current = state()
    def submit(_):
        try:
            current.accept(**callback())
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(16))) == 1


def test_html_contains_only_puzzle_display_data_and_local_nonce():
    html = helper._page({**CHALLENGE, "password": "PASSWORD-SECRET", "cookie": "COOKIE-SECRET",
                         "proof": "PROOF-SECRET", "api_key": "MODEL-SECRET"}, NONCE).decode()
    assert "type=\"range\"" in html and "/complete/" + NONCE in html
    assert "synthetic-challenge" not in html
    for marker in ("PASSWORD-SECRET", "COOKIE-SECRET", "PROOF-SECRET", "MODEL-SECRET"):
        assert marker not in html
    with pytest.raises(helper.HumanCaptchaRequired):
        helper._page({**CHALLENGE, "background_image": "https://attacker.invalid/image"}, NONCE)


def test_noninteractive_login_fails_before_challenge_or_password_prompt(monkeypatch):
    calls = []
    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(401)
    monkeypatch.setattr(helper, "_human_position", lambda *_: pytest.fail("Must not start loopback server"))
    with httpx.Client(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(helper.HumanCaptchaRequired, match="人工拼图"):
            helper.login_sync(client, "operator@example.com", lambda: pytest.fail("Must not prompt"), interactive=False)
    assert calls == ["/api/cloud/auth/me"]


@pytest.mark.asyncio
async def test_async_noninteractive_fails_without_starting_loopback(monkeypatch):
    monkeypatch.setattr(helper, "_human_position", lambda *_: pytest.fail("Must not start loopback server"))
    async with httpx.AsyncClient(base_url="http://localhost:3000", transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client:
        with pytest.raises(helper.HumanCaptchaRequired, match="人工拼图"):
            await helper.login(client, "operator@example.com", "synthetic-password", interactive=False)


def test_saved_session_requires_origin_private_file_and_matching_identity(tmp_path, monkeypatch):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"origin": "http://127.0.0.1:3000", "cookie": "synthetic-private-cookie"}))
    session.chmod(0o600)
    monkeypatch.setenv("SUXIAOYOU_CLOUD_SESSION_FILE", str(session))
    def transport(request):
        assert "cloud_session=synthetic-private-cookie" in request.headers["cookie"]
        assert request.url.path == "/api/cloud/auth/me"
        return httpx.Response(200, json=AUTH)
    with httpx.Client(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(transport)) as client:
        assert helper.login_sync(client, "OPERATOR@example.com", lambda: pytest.fail("Must not prompt"), interactive=False) == AUTH
        with pytest.raises(helper.HumanCaptchaRequired, match="账户不匹配"):
            helper.login_sync(client, "another@example.com", "synthetic-password", interactive=False)
    session.chmod(0o644)
    with httpx.Client(base_url="http://127.0.0.1:3000") as client:
        with pytest.raises(helper.HumanCaptchaRequired, match="0600"):
            helper._load_saved_session(client)
    session.chmod(0o600)
    with httpx.Client(base_url="http://127.0.0.1:9999") as client:
        with pytest.raises(helper.HumanCaptchaRequired):
            helper._load_saved_session(client)
    link = tmp_path / "link.json"
    link.symlink_to(session)
    monkeypatch.setenv("SUXIAOYOU_CLOUD_SESSION_FILE", str(link))
    with httpx.Client(base_url="http://127.0.0.1:3000") as client:
        with pytest.raises(helper.HumanCaptchaRequired):
            helper._load_saved_session(client)


@pytest.mark.asyncio
async def test_explicit_private_cache_always_checks_target_account_and_rejects_legacy(tmp_path):
    session = tmp_path / "private-cache.json"
    session.write_text(json.dumps({"origin": "http://127.0.0.1:3000", "cookie": "synthetic-private-cookie"}))
    session.chmod(0o600)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=AUTH))) as client:
        with pytest.raises(helper.HumanCaptchaRequired, match="账户不匹配"):
            await helper.login(client, "ordinary@example.com", "synthetic-password", session_file=session, interactive=False)
    session.write_text(json.dumps({"cloud_session": "legacy-cookie-without-origin"}))
    async with httpx.AsyncClient(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(lambda _: pytest.fail("Legacy cache must fail before HTTP"))) as client:
        with pytest.raises(helper.HumanCaptchaRequired):
            await helper.login(client, "operator@example.com", "synthetic-password", session_file=session, interactive=False)


def test_sync_interactive_uses_original_client_for_verify_and_login(monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "_human_position", lambda _: 137)
    def transport(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/me"):
            return httpx.Response(401)
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json=CHALLENGE, headers={"set-cookie": "cloud_login_challenge=synthetic-binding; Path=/; HttpOnly"})
        assert "cloud_login_challenge=synthetic-binding" in request.headers["cookie"]
        if request.url.path.endswith("/verify"):
            assert json.loads(request.content) == {"challenge_id": "synthetic-challenge", "x": 137}
            return httpx.Response(200, json={"proof": "synthetic-proof", "expires_in": 120})
        assert json.loads(request.content) == {"email": "operator@example.com", "password": "synthetic-password", "captcha_proof": "synthetic-proof"}
        return httpx.Response(200, json=AUTH)
    with httpx.Client(base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(transport)) as client:
        assert helper.login_sync(client, "operator@example.com", "synthetic-password", interactive=True) == AUTH
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_async_interactive_uses_human_position_and_proof(monkeypatch):
    stops = []
    def human(_challenge, stop):
        stops.append(stop)
        return 137
    monkeypatch.setattr(helper, "_human_position", human)
    def transport(request):
        if request.url.path.endswith("/me"):
            return httpx.Response(401)
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json=CHALLENGE)
        if request.url.path.endswith("/verify"):
            assert json.loads(request.content)["x"] == 137
            return httpx.Response(200, json={"proof": "synthetic-proof", "expires_in": 120})
        assert json.loads(request.content)["captcha_proof"] == "synthetic-proof"
        return httpx.Response(200, json=AUTH)
    async with httpx.AsyncClient(base_url="http://localhost:3000", transport=httpx.MockTransport(transport)) as client:
        assert await helper.login(client, "operator@example.com", "synthetic-password", interactive=True) == AUTH
    assert stops[0].is_set()


def test_smoke_scripts_all_use_shared_login_and_parse_cleanly():
    import ast
    files = list(_SCRIPTS.glob("cloud-*-smoke.py")) + [_SCRIPTS / "cloud-smoke.py", _SCRIPTS / "cloud-acp.py"]
    for path in files:
        source = path.read_text()
        ast.parse(source, filename=str(path))
        assert '"/api/cloud/auth/login"' not in source and '"/auth/login"' not in source
        assert "from cloud_login import" in source
        assert "client.cookies.update(json.loads(session_path.read_text()))" not in source
        assert 'browser_session.read_text()' not in source
