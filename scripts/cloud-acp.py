#!/usr/bin/env python3
"""Bridge ACP NDJSON over stdio to a signed-in Online runtime (POSIX client)."""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect
from cloud_login import HumanCaptchaRequired, login_sync

MAX_FRAME = 1024 * 1024


def origin(value):
    parsed = urlsplit(value.rstrip("/"))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Provide one exact Online HTTP(S) origin")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote ACP login requires HTTPS")
    return value.rstrip("/")


def save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("ACP session must be an owner-only 0600 regular file")
        data = json.load(stream)
    data["origin"] = origin(data["origin"])
    cookie = data.get("cookie", "")
    if not isinstance(cookie, str) or not cookie or len(cookie) > 128 or any(c in cookie for c in "\r\n;\0"):
        raise ValueError("ACP session is invalid")
    return data


async def stdio(path):
    session = load(path)
    reader = asyncio.StreamReader(limit=MAX_FRAME + 1)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    pending, empty = set(), asyncio.Event()
    empty.set()
    url = session["origin"].replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/cloud/acp"
    try:
        async with connect(url, additional_headers={"Cookie": "cloud_session=" + session["cookie"],
            "Origin": session["origin"]}, proxy=None, max_size=MAX_FRAME, max_queue=16) as socket:
            async def receive():
                async for frame in socket:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8")
                    message = json.loads(frame)
                    if "method" not in message and "id" in message:
                        pending.discard((type(message["id"]).__name__, str(message["id"])))
                        if not pending:
                            empty.set()
                    sys.stdout.write(frame.rstrip("\n") + "\n")
                    sys.stdout.flush()
            async def send():
                while line := await reader.readline():
                    if len(line) > MAX_FRAME:
                        raise ValueError("ACP frame exceeds its budget")
                    message = json.loads(line)
                    if "method" in message and "id" in message:
                        pending.add((type(message["id"]).__name__, str(message["id"])))
                        empty.clear()
                    await socket.send(line.decode("utf-8"))
                await asyncio.wait_for(empty.wait(), 300)
            tasks = [asyncio.create_task(fn()) for fn in (send, receive)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", type=Path, default=Path.home() / ".config/suxiaoyou-online/acp-session.json")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login")
    login.add_argument("--url", type=origin, required=True)
    login.add_argument("--email", required=True)
    commands.add_parser("stdio")
    commands.add_parser("logout")
    args = parser.parse_args()
    try:
        if args.command == "login":
            with httpx.Client(base_url=args.url, trust_env=False, timeout=180) as client:
                auth = login_sync(client, args.email, lambda: getpass.getpass("Password: "))
                client.headers["x-csrf-token"] = auth["csrf_token"]
                client.post("/api/cloud/runtime/start").raise_for_status()
                save(args.session_file, {"origin": args.url, "cookie": client.cookies["cloud_session"]})
            print("ACP session saved privately.", file=sys.stderr)
        elif args.command == "logout":
            session = load(args.session_file)
            with httpx.Client(base_url=session["origin"], trust_env=False, cookies={"cloud_session": session["cookie"]}) as client:
                result = client.get("/api/cloud/auth/me")
                if result.status_code == 200:
                    client.post("/api/cloud/auth/logout", headers={"x-csrf-token": result.json()["csrf_token"]}).raise_for_status()
            args.session_file.unlink(missing_ok=True)
        else:
            asyncio.run(stdio(args.session_file))
    except Exception as error:
        # Stdout is exclusively ACP. No URL, cookie, password or vendor detail
        # is included in transport/startup diagnostics.
        print("Cloud ACP stopped: " + (str(error) if isinstance(error, HumanCaptchaRequired) else type(error).__name__), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
