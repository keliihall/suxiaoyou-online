"""Test-only puzzle solving: exercise HTTP endpoints against isolated test SQL.

The answer is read from the fixture database, never from a production endpoint or
a bypass. This keeps unrelated cloud tests on the same enforced login path.
"""
import hashlib

from app.cloud.models import LoginCaptcha


async def _answer(database, challenge_id):
    async with database.sessions() as db:
        record = await db.get(LoginCaptcha, hashlib.sha256(challenge_id.encode()).hexdigest())
        assert record is not None and record.kind == "challenge"
        return record.target_x


async def captcha_proof(client, database):
    challenge = await client.get("/api/cloud/auth/captcha/challenge")
    assert challenge.status_code == 200, challenge.text
    challenge_id = challenge.json()["challenge_id"]
    x = await _answer(database, challenge_id)
    result = await client.post("/api/cloud/auth/captcha/verify", json={"challenge_id": challenge_id, "x": x})
    assert result.status_code == 200, result.text
    return result.json()["proof"]


def sync_captcha_proof(client, database):
    challenge = client.get("/api/cloud/auth/captcha/challenge")
    assert challenge.status_code == 200, challenge.text
    challenge_id = challenge.json()["challenge_id"]
    x = client.portal.call(_answer, database, challenge_id)
    result = client.post("/api/cloud/auth/captcha/verify", json={"challenge_id": challenge_id, "x": x})
    assert result.status_code == 200, result.text
    return result.json()["proof"]
