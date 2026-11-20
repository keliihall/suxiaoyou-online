"""Exercise a REAL managed Mac node through the central accounting code.

Uses a fresh private SQLite database, never the running cloud database. Only
reads the operator-selected node token file; no token/ciphertext is printed.
Run from backend using the cloud Python environment.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud import metering, mlx_nodes
from app.cloud.models import MlxNodeConfig, RequestRecord, TenantRuntime, User
from app.cloud.secrets import ModelSecretVault


async def verify(args):
    os.umask(0o077)
    root = args.node_root.resolve(strict=True)
    token_path = root / "token"
    descriptor = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Node token must be private and operator-owned")
        token = handle.read(1024).strip()
    pem = (root / "tls.crt").read_text()
    _, fingerprint = mlx_nodes.certificate_context(pem)
    run_id = secrets.token_hex(8)
    directory = root / "verification" / run_id
    directory.mkdir(parents=True, mode=0o700)
    settings = CloudSettings(environment="test", auto_migrate=True, cookie_secure=False,
        data_dir=directory, database_url=f"sqlite+aiosqlite:///{directory / 'ledger.sqlite'}")
    database = Database(settings)
    await database.initialize()
    capability = secrets.token_urlsafe(48)
    async with database.sessions() as db:
        db.add(User(id="mlx-verification", email="mlx-verification@private.invalid", display_name="MLX verification",
            password_hash="no-login", token_limit=1000000))
        await db.flush()
        db.add(TenantRuntime(user_id="mlx-verification", capability_hash=hashlib.sha256(capability.encode()).hexdigest(),
            capability_encrypted="verification-no-login",engine_token_encrypted="verification-no-login",
            container_name="verification-no-container",state="running"))
        db.add(MlxNodeConfig(user_id="mlx-verification", url=mlx_nodes.validate_url(args.node_url),
            certificate_pem=pem, certificate_fingerprint=fingerprint,
            token_encrypted=ModelSecretVault(settings).seal(token), enabled=True))
        await db.commit()
    app = FastAPI()
    app.state.settings, app.state.database = settings, database
    app.include_router(mlx_nodes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://private-control-plane",
        headers={"Authorization": "Bearer " + capability}, timeout=120) as client:
        base = "/api/cloud/internal/mlx-node"
        status = await client.get(base + "/rapid-mlx/status")
        assert status.status_code == 200 and status.json()["running"], status.text
        payload = {"model": args.model, "messages": [
            {"role": "system", "content": "Answer arithmetic questions with only the numeric result."},
            {"role": "user", "content": "What is 2 + 3?"}], "max_tokens": 32, "temperature": 0.0}
        response = await client.post(base + "/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["choices"][0]["message"]["content"].strip() == "5", result
        async with database.sessions() as db:
            rows = (await db.scalars(select(RequestRecord))).all()
            summary = await metering.usage_summary(db, "mlx-verification")
            assert len(rows) == 1 and rows[0].state == "settled" and rows[0].source == "mlx-node"
            assert rows[0].total_tokens == result["usage"]["total_tokens"] > 0
            assert summary["used_tokens"] == rows[0].total_tokens and summary["reserved_tokens"] == 0
            assert rows[0].raw_usage["receipt_source"] == "mac_node_proxy"
            record = {"source": rows[0].source, "state": rows[0].state, "total_tokens": rows[0].total_tokens,
                "raw_usage": rows[0].raw_usage, "normalized_usage": rows[0].normalized_usage}
            await db.execute(update(User).where(User.id == "mlx-verification").values(token_limit=0))
            await db.commit()
        rejection = await client.post(base + "/v1/chat/completions", json=payload)
        assert rejection.status_code == 429, rejection.text
        evidence = {"actual_model_inference": True, "transport": "pinned HTTPS to live Mac node",
            "control_plane_http": "ASGI in verification process, isolated migrated SQLite ledger",
            "node_status": status.json(), "model": args.model, "answer": result["choices"][0]["message"]["content"],
            "native_receipt_record": record, "usage": result["usage"], "quota_zero_http_status": rejection.status_code,
            "quota_zero_error": rejection.json(), "certificate_fingerprint": fingerprint,
            "vendor_api_key_used": False, "ledger": str(directory / "ledger.sqlite")}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print("Real Mac generation and central quota passed:", result["usage"]["total_tokens"], "tokens; quota-zero HTTP 429")
    await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-root", required=True, type=Path)
    parser.add_argument("--node-url", default="https://localhost:18443")
    parser.add_argument("--model", default="qwen2.5-0.5b")
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(verify(parser.parse_args()))
