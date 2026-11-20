"""Private, non-inference runtime maintenance for the 2026-11-17 incident release.

The host controller prepends PUBLIC_SOURCE (reviewed bytes) and REQUEST (dict),
then sends this source to the existing API's python stdin. All output contains
private operational metadata and must be captured privately by that controller.
"""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
from types import ModuleType

PUBLIC_SHA256 = "d314324ace60022b2fd9a4a8509d53baee676432bb998bb1fd0791f0de402fcd"
OPERATION = "20261117-results"
ORIGIN = "https://nav.cyneu.com:18443"
DATA = Path("/var/lib/suxiaoyou")
REPORTS = DATA / "deployment-checks"
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
TAG = re.compile(r"suxiaoyou-online-runtime:[A-Za-z0-9_.-]{1,100}\Z")
MAX_DOCUMENT = 4 * 1024 * 1024

# Only counts and hashes leave the owning engine. The deployment must preserve
# every original transcript row while the additive projection schema migrates.
TRANSCRIPT_PROBE = r'''
import hashlib, json, sqlite3
connection = sqlite3.connect("file:/tenant/state/data/suxiaoyou.db?mode=ro", uri=True)
try:
    connection.execute("BEGIN")
    result = {}
    for table in ("message", "part"):
        rows = connection.execute("SELECT * FROM " + table + " ORDER BY id").fetchall()
        raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(raw).hexdigest()}
    print(json.dumps(result, separators=(",", ":")))
finally:
    connection.close()
'''


class Failure(Exception):
    def __init__(self, code):
        self.code = code


def require(condition, code):
    if not condition:
        raise Failure(code)


def public_module():
    raw = globals().get("PUBLIC_SOURCE")
    require(isinstance(raw, bytes) and hashlib.sha256(raw).hexdigest() == PUBLIC_SHA256, "public_helper_changed")
    module = ModuleType("suxiaoyou_incident_reviewed_public_helper")
    module.__file__ = "<reviewed-public-origin-runtime>"
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def request_values():
    value = globals().get("REQUEST")
    require(isinstance(value, dict) and set(value) == {
        "mode", "operation_id", "origin", "api_image_id", "runtime_image", "images", "baseline", "rollback"
    }, "invalid_request")
    require(value["mode"] in {"snapshot", "refresh", "verify"} and value["operation_id"] == OPERATION
            and value["origin"] == ORIGIN and type(value["rollback"]) is bool
            and isinstance(value["api_image_id"], str) and SHA.fullmatch(value["api_image_id"])
            and isinstance(value["runtime_image"], str) and TAG.fullmatch(value["runtime_image"])
            and isinstance(value["images"], dict) and len(value["images"]) == 2
            and value["runtime_image"] in value["images"]
            and all(isinstance(tag, str) and TAG.fullmatch(tag) and isinstance(identity, str) and SHA.fullmatch(identity)
                    for tag, identity in value["images"].items()), "invalid_request")
    require((value["baseline"] is None and value["mode"] == "snapshot" and value["rollback"] is False)
            or (isinstance(value["baseline"], dict) and (value["mode"] != "snapshot" or value["rollback"])), "invalid_baseline")
    require(value["rollback"] is False,
            "schema_rollback_requires_reviewed_offline_projection_downgrade")
    return value


@contextmanager
def operation_lock(public):
    require(os.getuid() == os.geteuid() == 10001, "api_uid_required")
    require(DATA.is_dir() and DATA.lstat().st_uid == 10001 and not DATA.lstat().st_mode & 0o022, "unsafe_data_root")
    try:
        REPORTS.mkdir(mode=0o700)
    except FileExistsError:
        pass
    require(public.private(REPORTS.lstat(), True), "unsafe_report_directory")
    directory_fd = os.open(REPORTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = os.open(OPERATION + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory_fd)
    try:
        require(public.private(os.fstat(descriptor)) and os.fstat(descriptor).st_nlink == 1, "unsafe_operation_lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory_fd
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def progress_name(rollback):
    return OPERATION + ("-rollback.json" if rollback else "-forward.json")


def read_progress(public, directory_fd, rollback):
    descriptor = os.open(progress_name(rollback), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(public.private(info) and info.st_nlink == 1 and 0 < info.st_size <= MAX_DOCUMENT, "unsafe_progress_file")
        raw = source.read(MAX_DOCUMENT + 1)
    require(len(raw) == info.st_size, "progress_file_changed")
    result = json.loads(raw)
    require(result.get("operation_id") == OPERATION and result.get("rollback") is rollback, "progress_contract_changed")
    return result


def save_progress(public, directory_fd, value, *, create=False):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(raw) <= MAX_DOCUMENT, "progress_budget_exceeded")
    name = progress_name(value["rollback"])
    temporary = ".incident-" + secrets.token_hex(12)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if create:
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        else:
            require(public.private(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)), "unsafe_progress_file")
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


async def image(supervisor, reference):
    raw = await supervisor._docker("image", "inspect", reference)
    value = json.loads(raw)[0]
    require(value.get("Os") == "linux" and value.get("Architecture") == "amd64"
            and isinstance(value.get("Id"), str) and SHA.fullmatch(value["Id"])
            and value.get("RootFS", {}).get("Type") == "layers"
            and isinstance(value.get("Config"), dict), "invalid_runtime_image")
    return value


async def image_catalog(supervisor, request):
    result = {}
    for tag, identity in request["images"].items():
        value = await image(supervisor, tag)
        require(value["Id"] == identity and tag in (value.get("RepoTags") or []), "runtime_tag_changed")
        result[tag] = value
    return result


async def bind_container_image(supervisor, info, catalog):
    tag = info.get("Config", {}).get("Image")
    require(tag in catalog and isinstance(info.get("Image"), str) and SHA.fullmatch(info["Image"]), "unrecognized_tenant_image")
    # Docker 29 may expose a manifest descriptor for image.Id while a running
    # container reports its configuration identity. Resolve that actual image
    # and compare its configuration/filesystem, never blindly equate the IDs.
    actual = await image(supervisor, info["Image"])
    require(actual["Config"] == catalog[tag]["Config"] and actual["RootFS"] == catalog[tag]["RootFS"],
            "tenant_image_binding_changed")
    return tag


async def probe(public, supervisor, name, *, office_required=True):
    raw = await supervisor._docker("exec", "--user", "10001:10001", name,
                                  "python", "-c", public.ENGINE_PROBE, timeout=35)
    require(isinstance(raw, str) and len(raw) <= 4096, "invalid_engine_probe")
    value = json.loads(raw)
    require(value.get("ok") is True and isinstance(value.get("office_ready"), bool)
            and all(type(value.get(name)) is int and value[name] == 0
                    for name in ("active_streams", "active_automations", "enabled_automations")),
            "tenant_has_active_or_scheduled_work")
    require(not office_required or value["office_ready"] is True, "tenant_office_not_ready")


async def transcript(supervisor, name):
    raw = await supervisor._docker("exec", "--user", "10001:10001", name,
                                  "python", "-c", TRANSCRIPT_PROBE, timeout=35)
    require(isinstance(raw, str) and len(raw) <= 1024, "invalid_transcript_probe")
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) == {"message", "part"}
            and all(isinstance(row, dict) and set(row) == {"count", "sha256"}
                    and type(row["count"]) is int and row["count"] >= 0
                    and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                    for row in value.values()), "invalid_transcript_fingerprint")
    return value


async def tenants(public, supervisor, db_state, catalog, *, failed_owners=(), office_required=True):
    result = []
    for user_id, generation, name, container_id, state in db_state["inventory"]:
        require(public.USER_ID.fullmatch(user_id) and type(generation) is int and generation >= 1,
                "invalid_tenant_inventory")
        owner = hashlib.sha256(user_id.encode()).hexdigest()
        volume = "sxy-data-" + owner[:32]
        require(name == "sxy-user-" + owner[:32] and state in {"running", "stopped", "failed"}, "tenant_transition_in_progress")
        volume_info = json.loads(await supervisor._docker("volume", "inspect", volume))[0]
        require((volume_info.get("Labels") or {}).get(public.TENANT_LABEL) == owner, "tenant_volume_owner_changed")
        record = await supervisor.record(user_id)
        require(record is not None and record.generation == generation and record.state == state, "tenant_inventory_changed")
        info = await supervisor._inspect(record)
        running = bool(info and info.get("State", {}).get("Running"))
        if state == "running":
            require(running and info.get("Id") == container_id, "running_tenant_missing")
        elif state == "failed" and owner in failed_owners:
            pass  # Explicit rollback may repair a failed original running tenant.
        else:
            require(not running, "inactive_tenant_still_running")
        tag = None
        if info:
            labels, config, host = info.get("Config", {}).get("Labels") or {}, info.get("Config", {}), info.get("HostConfig", {})
            require(labels.get(public.TENANT_LABEL) == owner and labels.get(public.GENERATION_LABEL) == str(generation),
                    "tenant_labels_changed")
            mounts = info.get("Mounts", [])
            require(len(mounts) == 1 and mounts[0].get("Type") == "volume" and mounts[0].get("Name") == volume
                    and mounts[0].get("Destination") == "/tenant", "tenant_mount_changed")
            require(config.get("User") == "10001:10001" and host.get("ReadonlyRootfs") is True
                    and host.get("Privileged") is False and set(host.get("CapDrop") or []) == {"ALL"}
                    and not host.get("PortBindings"), "tenant_isolation_changed")
            for field in ("CLOUD_ENGINE_ALLOWED_ORIGIN", "SUXIAOYOU_OAUTH_CALLBACK_ORIGIN"):
                require([entry for entry in config.get("Env", []) if entry.startswith(field + "=")] == [field + "=" + ORIGIN],
                        "tenant_origin_changed")
            tag = await bind_container_image(supervisor, info, catalog)
        if running:
            await probe(public, supervisor, name, office_required=office_required)
        result.append({"user_id": user_id, "owner_id": owner, "generation": generation, "state": state,
                       "container_id": info.get("Id") if info else None, "container_running": running,
                       "image_id": info.get("Image") if info else None, "runtime_image": tag,
                       "image_descriptor": catalog[tag]["Id"] if tag else None, "volume_name": volume,
                       "transcript": await transcript(supervisor, name) if running else None})
    return result


async def capture(public, supervisor, database, request, catalog, *, failed_owners=(), office_required=True):
    before = await public.database_snapshot(database)
    inventory = await tenants(public, supervisor, before, catalog, failed_owners=failed_owners, office_required=office_required)
    after = await public.database_snapshot(database)
    public.unchanged(before, after)
    require(before["inventory"] == after["inventory"], "tenant_inventory_changed")
    control = await public.control_identity(supervisor)
    control_info = json.loads(await supervisor._docker("container", "inspect", control))[0]
    require(control_info.get("Image") == request["api_image_id"], "api_image_changed")
    return {"operation_id": OPERATION, "origin": ORIGIN, "api_id": control,
            "api_image_id": request["api_image_id"], "runtime_image": request["runtime_image"],
            "images": request["images"], "billing": before["billing"], "web_auth_sessions": before["web_auth_sessions"],
            "inventory": before["inventory"], "tenants": inventory}


def baseline_valid(request, baseline):
    require(baseline.get("operation_id") == OPERATION and baseline.get("origin") == ORIGIN
            and baseline.get("images") == request["images"] and isinstance(baseline.get("tenants"), list)
            and isinstance(baseline.get("inventory"), list) and baseline.get("runtime_image") in request["images"],
            "baseline_contract_changed")


def unchanged(public, before, after):
    public.unchanged(before, after)
    require(before["images"] == after["images"] and before["origin"] == after["origin"], "maintenance_identity_changed")
    if "tenants" in before and "tenants" in after:
        original = {item["owner_id"]: item.get("transcript") for item in before["tenants"]}
        current = {item["owner_id"]: item.get("transcript") for item in after["tenants"]}
        require(original == current, "original_transcript_changed")


def refresh_inventory(baseline, current, selected, rollback):
    original = {row[0]: row for row in baseline["inventory"]}
    require(set(original) == {row[0] for row in current["inventory"]}, "tenant_set_changed")
    for row in current["inventory"]:
        old = original[row[0]]
        owner = hashlib.sha256(row[0].encode()).hexdigest()
        if owner not in selected:
            require(row == old, "unselected_tenant_changed")
        elif rollback:
            require(row[2] == old[2] and row[4] in {"running", "failed"}
                    and old[1] <= row[1] <= old[1] + 1, "rollback_tenant_state_changed")
        else:
            require(row == old, "forward_tenant_changed")


def pristine_rollback(forward, baseline, current, request):
    """A pre-refresh failure can restore services without restarting tenants."""
    require(forward.get("stage") == "snapshotted" and forward.get("baseline") == baseline and forward.get("applied") == []
            and current["inventory"] == baseline["inventory"]
            and current["tenants"] == baseline["tenants"]
            and current["runtime_image"] == baseline["runtime_image"] == request["runtime_image"],
            "snapshotted_rollback_tenants_changed")


async def operate(public, request, directory_fd, state):
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.office_gateway import OfficeGateway
    from app.cloud.runtime import RuntimeSupervisor

    settings = CloudSettings()
    require(settings.environment == "production" and settings.cookie_secure is True and not settings.allow_mock_provider
            and not settings.auto_migrate and settings.full_runtime_enabled and settings.runtime_network_mode
            and settings.database_url.startswith("postgresql+asyncpg://") and Path(settings.data_dir) == DATA
            and settings.allowed_origin == ORIGIN and settings.runtime_image == request["runtime_image"], "production_settings_changed")
    database = Database(settings)
    try:
        await database.initialize()
        supervisor = RuntimeSupervisor(database, settings)
        catalog = await image_catalog(supervisor, request)
        baseline = request["baseline"]
        if baseline is not None:
            baseline_valid(request, baseline)
        selected = {item["owner_id"] for item in (baseline or {}).get("tenants", []) if item["state"] == "running"}
        current = await capture(public, supervisor, database, request, catalog,
                                failed_owners=selected if request["rollback"] else (),
                                office_required=not (request["mode"] in {"snapshot", "refresh"} and request["rollback"]))
        mode = request["mode"]
        if mode == "snapshot":
            if request["rollback"]:
                unchanged(public, baseline, current)
                refresh_inventory(baseline, current, selected, True)
                return current, 0
            require(all(item["runtime_image"] == request["runtime_image"] for item in current["tenants"] if item["state"] == "running"),
                    "running_runtime_version_changed")
            try:
                previous = read_progress(public, directory_fd, False)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                require(previous.get("stage") == "snapshotted" and previous.get("baseline") == current
                        and previous.get("applied") == [], "snapshot_already_changed")
                return current, 0
            document = {"operation_id": OPERATION, "rollback": False, "stage": "snapshotted", "baseline": current,
                        "applied": [], "created_at_utc": public.utcnow()}
            save_progress(public, directory_fd, document, create=True)
            return current, 0
        unchanged(public, baseline, current)
        if mode == "verify":
            require(current == baseline, "verified_snapshot_changed")
            require(all(item["runtime_image"] == request["runtime_image"] and item["state"] == "running"
                        for item in current["tenants"] if item["owner_id"] in selected), "verified_runtime_version_changed")
            return current, 0

        refresh_inventory(baseline, current, selected, request["rollback"])
        if request["rollback"]:
            forward = read_progress(public, directory_fd, False)
            require(forward.get("baseline") == baseline and forward.get("stage") in {"snapshotted", "applying", "complete", "failed"},
                    "rollback_has_no_forward_attempt")
            require(request["runtime_image"] == baseline["runtime_image"], "rollback_runtime_version_changed")
            if forward["stage"] == "snapshotted":
                # No tenant refresh was attempted. Prove the full original
                # tenant identities are intact and skip their restart entirely.
                pristine_rollback(forward, baseline, current, request)
                document = {"operation_id": OPERATION, "rollback": True, "stage": "complete",
                            "baseline": baseline, "before": current, "after": current, "applied": [],
                            "no_tenant_refresh": True, "completed_at_utc": public.utcnow()}
                save_progress(public, directory_fd, document, create=True)
                return current, 0
            document = {"operation_id": OPERATION, "rollback": True, "stage": "prepared", "baseline": baseline,
                        "before": current, "applied": [], "created_at_utc": public.utcnow()}
            save_progress(public, directory_fd, document, create=True)
        else:
            document = read_progress(public, directory_fd, False)
            require(document.get("stage") == "snapshotted" and document.get("baseline") == baseline
                    and document.get("applied") == [] and request["runtime_image"] != baseline["runtime_image"],
                    "forward_already_attempted")
            document["before"] = current
        require(public.private((DATA / "secrets").lstat(), True), "unsafe_secret_directory")
        for name in ("model-master.key", "office-signing.key"):
            require(public.private((DATA / "secrets" / name).lstat()), "existing_signing_material_required")
        supervisor.office_gateway = OfficeGateway(settings)
        document.update(stage="applying", started_at_utc=public.utcnow())
        save_progress(public, directory_fd, document)
        state["document"] = document
        pre_rows = {row[0]: row for row in current["inventory"]}
        for item in current["tenants"]:
            if item["owner_id"] not in selected:
                continue
            fresh = await public.database_snapshot(database)
            unchanged(public, baseline, dict(fresh, images=request["images"], origin=ORIGIN))
            expected_rows = {row[0]: row for row in document["before"]["inventory"]}
            for completed in document["applied"]:
                expected_rows[completed["user_id"]] = completed["inventory_row"]
            require(fresh["inventory"] == list(expected_rows.values()), "tenant_changed_before_restart")
            record = await supervisor.record(item["user_id"])
            info = await supervisor._inspect(record)
            if info and info.get("State", {}).get("Running"):
                await probe(public, supervisor, record.container_name, office_required=not request["rollback"])
            document.update(current_owner_id=item["owner_id"], current_stage="before_restart")
            save_progress(public, directory_fd, document)
            result = await supervisor.start(item["user_id"], restart=True)
            require(result.get("ready") is True, "refreshed_tenant_not_ready")
            after_db = await public.database_snapshot(database)
            unchanged(public, baseline, dict(after_db, images=request["images"], origin=ORIGIN))
            only = dict(after_db, inventory=[row for row in after_db["inventory"] if row[0] == item["user_id"]])
            refreshed = (await tenants(public, supervisor, only, catalog))[0]
            require(refreshed["transcript"] == item["transcript"], "original_transcript_changed_after_migration")
            require(refreshed["state"] == "running" and refreshed["generation"] == pre_rows[item["user_id"]][1] + 1
                    and refreshed["runtime_image"] == request["runtime_image"] and refreshed["volume_name"] == item["volume_name"],
                    "refreshed_tenant_identity_changed")
            row = only["inventory"][0]
            expected_rows[item["user_id"]] = row
            require(after_db["inventory"] == list(expected_rows.values()), "unselected_tenant_changed")
            document["applied"].append({**refreshed, "inventory_row": row})
            document["current_stage"] = "after_restart"
            save_progress(public, directory_fd, document)
        final = await capture(public, supervisor, database, request, catalog)
        unchanged(public, baseline, final)
        require(len(document["applied"]) == len(selected), "runtime_refresh_incomplete")
        document.update(stage="complete", after=final, completed_at_utc=public.utcnow())
        save_progress(public, directory_fd, document)
        state["document"] = None
        return final, len(selected)
    finally:
        await database.close()


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    state = {"document": None}
    try:
        public, request = public_module(), request_values()
        with operation_lock(public) as directory_fd:
            try:
                snapshot, refreshed = asyncio.run(operate(public, request, directory_fd, state))
            except BaseException as error:
                if state["document"] is not None:
                    document = state["document"]
                    document.update(stage="failed", failure=error.code if isinstance(error, Failure) else "runtime_operation_failed",
                                    failed_at_utc=public.utcnow())
                    try:
                        save_progress(public, directory_fd, document)
                    except BaseException:
                        pass
                raise
        result = {"complete": True, "ready": True, "mode": request["mode"], "operation_id": OPERATION,
                  "snapshot": snapshot, "refreshed_users": refreshed, "rollback": request["rollback"]}
    except BaseException as error:
        result = {"complete": False, "ready": False, "operation_id": OPERATION,
                  "failure": error.code if isinstance(error, Failure) else "runtime_operation_failed"}
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
