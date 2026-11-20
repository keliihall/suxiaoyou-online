#!/usr/bin/env python3
"""One-time, non-inference tenant refresh for the reviewed public HTTPS origin.

Run in the current API as UID 10001: python - --mode preflight|restart.
The operator must keep ingress in maintenance during both phases. Preflight
only reads DB/Docker/engine state and creates its private operational snapshot.
Restart rechecks quiescence, claims that snapshot once, then uses the existing
supervisor to replace running tenant containers with their original volumes.
No prompt, provider call, Web login, cancellation, or automatic retry is issued.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys


OLD_ORIGIN = "https://192.168.1.211:8443"
TARGET_ORIGIN = "https://nav.cyneu.com:18443"
DATA_ROOT = Path("/var/lib/suxiaoyou")
REPORT_DIR = DATA_ROOT / "deployment-checks"
SNAPSHOT = REPORT_DIR / "public-origin-runtime.json"
LOCK_NAME = "public-origin-runtime.lock"
MAX_SNAPSHOT = 1024 * 1024
MAX_ROWS = 100000
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
USER_ID = re.compile(r"[0-9a-f]{32}\Z")
TENANT_LABEL = "io.suxiaoyou.cloud.owner"
GENERATION_LABEL = "io.suxiaoyou.cloud.generation"


class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Failure(code)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private(metadata, directory=False):
    return (metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == (0o700 if directory else 0o600)
            and (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)))


@contextmanager
def snapshot_lock():
    metadata = DATA_ROOT.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and not metadata.st_mode & 0o022, "unsafe_data_root")
    try:
        REPORT_DIR.mkdir(mode=0o700)
    except FileExistsError:
        pass
    require(private(REPORT_DIR.lstat(), True), "unsafe_snapshot_directory")
    directory_fd = os.open(REPORT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        descriptor = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory_fd)
        require(private(os.fstat(descriptor)), "unsafe_snapshot_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("another_origin_operation_is_running") from None
        yield directory_fd
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def read_snapshot(directory_fd):
    descriptor = os.open(SNAPSHOT.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as source:
        require(private(os.fstat(source.fileno())), "unsafe_snapshot_file")
        raw = source.read(MAX_SNAPSHOT + 1)
    require(len(raw) <= MAX_SNAPSHOT, "snapshot_too_large")
    document = json.loads(raw)
    require(isinstance(document, dict) and document.get("format_version") == 1
            and document.get("kind") == "public-origin-runtime"
            and document.get("source_origin") == OLD_ORIGIN
            and document.get("target_origin") == TARGET_ORIGIN, "snapshot_contract_mismatch")
    return document


def save_snapshot(directory_fd, document, *, create=False):
    raw = json.dumps(document, sort_keys=True, ensure_ascii=False).encode() + b"\n"
    require(len(raw) <= MAX_SNAPSHOT, "snapshot_too_large")
    temporary = ".public-origin-runtime." + secrets.token_hex(16) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if create:
            try:
                os.link(temporary, SNAPSHOT.name, src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                raise Failure("preflight_snapshot_already_exists") from None
        else:
            require(private(os.stat(SNAPSHOT.name, dir_fd=directory_fd, follow_symlinks=False)),
                    "unsafe_snapshot_file")
            os.replace(temporary, SNAPSHOT.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


# Execute exactly this read-only probe inside the already owner-verified engine.
# It works before the recreated API has joined the tenant's private network.
# Response documents and the service credential never cross this subprocess.
ENGINE_PROBE = r'''
import json, os, urllib.request
def run():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    if not token.startswith("suxiaoyou_st_") or len(token) < 50:
        raise ValueError()
    def get(path):
        request = urllib.request.Request("http://127.0.0.1:8080" + path,
            headers={"Authorization": "Bearer " + token, "User-Agent": "Suxiaoyou-Origin-Maintenance/1"})
        with opener.open(request, timeout=8) as response:
            if response.status != 200 or response.geturl() != request.full_url:
                raise ValueError()
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        return json.loads(raw)
    active, automations = get("/api/chat/active"), get("/api/automations")
    context = get("/api/cloud-runtime/files/context")
    if not isinstance(active, list) or not isinstance(automations, list) or not isinstance(context, dict):
        raise ValueError()
    if any(not isinstance(item, dict) or not isinstance(item.get("enabled"), bool) for item in automations):
        raise ValueError()
    return {"ok": True, "active_streams": len(active),
        "active_automations": sum(item.get("last_run_status") == "running" for item in automations),
        "enabled_automations": sum(item["enabled"] for item in automations),
        "office_ready": isinstance(context.get("office_cloud"), dict) and context["office_cloud"].get("ready") is True}
try:
    result = run()
except BaseException:
    print('{"ok":false,"failure":"engine_read_probe_failed"}')
    raise SystemExit(1) from None
print(json.dumps(result))
'''


async def engine_probe(supervisor, name):
    try:
        output = await supervisor._docker("exec", "--user", "10001:10001", name,
                                          "python", "-c", ENGINE_PROBE, timeout=35)
    except (Exception, TimeoutError):
        raise Failure("engine_read_probe_failed") from None
    require(isinstance(output, str) and len(output) <= 4096, "invalid_engine_probe")
    result = json.loads(output)
    require(isinstance(result, dict) and set(result) == {
        "ok", "active_streams", "active_automations", "enabled_automations", "office_ready"}
        and result["ok"] is True and isinstance(result["office_ready"], bool)
        and all(type(result[name]) is int and 0 <= result[name] <= MAX_ROWS for name in
                ("active_streams", "active_automations", "enabled_automations")), "invalid_engine_probe")
    require(result["active_streams"] == 0 and result["active_automations"] == 0, "engine_has_active_work")
    # A scheduled task could start between checks; require an explicit idle
    # maintenance window rather than silently disabling an enabled automation.
    require(result["enabled_automations"] == 0, "enabled_automations_prevent_maintenance")
    require(result["office_ready"], "engine_office_not_ready")
    return result


async def database_snapshot(database):
    from sqlalchemy import select, text
    from app.cloud.models import (AuthSession, ImageQuotaBucket, ImageReservation, Job,
        QuotaBucket, RequestRecord, Reservation, TenantRuntime, UsageEvent, User)

    async with database.sessions() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

        async def rows(*columns):
            result = (await session.execute(select(*columns).order_by(columns[0]).limit(MAX_ROWS + 1))).all()
            require(len(result) <= MAX_ROWS, "database_snapshot_limit_exceeded")
            return [list(row) for row in result]

        requests = await rows(RequestRecord.id, RequestRecord.user_id, RequestRecord.state,
                              RequestRecord.total_tokens, RequestRecord.cost_microusd)
        usage = await rows(UsageEvent.request_id, UsageEvent.user_id, UsageEvent.total_tokens, UsageEvent.cost_microusd)
        buckets = await rows(QuotaBucket.id, QuotaBucket.user_id, QuotaBucket.period,
                             QuotaBucket.used_tokens, QuotaBucket.reserved_tokens, QuotaBucket.revision)
        reservations = await rows(Reservation.id, Reservation.request_id, Reservation.bucket_id,
                                  Reservation.amount, Reservation.state)
        image_buckets = await rows(ImageQuotaBucket.id, ImageQuotaBucket.user_id, ImageQuotaBucket.period,
                                   ImageQuotaBucket.used_images, ImageQuotaBucket.reserved_images, ImageQuotaBucket.revision)
        image_reservations = await rows(ImageReservation.request_id, ImageReservation.user_id,
                                        ImageReservation.bucket_id, ImageReservation.state)
        jobs = await rows(Job.id, Job.user_id, Job.state, Job.fencing)
        users = await rows(User.id, User.is_active)
        tenants = await rows(TenantRuntime.user_id, TenantRuntime.generation,
                             TenantRuntime.container_name, TenantRuntime.container_id, TenantRuntime.state)
        from sqlalchemy import func
        sessions = int(await session.scalar(select(func.count()).select_from(AuthSession)))

    require(all(row[2] in {"settled", "released"} for row in requests), "unsettled_or_active_requests")
    require(all(row[4] in {"settled", "released"} for row in reservations), "unsettled_reservations")
    require(all(row[3] in {"settled", "released"} for row in image_reservations), "unsettled_image_reservations")
    require(all(row[2] not in {"queued", "running"} for row in jobs), "active_cloud_jobs")
    require(all(row[4] == 0 for row in buckets + image_buckets), "reserved_quota_is_not_zero")
    settled = {row[0]: row for row in requests if row[2] == "settled"}
    require(len(usage) == len(settled) and all(row[0] in settled and row[1] == settled[row[0]][1]
        and row[2:] == settled[row[0]][3:] for row in usage), "settled_usage_ledger_mismatch")
    require(sum(row[3] for row in buckets) == sum(row[3] for row in settled.values()), "token_bucket_ledger_mismatch")
    require(sum(row[3] for row in image_buckets) == sum(row[3] == "settled" for row in image_reservations),
            "image_bucket_ledger_mismatch")
    require(all(USER_ID.fullmatch(row[0]) and isinstance(row[1], bool) for row in users), "invalid_user_inventory")
    active = {row[0] for row in users if row[1]}
    require(all(row[4] in {"running", "stopped", "failed"} for row in tenants), "tenant_transition_in_progress")
    require(all(row[0] in active for row in tenants if row[4] == "running"), "inactive_owner_has_running_tenant")
    public_billing = {"request_count": len(requests), "settled_requests": len(settled),
        "released_requests": len(requests) - len(settled), "settled_tokens": sum(row[3] for row in settled.values()),
        "settled_cost_microusd": sum(row[4] for row in settled.values()),
        "used_tokens": sum(row[3] for row in buckets), "reserved_tokens": 0,
        "used_images": sum(row[3] for row in image_buckets), "reserved_images": 0,
        "ledger_sha256": digest([requests, usage, buckets, reservations, image_buckets, image_reservations, jobs, users])}
    return {"billing": public_billing, "web_auth_sessions": sessions, "inventory": tenants}


async def control_identity(supervisor):
    raw = await supervisor._docker("container", "inspect", os.uname().nodename)
    info = json.loads(raw)[0]
    labels = info.get("Config", {}).get("Labels") or {}
    require(labels.get("com.docker.compose.project") == "suxiaoyou-cloud"
        and labels.get("com.docker.compose.service") == "api"
        and str(labels.get("com.docker.compose.oneoff", "")).lower() == "false"
        and info.get("State", {}).get("Running") is True, "current_api_container_required")
    require(re.fullmatch(r"[0-9a-f]{64}", info.get("Id", "")), "invalid_current_api_id")
    import httpx
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        response = await client.get("http://127.0.0.1:8000/readyz")
    require(response.status_code == 200 and response.json().get("mode") == "cloud", "current_api_not_ready")
    return info["Id"]


async def image_identity(supervisor):
    raw = await supervisor._docker("image", "inspect", supervisor.settings.runtime_image)
    info = json.loads(raw)[0]
    require(SHA.fullmatch(info.get("Id", "")) and info.get("Architecture") == "amd64"
            and info.get("Os") == "linux"
            and supervisor.settings.runtime_image in (info.get("RepoTags") or []),
            "configured_runtime_image_unavailable")
    return info["Id"]


async def inspect_tenants(supervisor, snapshot, image_id, origin):
    result = []
    for user_id, generation, name, container_id, state in snapshot["inventory"]:
        require(USER_ID.fullmatch(user_id) and type(generation) is int and generation >= 1, "invalid_tenant_inventory")
        owner = hashlib.sha256(user_id.encode()).hexdigest()
        expected_name, volume = "sxy-user-" + owner[:32], "sxy-data-" + owner[:32]
        require(name == expected_name, "tenant_container_name_mismatch")
        if state != "running":
            continue
        record = await supervisor.record(user_id)
        require(record is not None and record.generation == generation and record.state == state,
                "tenant_inventory_changed")
        info = await supervisor._inspect(record)
        require(info is not None and info.get("State", {}).get("Running") is True
            and info.get("Id") == container_id and SHA.fullmatch(info.get("Image", "")),
            "tenant_container_or_image_changed")
        config, host = info.get("Config", {}), info.get("HostConfig", {})
        labels = config.get("Labels") or {}
        require(labels.get(TENANT_LABEL) == owner
            and labels.get(GENERATION_LABEL) == str(generation), "tenant_labels_mismatch")
        mounts = info.get("Mounts", [])
        require(len(mounts) == 1 and mounts[0].get("Type") == "volume" and mounts[0].get("Name") == volume
                and mounts[0].get("Destination") == "/tenant", "tenant_private_volume_mismatch")
        require(config.get("Image") == supervisor.settings.runtime_image
                and config.get("User") == "10001:10001" and host.get("ReadonlyRootfs") is True
                and host.get("Privileged") is False and set(host.get("CapDrop") or []) == {"ALL"}
                and not host.get("PortBindings"), "tenant_isolation_changed")
        volume_raw = await supervisor._docker("volume", "inspect", volume)
        require((json.loads(volume_raw)[0].get("Labels") or {}).get(TENANT_LABEL) == owner,
                "tenant_volume_owner_mismatch")
        environment = config.get("Env") or []
        for field in ("CLOUD_ENGINE_ALLOWED_ORIGIN", "SUXIAOYOU_OAUTH_CALLBACK_ORIGIN"):
            require([entry for entry in environment if entry.startswith(field + "=")] == [field + "=" + origin],
                    "tenant_origin_mismatch")
        await engine_probe(supervisor, name)
        result.append({"owner_id": owner, "volume_name": volume, "generation": generation,
                       "container_state": "running", "image_id": info["Image"]})
    require(len(result) <= 1000, "too_many_running_tenants")
    return result


def public_result(mode, document):
    current = document["after"] if mode == "restart" else document["before"]
    return {"complete": True, "ready": True, "mode": mode, "stage": document["stage"],
        "active_users": len(current["tenants"]), "active_requests": 0, "active_jobs": 0,
        "active_streams": 0, "reserved_tokens": 0, "source_origin": OLD_ORIGIN,
        "target_origin": TARGET_ORIGIN, "tenants": current["tenants"], "billing": current["billing"],
        "web_auth_sessions": current["web_auth_sessions"], "snapshot_path": str(SNAPSHOT),
        **({"restarted_users": len(document["applied"])} if mode == "restart" else {})}


def unchanged(before, after):
    require(before["billing"] == after["billing"], "billing_changed_since_preflight")
    require(before["web_auth_sessions"] == after["web_auth_sessions"], "web_auth_session_count_changed")


async def operate(mode, state, directory_fd):
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.office_gateway import OfficeGateway
    from app.cloud.runtime import RuntimeSupervisor

    settings = CloudSettings()
    require(settings.environment == "production" and settings.cookie_secure is True and not settings.allow_mock_provider
            and not settings.auto_migrate and settings.full_runtime_enabled and settings.runtime_network_mode
            and settings.database_url.startswith("postgresql+asyncpg://") and Path(settings.data_dir) == DATA_ROOT,
            "production_configuration_required")
    require(settings.allowed_origin == (OLD_ORIGIN if mode == "preflight" else TARGET_ORIGIN),
            "api_origin_not_ready_for_phase")
    database = Database(settings)
    try:
        await database.initialize()
        supervisor = RuntimeSupervisor(database, settings)
        document = None
        if mode == "restart":
            document = read_snapshot(directory_fd)
            require(document.get("stage") == "preflight_ready" and document.get("applied") == [],
                    "snapshot_already_claimed_do_not_retry")
        control = await control_identity(supervisor)
        image_id = await image_identity(supervisor)
        before = await database_snapshot(database)
        origin = OLD_ORIGIN
        tenants = await inspect_tenants(supervisor, before, image_id, origin)
        stable = await database_snapshot(database)
        unchanged(before, stable)
        require(before["inventory"] == stable["inventory"], "tenant_inventory_changed")
        before["tenants"] = tenants
        if mode == "preflight":
            document = {"format_version": 1, "kind": "public-origin-runtime", "stage": "preflight_ready",
                "source_origin": OLD_ORIGIN, "target_origin": TARGET_ORIGIN, "created_at_utc": utcnow(),
                "preflight_api_id": control, "runtime_image_id": image_id, "before": before, "applied": []}
            save_snapshot(directory_fd, document, create=True)
            return public_result(mode, document)

        require(document.get("runtime_image_id") == image_id and document.get("before") == before,
                "preflight_state_changed")
        require(document.get("preflight_api_id") != control, "api_was_not_recreated")
        # Existing private signing/master material is mandatory. Do not let the
        # existing constructors create replacement keys during this operation.
        require(private((DATA_ROOT / "secrets").lstat(), True), "unsafe_existing_secret_directory")
        for filename in ("model-master.key", "office-signing.key"):
            require(private((DATA_ROOT / "secrets" / filename).lstat()), "existing_signing_material_required")
        supervisor.office_gateway = OfficeGateway(settings)
        document.update(stage="applying", restart_api_id=control, apply_started_at_utc=utcnow())
        save_snapshot(directory_fd, document)
        state.update(stage="applying", document=document, claimed=True)

        for old in tenants:
            current = await database_snapshot(database)
            unchanged(document["before"], current)
            user_id = next(row[0] for row in current["inventory"]
                if hashlib.sha256(row[0].encode()).hexdigest() == old["owner_id"])
            record = await supervisor.record(user_id)
            require(record is not None and record.generation == old["generation"] and record.state == "running",
                    "tenant_changed_before_restart")
            await engine_probe(supervisor, record.container_name)
            document["current_owner_id"] = old["owner_id"]
            save_snapshot(directory_fd, document)
            status = await supervisor.start(user_id, restart=True)
            require(status.get("ready") is True, "restarted_tenant_not_ready")
            record = await supervisor.record(user_id)
            require(record is not None and record.generation == old["generation"] + 1, "tenant_generation_not_incremented")
            partial = await database_snapshot(database)
            unchanged(document["before"], partial)
            only = dict(partial, inventory=[row for row in partial["inventory"] if row[0] == user_id])
            refreshed = await inspect_tenants(supervisor, only, image_id, TARGET_ORIGIN)
            require(len(refreshed) == 1 and refreshed[0]["volume_name"] == old["volume_name"]
                    and refreshed[0]["image_id"] == old["image_id"], "tenant_volume_or_image_changed")
            document["applied"].append(refreshed[0])
            save_snapshot(directory_fd, document)

        after = await database_snapshot(database)
        unchanged(document["before"], after)
        expected_inventory = [list(row) for row in document["before"]["inventory"]]
        old_running = {item["owner_id"] for item in tenants}
        require(len(after["inventory"]) == len(expected_inventory), "tenant_inventory_changed")
        for old, new in zip(expected_inventory, after["inventory"]):
            require(old[0] == new[0] and old[2] == new[2] and old[4] == new[4], "tenant_inventory_changed")
            if hashlib.sha256(old[0].encode()).hexdigest() in old_running:
                require(new[1] == old[1] + 1 and new[3] != old[3], "tenant_refresh_identity_mismatch")
            else:
                require(old == new, "unselected_tenant_changed")
        after["tenants"] = await inspect_tenants(supervisor, after, image_id, TARGET_ORIGIN)
        require(after["tenants"] == document["applied"], "refreshed_inventory_mismatch")
        document.update(stage="complete", after=after, completed_at_utc=utcnow())
        document.pop("current_owner_id", None)
        save_snapshot(directory_fd, document)
        state["stage"] = "complete"
        return public_result(mode, document)
    finally:
        await database.close()


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    mode = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--mode" else "invalid"
    state = {"stage": "preflight" if mode == "preflight" else "restart_precheck", "claimed": False}
    try:
        require(mode in {"preflight", "restart"}, "mode_argument_required")
        require(os.getuid() == 10001 and os.geteuid() == 10001, "run_inside_current_api_as_service_uid")
        with snapshot_lock() as directory_fd:
            try:
                async def bounded():
                    async with asyncio.timeout(600):
                        return await operate(mode, state, directory_fd)
                result = asyncio.run(bounded())
            except BaseException as exc:
                if state["claimed"]:
                    document = state["document"]
                    document.update(stage="failed", failure=(exc.code if isinstance(exc, Failure) else "unexpected_failure"),
                                    failed_at_utc=utcnow())
                    try:
                        save_snapshot(directory_fd, document)
                    except BaseException:
                        pass  # Prior durable applying state also refuses another apply.
                    state["stage"] = "failed"
                raise
    except BaseException as exc:
        print(json.dumps({"complete": False, "ready": False, "mode": mode if mode != "invalid" else None,
            "stage": state["stage"], "failure": exc.code if isinstance(exc, Failure) else "unexpected_failure",
            "snapshot_path": str(SNAPSHOT)}))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
