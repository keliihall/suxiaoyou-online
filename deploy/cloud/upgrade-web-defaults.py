#!/usr/bin/env python3
"""Fixed web-only default-mode release; never restart a tenant or control plane.

Build and activate are separate operator commands. Only the web container is
recreated; nginx receives a graceful reload so existing API/SSE connections can
drain. No migration, seed, runtime refresh, model request or database operation
is performed. Failures persist their phase and require explicit review. The
rollback command changes only the web image and never restores databases.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import ssl
import stat
import subprocess
from types import ModuleType
import urllib.request

APP = Path('/srv/suxiaoyou')
SHARED = APP / 'shared'
OLD_VERSION = '20261116-0205-taskfix'
VERSION = '20261116-0235-auto'
OLD_RELEASE = APP / 'releases' / OLD_VERSION
RELEASE = APP / 'releases' / VERSION
OLD_CONFIG = SHARED / 'config' / OLD_VERSION
CONFIG = SHARED / 'config' / VERSION
SOURCE = APP / 'incoming/auto-default-20261116.tar.gz'
SOURCE_SHA256 = '4d04ceb5d3f2a993b558dc4aff37477b1d7ff286e8a974c800d7e59b3ad60811'
OLD_SOURCE = APP / 'incoming/taskfix-20261116.tar.gz'
OLD_SOURCE_SHA256 = '1d6d93d9ebbb47b09f4b22b19cc422f0c1133eae6e889f3772cd611480fa67b6'
MASTER = APP / 'incoming/deploy-release-pinned.py'
MASTER_SHA256 = '4fd0363428249a2de992a884f48f703d4a59ddd1e83b1ab2e84a62df67f18ad8'
ENV = SHARED / '.env.cloud'
STATE = CONFIG / 'upgrade-state.json'
LOG = SHARED / 'logs' / (VERSION + '.log')
PROJECT = 'suxiaoyou-cloud'
SOCKET = '/run/user/1001/docker.sock'
ORIGIN = 'https://192.168.1.211:8443'
WEB_RECIPE_SHA256 = '24e7e322c003137dd33bf8f846a8cb2c83cd279f003e9aa9ad18ac2b38629316'
OLD_OVERRIDE_SHA256 = 'e961450a741b597fdc9589f2a9eb9664d2ed99bacd74d84ce639d1881efe8b3b'
OLD_WEB = 'suxiaoyou-cloud-web:' + OLD_VERSION
WEB = 'suxiaoyou-cloud-web:' + VERSION
API = 'suxiaoyou-cloud-api:' + OLD_VERSION
RUNTIME = 'suxiaoyou-online-runtime:' + OLD_VERSION
CACHE = 'suxiaoyou-npm-cache-complete:20261115-2144'
NODE = 'suxiaoyou-base-node:20261115-amd64'
BASE_IDS = {
    OLD_WEB: 'sha256:24c18ac4cea51f0217d2d09a171206b004a187c90d1b866e6324a882d34af08c',
    API: 'sha256:0888fb196447d3d8aba1b14f54db1c565093695bf20351a1abe0bab886a96675',
    RUNTIME: 'sha256:cbcbdf5447d089da88fd9e945d8f103842d96463747d0f5c5b63e26e817fadbf',
    CACHE: 'sha256:51fe5c916aafefc26dfce5508b14ae943b866f9eb4d6d3c22386d88a26870e67',
    NODE: 'sha256:f9c30b69fe318cb92754ebf62d853c24bf77a702611fa3ae664fbd4144af8722',
}
SHA = re.compile(r'[0-9a-f]{64}\Z')
SERVICES = ('db', 'api', 'worker', 'web', 'proxy')


class Failure(Exception):
    def __init__(self, code='review_required'):
        self.code = code
        super().__init__(code)


def require(condition, code='precondition_failed'):
    if not condition:
        raise Failure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, 'invalid_json_number'))


def read(path, maximum=1024 * 1024, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001 and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and 0 <= info.st_size <= maximum
                and (not private or stat.S_IMODE(info.st_mode) == 0o600), 'unsafe_input_file')
        raw = handle.read(maximum + 1)
    require(len(raw) == info.st_size, 'input_file_changed')
    return raw


def directory(path, *, strict=True):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 1001
            and not stat.S_IMODE(info.st_mode) & 0o022
            and (not strict or stat.S_IMODE(info.st_mode) == 0o700), 'unsafe_directory')


def write(path, raw, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def atomic(path, raw):
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    try:
        write(temporary, raw)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def save(state, phase=None):
    if phase is not None:
        state['phase'] = phase
    state['updated_at_utc'] = now()
    atomic(STATE, (json.dumps(state, indent=2, sort_keys=True) + '\n').encode())


def environment():
    account = pwd.getpwnam('suxiaoyou')
    require(os.getuid() == os.geteuid() == account.pw_uid == 1001
            and os.getgid() == account.pw_gid == 1001, 'dedicated_deployment_user_required')
    result = {key: value for key, value in os.environ.items() if not key.startswith(
        ('COMPOSE_', 'SUXIAOYOU_', 'NEXT_PUBLIC_', 'DOCKER_')) and key not in {
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'}}
    result.update(HOME=account.pw_dir,
                  PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                  DOCKER_HOST='unix://' + SOCKET, XDG_RUNTIME_DIR='/run/user/1001',
                  DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1001/bus',
                  NO_PROXY='127.0.0.1,localhost,192.168.1.211')
    return result


def command(arguments, *, content=None, capture=True, timeout=180):
    # Inspect/config output may contain process-only secrets; capture it only
    # in memory. Build diagnostics stay in the private deployment log.
    with LOG.open('ab') as log:
        result = subprocess.run(arguments, cwd=APP, env=environment(), input=content,
                                stdin=subprocess.DEVNULL if content is None else None,
                                stdout=subprocess.PIPE if capture else log,
                                stderr=log, timeout=timeout, check=False)
    require(result.returncode == 0, 'operation_failed_review_private_log')
    return result.stdout if capture else b''


def compose(*arguments, old=False):
    release, config = (OLD_RELEASE, OLD_CONFIG) if old else (RELEASE, CONFIG)
    return ['docker', 'compose', '--project-name', PROJECT, '--env-file', str(ENV),
            '-f', str(release / 'compose.cloud.yaml'),
            '-f', str(config / 'compose.override.yaml'), *arguments]


def pinned_master():
    raw = read(MASTER, 128 * 1024)
    require(digest(raw) == MASTER_SHA256, 'master_identity_mismatch')
    module = ModuleType('suxiaoyou_web_package_validator')
    module.__file__ = str(MASTER)
    exec(compile(raw, str(MASTER), 'exec'), module.__dict__)
    require(module.APP == APP and module.VERSION == '20261115-2144'
            and module.DEPLOY_UID == 1001 and module.PROJECT == PROJECT)
    return module


def fixed_inputs():
    raw = read(ENV, 16384, private=True)
    values = {}
    for line in raw.decode().splitlines():
        require('=' in line)
        key, value = line.split('=', 1)
        require(key not in values)
        values[key] = value
    require(values.get('SUXIAOYOU_CLOUD_ENVIRONMENT') == 'production'
            and values.get('SUXIAOYOU_CLOUD_COOKIE_SECURE') == 'true'
            and values.get('SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER') == 'false'
            and values.get('SUXIAOYOU_CLOUD_ALLOWED_ORIGIN') == 'https://nav.cyneu.com:18443'
            and values.get('CLOUD_DOCKER_SOCKET') == SOCKET and values.get('CLOUD_DOCKER_GID') == '983')
    nginx = Path(values.get('CLOUD_NGINX_CONFIG', ''))
    require(nginx == SHARED / 'config/20261115-2144-public/nginx.conf')
    directory(nginx.parent)
    nginx_raw = read(nginx, 65536)
    # A graceful reload and the existing dynamic DNS avoid proxy recreation.
    require(b'resolver 127.0.0.11' in nginx_raw and b'set $cloud_web http://web:3000;' in nginx_raw)
    key = (SHARED / 'tls/key.pem').lstat()
    require(stat.S_ISREG(key.st_mode) and key.st_uid == 1001 and key.st_nlink == 1
            and stat.S_IMODE(key.st_mode) == 0o444)
    override = read(OLD_CONFIG / 'compose.override.yaml', 65536, private=True)
    require(digest(override) == OLD_OVERRIDE_SHA256, 'old_override_changed')
    return {'environment_sha256': digest(raw), 'nginx_path': str(nginx),
            'nginx_sha256': digest(nginx_raw), 'old_override_sha256': digest(override),
            'certificate_sha256': digest(read(SHARED / 'tls/cert.pem', 65536)),
            'key_identity': [key.st_dev, key.st_ino, key.st_size, key.st_mtime_ns]}


def image(tag):
    result = decode(command(['docker', 'image', 'inspect', tag]))[0]
    require(result.get('Architecture') == 'amd64' and result.get('Os') == 'linux'
            and tag in result.get('RepoTags', [])
            and re.fullmatch(r'sha256:[0-9a-f]{64}', result.get('Id', '')))
    return result


def check_images():
    for tag, expected in BASE_IDS.items():
        require(image(tag)['Id'] == expected, 'pinned_image_changed')


def container(name):
    template = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
                '"image":{{json .Config.Image}},"state":{{json .State}},'
                '"mounts":{{json .Mounts}}}')
    result = decode(command(['docker', 'container', 'inspect', '--format', template, name]))
    require(SHA.fullmatch(result.get('id', '')))
    state = result.pop('state')
    result.update(started_at=state.get('StartedAt'), running=state.get('Running'),
                  oom_killed=state.get('OOMKilled'), health=state.get('Health', {}).get('Status'))
    return result


def services():
    return {name: container(PROJECT + '-' + name + '-1') for name in SERVICES}


def health(rows):
    for name, row in rows.items():
        require(row['running'] is True and row['oom_killed'] is False, 'service_not_running')
        if name in {'db', 'api', 'web'}:
            require(row['health'] == 'healthy', 'service_not_healthy')
    for name in ('api', 'worker'):
        require(rows[name]['image'] == API and rows[name]['image_id'] == BASE_IDS[API],
                'control_plane_image_changed')


def tenant_inventory():
    names = command(['docker', 'ps', '-a', '--filter', 'name=^/sxy-user-',
                     '--format', '{{.Names}}']).decode().splitlines()
    require(len(names) <= 10000 and len(names) == len(set(names)))
    require(all(re.fullmatch(r'sxy-user-[0-9a-f]{32}', name) for name in names))
    return {name: container(name) for name in sorted(names)}


def identity(row):
    # Health and application task activity may change while users work.
    return {key: row[key] for key in ('id', 'name', 'image', 'image_id', 'started_at', 'mounts')}


def preserved(before, after, tenants):
    health(after)
    for name in SERVICES:
        if name != 'web':
            require(identity(before[name]) == identity(after[name]), 'non_web_container_changed')
    current = tenant_inventory()
    for name, row in tenants.items():
        require(name in current and identity(row) == identity(current[name]), 'tenant_container_changed')
        if row['running']:
            require(current[name]['running'], 'running_tenant_stopped')


def verify_source(master, sha):
    manifest, files = master.validate_package(SOURCE, sha)
    old_manifest, old_files = master.validate_package(OLD_SOURCE, OLD_SOURCE_SHA256)
    old = {name: raw for name, raw, _mode in old_files}
    new = {name: raw for name, raw, _mode in files}
    for name, raw, _mode in old_files:
        require(read(OLD_RELEASE / name, max(1, len(raw))) == raw, 'old_release_source_changed')
    require(old.keys() <= new.keys(), 'unexpected_source_file_removal')
    changes = sorted(name for name in new if name != 'RELEASE-MANIFEST.json' and old.get(name) != new[name])
    require(changes and all(name.startswith('frontend/src/') for name in changes), 'non_frontend_source_change')
    require(manifest['source_sha256'] != old_manifest['source_sha256'])
    return manifest, files, changes


def extract(files):
    require(not RELEASE.exists() and not RELEASE.is_symlink(), 'release_already_exists')
    RELEASE.mkdir(mode=0o700)
    for name, raw, mode in files:
        parent = RELEASE
        for part in PurePosixPath(name).parts[:-1]:
            parent /= part
            if not parent.exists():
                parent.mkdir(mode=0o755)
                parent.chmod(0o755)
        write(RELEASE / name, raw, mode)


def checked_release(files):
    for name, raw, _mode in files:
        require(read(RELEASE / name, max(1, len(raw))) == raw, 'staged_source_changed')


def compose_contract():
    before = decode(command(compose('config', '--format', 'json', old=True)))
    after = decode(command(compose('config', '--format', 'json')))
    require(before['services']['web']['image'] == OLD_WEB and after['services']['web']['image'] == WEB)
    require(before.keys() == after.keys() and before['services'].keys() == after['services'].keys())
    # Every service build context follows the first Compose file's source tree;
    # only web's image/recipe changes. No other rendered runtime field may vary.
    for doc in (before, after):
        for item in doc['services'].values():
            if isinstance(item.get('build'), dict):
                item['build']['context'] = '<release-source>'
        doc['services']['web']['image'] = '<web-image>'
        doc['services']['web']['build']['dockerfile'] = '<web-recipe>'
    require(before == after, 'non_web_compose_contract_changed')


def https_ready():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args):
            return None
    context = ssl.create_default_context(cafile=str(SHARED / 'tls/cert.pem'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context), NoRedirect())
    for path in ('/readyz', '/cloud'):
        with opener.open(ORIGIN + path, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
            require(response.status == 200 and response.geturl() == ORIGIN + path and len(raw) <= 1024 * 1024,
                    'https_readiness_failed')
        if path == '/readyz':
            require(decode(raw).get('status') == 'ready' and decode(raw).get('mode') == 'cloud')


def reload_proxy():
    name = PROJECT + '-proxy-1'
    command(['docker', 'exec', name, 'nginx', '-t'], timeout=30)
    command(['docker', 'exec', name, 'nginx', '-s', 'reload'], timeout=30)


def switch_current(release):
    temporary = APP / ('.current-web-auto-' + secrets.token_hex(8))
    os.symlink(release, temporary)
    os.replace(temporary, APP / 'current')
    descriptor = os.open(APP, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build(master, sha):
    require((APP / 'current').is_symlink() and (APP / 'current').resolve() == OLD_RELEASE,
            'old_release_not_current')
    require(not CONFIG.exists() and not CONFIG.is_symlink(), 'version_already_staged')
    fixed = fixed_inputs()
    before = services()
    health(before)
    require(before['web']['image'] == OLD_WEB and before['web']['image_id'] == BASE_IDS[OLD_WEB])
    tenants = tenant_inventory()
    check_images()
    https_ready()
    manifest, files, changes = verify_source(master, sha)
    require(shutil.disk_usage(APP).free >= 8 * 1024 ** 3, 'insufficient_build_disk')
    require(not command(['docker', 'image', 'ls', '--filter', 'reference=' + WEB, '--format', '{{.ID}}']),
            'new_web_tag_already_exists')
    CONFIG.mkdir(mode=0o700)
    state = {'format_version': 1, 'version': VERSION, 'old_version': OLD_VERSION,
             'phase': 'preparing', 'created_at_utc': now(), 'source_archive_sha256': sha,
             'source_sha256': manifest['source_sha256'], 'source_changes': changes,
             'fixed_inputs': fixed, 'original_services': before, 'original_tenants': tenants,
             'controller_sha256': digest(read(Path(__file__), 128 * 1024)),
             'schema_changes': False, 'dependencies_changed': False, 'credentials_changed': False,
             'model_requests': 0, 'runtime_restarts': 0, 'automatic_rollback': False}
    save(state)
    try:
        extract(files)
        build_dir = CONFIG / 'build'
        build_dir.mkdir(mode=0o700)
        recipe = read(OLD_CONFIG / 'build/Dockerfile.web', 65536)
        require(digest(recipe) == WEB_RECIPE_SHA256 and b'--offline' in recipe, 'web_recipe_changed')
        write(build_dir / 'Dockerfile.web', recipe)
        write(build_dir / 'Dockerfile.web.dockerignore', read(OLD_CONFIG / 'build/Dockerfile.web.dockerignore', 65536))
        override = read(OLD_CONFIG / 'compose.override.yaml', 65536, private=True).decode()
        require(override.count(OLD_WEB) == 1)
        override = override.replace(OLD_WEB, WEB)
        old_recipe = str(OLD_CONFIG / 'build/Dockerfile.web')
        require(override.count(old_recipe) == 1)
        override = override.replace(old_recipe, str(build_dir / 'Dockerfile.web'))
        write(CONFIG / 'compose.override.yaml', override.encode())
        compose_contract()
        save(state, 'building_web')
        check_images()
        command(['docker', 'build', '--platform', 'linux/amd64', '--network', 'none', '--pull=false',
                 '--file', str(build_dir / 'Dockerfile.web'), '--tag', WEB,
                 '--build-arg', 'NEXT_PUBLIC_API_URL=http://api:8000', str(RELEASE)], capture=False, timeout=1800)
        state['built_web_image'] = image(WEB)['Id']
        check_images()
        require(fixed_inputs() == fixed, 'fixed_inputs_changed')
        preserved(before, services(), tenants)
        checked_release(files)
        save(state, 'built')
        return state
    except BaseException:
        state['failed_phase'] = state['phase']
        save(state, 'failed')
        raise


def activate(master, sha, *, rollback=False):
    state = decode(read(STATE, private=True))
    require(state.get('version') == VERSION and state.get('source_archive_sha256') == sha
            and state.get('controller_sha256') == digest(read(Path(__file__), 128 * 1024)))
    require(state['phase'] in ({'active', 'failed'} if rollback else {'built'}), 'phase_requires_manual_review')
    require((APP / 'current').is_symlink() and (APP / 'current').resolve()
            in ({OLD_RELEASE, RELEASE} if rollback else {OLD_RELEASE}), 'current_release_changed')
    require(fixed_inputs() == state['fixed_inputs'], 'fixed_inputs_changed')
    _manifest, files, _changes = verify_source(master, sha)
    checked_release(files)
    check_images()
    require(image(WEB)['Id'] == state['built_web_image'], 'built_web_identity_changed')
    compose_contract()
    before = services()
    health(before)
    tenants = tenant_inventory()
    if not rollback:
        require(before['web']['image'] == OLD_WEB and before['web']['image_id'] == BASE_IDS[OLD_WEB])
    try:
        state['activation_services'] = before
        state['activation_tenants'] = tenants
        save(state, 'rollback_web' if rollback else 'switching_web')
        command(compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
                        '--wait', '--wait-timeout', '180', 'web', old=rollback), timeout=240)
        after = services()
        require(after['web']['image'] == (OLD_WEB if rollback else WEB)
                and after['web']['image_id'] == (BASE_IDS[OLD_WEB] if rollback else state['built_web_image']))
        preserved(before, after, tenants)
        save(state, 'rollback_graceful_proxy_reload' if rollback else 'graceful_proxy_reload')
        reload_proxy()
        https_ready()
        after = services()
        preserved(before, after, tenants)
        require(fixed_inputs() == state['fixed_inputs'], 'fixed_inputs_changed')
        switch_current(OLD_RELEASE if rollback else RELEASE)
        state['completed_at_utc'] = now()
        state['non_web_containers_preserved'] = True
        state['tenant_containers_preserved'] = True
        state['services_after'] = after
        save(state, 'rolled_back' if rollback else 'active')
        atomic(CONFIG / 'deployment-result.json', (json.dumps(state, indent=2, sort_keys=True) + '\n').encode())
        return state
    except BaseException:
        state['failed_phase'] = state['phase']
        save(state, 'failed')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('build', 'activate', 'rollback'))
    parser.add_argument('--source-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    lock = None
    try:
        require(SHA.fullmatch(args.source_sha256) and args.source_sha256 == SOURCE_SHA256,
                'source_archive_pin_mismatch')
        environment()
        for path in (APP, APP / 'incoming', APP / 'releases'):
            directory(path, strict=False)
        for path in (SHARED, SHARED / 'config', SHARED / 'logs', SHARED / 'tls', OLD_CONFIG, OLD_RELEASE):
            directory(path)
        lock = os.open(SHARED / 'deploy.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 1001
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'unsafe_deploy_lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not LOG.exists():
            write(LOG, b'Web-only automatic execution mode release log.\n')
        read(LOG, 256 * 1024 * 1024, private=True)
        master = pinned_master()
        result = build(master, args.source_sha256) if args.phase == 'build' else activate(
            master, args.source_sha256, rollback=args.phase == 'rollback')
        print(json.dumps({'complete': True, 'version': VERSION, 'phase': result['phase'],
                          'operator_review_required': False}))
        return 0
    except BaseException as error:
        print(json.dumps({'complete': False, 'version': VERSION, 'phase': args.phase,
                          'failure': error.code if isinstance(error, Failure) else 'operation_failed',
                          'operator_review_required': True, 'automatic_rollback': False}))
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
