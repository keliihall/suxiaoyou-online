#!/usr/bin/env bash
# Prepare a fresh Ubuntu host for Su Xiaoyou's dedicated rootless Docker daemon.
# Run this reviewed file as root. It never deploys source, creates app accounts,
# changes firewall/TLS settings, upgrades the OS, or prints application secrets.
# Docker references: https://docs.docker.com/engine/install/ubuntu/
# https://docs.docker.com/engine/security/rootless/{,tips/,troubleshoot/}
set -Eeuo pipefail
umask 077
export LC_ALL=C
export DEBIAN_FRONTEND=noninteractive

readonly DEPLOY_USER=suxiaoyou
readonly DEPLOY_HOME=/home/suxiaoyou
readonly APP_DIR=/srv/suxiaoyou
readonly STATE_DIR=/var/lib/suxiaoyou-host-bootstrap
readonly STATE_FILE="$STATE_DIR/managed-v1"
readonly DOCKER_KEY=/etc/apt/keyrings/docker.asc
readonly DOCKER_SOURCE=/etc/apt/sources.list.d/docker.sources
readonly DOCKER_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
TASK_TMP_DIR=

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() { if [[ -n "$TASK_TMP_DIR" ]]; then rm -rf -- "$TASK_TMP_DIR"; fi; }
trap cleanup EXIT
trap 'printf "ERROR: host preparation stopped at line %s; no app was deployed.\n" "$LINENO" >&2' ERR

[[ $EUID -eq 0 ]] || die 'Run as root (sudo bash deploy/cloud/bootstrap-host.sh).'
[[ $# -eq 0 ]] || die 'This script takes no arguments.'
[[ -f /etc/os-release ]] || die 'Missing /etc/os-release.'
# The distribution-owned file is the only sourced input.
# shellcheck disable=SC1091
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || die 'Only a fresh Ubuntu host is supported.'
case ${VERSION_ID:-} in 22.04|24.04|26.04|26.10) ;; *) die 'Unsupported Ubuntu release; review Docker support first.' ;; esac
readonly UBUNTU_SUITE="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
[[ $UBUNTU_SUITE =~ ^[a-z]+$ ]] || die 'Invalid Ubuntu repository suite.'
readonly HOST_ARCH="$(dpkg --print-architecture)"
case $HOST_ARCH in amd64|arm64) ;; *) die 'Only amd64/arm64 are supported by this host bootstrap.' ;; esac
[[ -d /run/systemd/system ]] || die 'An active systemd host is required.'
[[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || die 'cgroup v2 is required; no boot configuration will be changed automatically.'
if [[ -r /proc/sys/kernel/unprivileged_userns_clone ]]; then
    [[ $(cat /proc/sys/kernel/unprivileged_userns_clone) == 1 ]] || die 'Unprivileged user namespaces are disabled; review host policy before continuing.'
fi
[[ $(cat /proc/sys/user/max_user_namespaces) -ge 1024 ]] || die 'Insufficient user namespaces; review host policy before continuing.'
command -v flock >/dev/null || die 'The distribution util-linux/flock package is required.'
exec 9>/run/lock/suxiaoyou-host-bootstrap.lock
flock -n 9 || die 'Another Su Xiaoyou host bootstrap is running.'
TASK_TMP_DIR=$(mktemp -d /tmp/suxiaoyou-host-bootstrap.XXXXXXXX)
printf 'suxiaoyou-host-bootstrap-v1\nuser=%s\nhome=%s\napp=%s\n' \
    "$DEPLOY_USER" "$DEPLOY_HOME" "$APP_DIR" > "$TASK_TMP_DIR/marker"

assert_private_state() {
    [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || die 'Unsafe bootstrap state directory.'
    [[ $(stat -c %u "$STATE_DIR") == 0 && $(stat -c %a "$STATE_DIR") == 700 ]] || die 'Bootstrap state directory must be root-owned mode 0700.'
    [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || die 'Incomplete/unrecognized bootstrap state; inspect it before retrying.'
    [[ $(stat -c %u "$STATE_FILE") == 0 && $(stat -c %a "$STATE_FILE") == 600 ]] || die 'Bootstrap marker must be root-owned mode 0600.'
    cmp -s "$TASK_TMP_DIR/marker" "$STATE_FILE" || die 'Bootstrap marker does not match this deployment.'
}

if [[ -e "$STATE_DIR" || -L "$STATE_DIR" ]]; then
    assert_private_state
else
    # Do not remove, stop, replace, or silently adopt anybody else's Docker.
    for binary in docker dockerd containerd runc; do
        if command -v "$binary" >/dev/null; then die "Existing $binary installation; automatic takeover is refused."; fi
    done
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker \
        docker-ce docker-ce-cli docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin containerd containerd.io runc; do
        if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
            die "Existing $package package; automatic replacement is refused."
        fi
    done
    if pgrep -x dockerd >/dev/null || pgrep -x containerd >/dev/null; then die 'An existing container daemon is running.'; fi
    for path in /var/run/docker.sock /run/containerd/containerd.sock /var/lib/docker /var/lib/containerd \
        /etc/docker "$DOCKER_KEY" "$DOCKER_SOURCE" "$DEPLOY_HOME" "$APP_DIR"; do
        [[ ! -e "$path" && ! -L "$path" ]] || die "Existing $path; automatic takeover is refused."
    done
    if getent passwd "$DEPLOY_USER" >/dev/null || getent group "$DEPLOY_USER" >/dev/null; then
        die 'The deployment user/group already exists without our managed marker.'
    fi
    # Refuse alternate existing Docker repositories or service customization.
    docker_repo_matches=$(grep -RIlE 'download\.docker\.com|dockerproject\.org' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true)
    if [[ -n "$docker_repo_matches" ]]; then
        die 'An existing Docker APT repository needs manual review.'
    fi
    for unit in docker.service docker.socket containerd.service; do
        [[ $(systemctl show "$unit" --property=LoadState --value) == not-found ]] || die "Existing $unit configuration needs manual review."
    done
    install -d -m 0700 "$STATE_DIR"
    install -m 0600 "$TASK_TMP_DIR/marker" "$STATE_FILE"
fi

# Publish only new managed configuration. Matching files are kept; changed files
# and symlinks are rejected rather than overwritten on a retry.
publish_config() {
    local candidate=$1 destination=$2 mode=$3
    [[ ! -L "$destination" ]] || die "Refusing configuration symlink: $destination"
    if [[ -e "$destination" ]]; then
        [[ -f "$destination" && $(stat -c %u "$destination") == 0 ]] || die "Unsafe configuration: $destination"
        cmp -s "$candidate" "$destination" || die "Existing configuration differs: $destination"
        [[ $(stat -c %a "$destination") == "$mode" ]] || die "Unexpected configuration mode: $destination"
    else
        install -m "$mode" "$candidate" "$destination"
    fi
}

# These units belong only to a fresh installation marked above. Mask before APT
# so package post-install hooks cannot leave an unnecessary rootful daemon up.
for unit in docker.service docker.socket containerd.service; do
    unit_path="/etc/systemd/system/$unit"
    if [[ -e "$unit_path" || -L "$unit_path" ]]; then
        [[ -L "$unit_path" && $(readlink "$unit_path") == /dev/null ]] || die "Existing rootful unit override: $unit_path"
    fi
done
systemctl mask docker.service docker.socket containerd.service
systemctl daemon-reload
if systemctl is-active --quiet docker.service || systemctl is-active --quiet docker.socket || systemctl is-active --quiet containerd.service; then
    die 'A rootful container daemon is active; refusing to stop an unexpected workload.'
fi

apt-get update
apt-get install -y --no-install-recommends --no-upgrade -o Dpkg::Options::=--force-confold \
    ca-certificates curl gnupg python3 uidmap slirp4netns fuse-overlayfs dbus-user-session apparmor
[[ ! -L /etc/apt/keyrings ]] || die 'Refusing a symlinked APT keyring directory.'
if [[ ! -e /etc/apt/keyrings ]]; then install -d -m 0755 /etc/apt/keyrings; fi
[[ -d /etc/apt/keyrings && $(stat -c %u /etc/apt/keyrings) == 0 ]] || die 'APT keyring directory is unsafe.'
[[ $(( 8#$(stat -c %a /etc/apt/keyrings) & 0022 )) -eq 0 ]] || die 'APT keyring directory is externally writable.'
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 90 --max-filesize 1048576 \
    https://download.docker.com/linux/ubuntu/gpg -o "$TASK_TMP_DIR/docker.asc"
install -d -m 0700 "$TASK_TMP_DIR/gnupg"
key_fingerprint=$(gpg --batch --no-options --homedir "$TASK_TMP_DIR/gnupg" --with-colons --show-keys "$TASK_TMP_DIR/docker.asc" 2>/dev/null | awk -F: '$1 == "fpr" {print $10; exit}')
[[ $key_fingerprint == "$DOCKER_FINGERPRINT" ]] || die 'Docker signing key fingerprint changed; review it before trusting a new key.'
publish_config "$TASK_TMP_DIR/docker.asc" "$DOCKER_KEY" 644
cat > "$TASK_TMP_DIR/docker.sources" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $UBUNTU_SUITE
Components: stable
Architectures: $HOST_ARCH
Signed-By: $DOCKER_KEY
EOF
publish_config "$TASK_TMP_DIR/docker.sources" "$DOCKER_SOURCE" 644
apt-get update
apt-get install -y --no-install-recommends --no-upgrade -o Dpkg::Options::=--force-confold \
    docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin
for unit in docker.service docker.socket containerd.service; do
    ! systemctl is-active --quiet "$unit" || die "Unexpected active rootful service: $unit"
    [[ $(systemctl is-enabled "$unit" 2>/dev/null || true) == masked ]] || die "Rootful service is not masked: $unit"
done

if ! getent passwd "$DEPLOY_USER" >/dev/null; then
    [[ ! -e "$DEPLOY_HOME" && ! -L "$DEPLOY_HOME" ]] || die 'Deployment home exists without its user.'
    useradd --create-home --home-dir "$DEPLOY_HOME" --shell /bin/bash --user-group "$DEPLOY_USER"
    passwd --lock "$DEPLOY_USER" >/dev/null
    chmod 0700 "$DEPLOY_HOME"
fi
readonly DEPLOY_UID="$(id -u "$DEPLOY_USER")"
readonly DEPLOY_GID="$(id -g "$DEPLOY_USER")"
[[ $DEPLOY_UID -ge 1000 && $DEPLOY_UID -ne 65534 ]] || die 'Deployment account must have a dedicated ordinary UID.'
[[ $(getent passwd "$DEPLOY_USER" | cut -d: -f6) == "$DEPLOY_HOME" ]] || die 'Unexpected deployment home; no account settings changed.'
[[ $(getent passwd "$DEPLOY_USER" | cut -d: -f7) == /bin/bash ]] || die 'Unexpected deployment shell; no account settings changed.'
[[ $(passwd -S "$DEPLOY_USER" | awk '{print $2}') == L ]] || die 'Deployment password is unlocked; inspect the account before continuing.'
[[ -d "$DEPLOY_HOME" && ! -L "$DEPLOY_HOME" && $(stat -c %u "$DEPLOY_HOME") == "$DEPLOY_UID" ]] || die 'Deployment home is unsafe.'
if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -Eq '^(sudo|docker|adm|root)$'; then die 'Deployment account has unexpected privileged groups.'; fi

# Ordinary useradd normally allocates both ranges. If one is absent after a
# partial install, allocate a new non-overlapping range without rewriting any
# existing subordinate-ID entries. A short existing range requires review.
for map_kind in subuid subgid; do
    range_start=$(python3 - "$DEPLOY_USER" "/etc/$map_kind" <<'PY'
import pathlib, sys
user, filename = sys.argv[1:]
ranges = []
own = []
for line in pathlib.Path(filename).read_text().splitlines():
    if not line or line.startswith('#'):
        continue
    name, start, count = line.split(':')
    start, count = int(start), int(count)
    if start < 0 or count < 1:
        raise SystemExit('Invalid subordinate-ID map; manual review required')
    ranges.append((start, start + count))
    if name == user:
        own.append((start, count))
if any(count >= 65536 for _, count in own):
    print('existing')
elif own:
    raise SystemExit('Existing subordinate-ID allocation is too small; manual review required')
else:
    candidate = 100000
    for start, end in sorted(ranges):
        if candidate + 65536 <= start:
            break
        if candidate < end:
            candidate = end
    if candidate + 65536 > 2**32 - 2:
        raise SystemExit('No safe subordinate-ID allocation remains')
    print(candidate)
PY
    )
    if [[ $range_start != existing ]]; then
        [[ $range_start =~ ^[0-9]+$ ]] || die 'Invalid subordinate-ID allocation result.'
        if [[ $map_kind == subuid ]]; then
            usermod --add-subuids "$range_start-$((range_start + 65535))" "$DEPLOY_USER"
        else
            usermod --add-subgids "$range_start-$((range_start + 65535))" "$DEPLOY_USER"
        fi
    fi
done

if [[ ! -e "$APP_DIR" && ! -L "$APP_DIR" ]]; then install -d -o "$DEPLOY_UID" -g "$DEPLOY_GID" -m 0750 "$APP_DIR"; fi
[[ -d "$APP_DIR" && ! -L "$APP_DIR" && $(stat -c %u "$APP_DIR") == "$DEPLOY_UID" ]] || die 'Application directory has unexpected ownership/type.'
[[ $(( 8#$(stat -c %a "$APP_DIR") & 0022 )) -eq 0 ]] || die 'Application directory must not be group/world writable.'

readonly USER_UNIT="user@$DEPLOY_UID.service"
readonly DELEGATE_DIR="/etc/systemd/system/$USER_UNIT.d"
readonly DELEGATE_FILE="$DELEGATE_DIR/50-suxiaoyou-delegate.conf"
cat > "$TASK_TMP_DIR/delegate.conf" <<'EOF'
# Managed by deploy/cloud/bootstrap-host.sh; applies only to suxiaoyou.
[Service]
Delegate=cpu cpuset io memory pids
EOF
if [[ ! -e "$DELEGATE_FILE" && ! -L "$DELEGATE_FILE" ]] && systemctl is-active --quiet "$USER_UNIT"; then
    die 'User manager already runs without our cgroup delegation; stop its workloads and review rather than restarting it automatically.'
fi
[[ ! -L "$DELEGATE_DIR" ]] || die 'Refusing a symlinked user-manager drop-in directory.'
if [[ ! -e "$DELEGATE_DIR" ]]; then install -d -m 0755 "$DELEGATE_DIR"; fi
[[ -d "$DELEGATE_DIR" && $(stat -c %u "$DELEGATE_DIR") == 0 ]] || die 'User-manager drop-in directory is unsafe.'
[[ $(( 8#$(stat -c %a "$DELEGATE_DIR") & 0022 )) -eq 0 ]] || die 'User-manager drop-in directory is externally writable.'
publish_config "$TASK_TMP_DIR/delegate.conf" "$DELEGATE_FILE" 644
systemctl daemon-reload
loginctl enable-linger "$DEPLOY_USER"
systemctl start "$USER_UNIT"
readonly USER_RUNTIME="/run/user/$DEPLOY_UID"
readonly ROOTLESS_SOCKET="$USER_RUNTIME/docker.sock"
[[ -d "$USER_RUNTIME" && ! -L "$USER_RUNTIME" && $(stat -c %u "$USER_RUNTIME") == "$DEPLOY_UID" ]] || die 'User runtime directory is unavailable or unsafe.'
as_deploy_user() {
    runuser -u "$DEPLOY_USER" -- env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
        -u XDG_CONFIG_HOME -u XDG_DATA_HOME -u XDG_CACHE_HOME XDG_RUNTIME_DIR="$USER_RUNTIME" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$USER_RUNTIME/bus" "$@"
}
as_deploy_user systemctl --user start dbus.socket
for controller in cpu memory pids; do
    grep -qw "$controller" "/sys/fs/cgroup/user.slice/user-$DEPLOY_UID.slice/$USER_UNIT/cgroup.controllers" || die "Missing delegated $controller controller."
done
if [[ ! -f "$DEPLOY_HOME/.config/systemd/user/docker.service" ]]; then
    # Fresh rootless hosts may not have loaded the nftables kernel module yet.
    # Load the required module without adding or changing firewall rules.
    modprobe nf_tables
    as_deploy_user /usr/bin/dockerd-rootless-setuptool.sh install
fi
as_deploy_user systemctl --user enable --now docker.service
for attempt in $(seq 1 30); do
    if as_deploy_user docker --host "unix://$ROOTLESS_SOCKET" info --format '{{json .}}' > "$TASK_TMP_DIR/docker-info.json" 2>/dev/null; then break; fi
    sleep 1
done
python3 - "$TASK_TMP_DIR/docker-info.json" <<'PY'
import json, pathlib, sys
info = json.loads(pathlib.Path(sys.argv[1]).read_text())
if not any('rootless' in item for item in info.get('SecurityOptions', [])):
    raise SystemExit('The selected daemon is not rootless')
if str(info.get('CgroupVersion')) != '2' or info.get('CgroupDriver') != 'systemd':
    raise SystemExit('Rootless Docker must use systemd and cgroup v2')
for feature in ('MemoryLimit', 'PidsLimit', 'CpuCfsQuota', 'CpuCfsPeriod'):
    if info.get(feature) is not True:
        raise SystemExit('Rootless Docker resource limits are not available: ' + feature)
PY
[[ -S "$ROOTLESS_SOCKET" && $(stat -c %u "$ROOTLESS_SOCKET") == "$DEPLOY_UID" ]] || die 'Rootless socket ownership/type mismatch.'
as_deploy_user docker compose version
as_deploy_user docker buildx version
printf 'SUXIAOYOU_HOST_BOOTSTRAP=ready\nDEPLOY_USER=%s\nDEPLOY_UID=%s\nDEPLOY_GID=%s\nAPP_DIR=%s\nCLOUD_DOCKER_SOCKET=%s\nDOCKER_HOST=unix://%s\nSOCKET_HOST_GID=%s\n' \
    "$DEPLOY_USER" "$DEPLOY_UID" "$DEPLOY_GID" "$APP_DIR" "$ROOTLESS_SOCKET" "$ROOTLESS_SOCKET" "$(stat -c %g "$ROOTLESS_SOCKET")"
printf 'ROOTLESS=true\nCGROUP_VERSION=2\nCGROUP_DRIVER=systemd\n'
printf 'Next: deploy reviewed source as %s; probe socket access from API UID 10001 to select CLOUD_DOCKER_GID.\n' "$DEPLOY_USER"
