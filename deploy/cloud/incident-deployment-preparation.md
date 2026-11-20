# 20261117 incident release preparation

Prepared locally only. No server operation or provider request was performed by
this preparation. The original controllers and releases are unchanged.

`upgrade-incident.py` is a new one-shot controller for `20261117-incident`.
`incident-runtime.py` is the separately pinned API-side maintenance helper.
The production source archive deliberately excludes these operational files.

## Finalize before upload

The controller intentionally refuses every phase before host access until all
pins are finalized:

1. Finish the confirmed incident fix and its regression tests. Freeze a new
   source package at `/tmp/sxy-incident-release.tar.gz` with
   `scripts/cloud-release-package.py`. Compare it with the frozen auto-mode
   archive, whose SHA256 is
   `4d04ceb5d3f2a993b558dc4aff37477b1d7ff286e8a974c800d7e59b3ad60811`.
2. Fill `SOURCE_SHA256` with the new archive digest. Fill
   `EXPECTED_SOURCE_CHANGES` with the exact changed/added production paths and
   the SHA256 of each new file's bytes. No source removals, dependency changes,
   schema changes, Compose changes, or changes outside `backend/app/` and
   `frontend/src/` are accepted. Do not add unrelated workspace edits.
3. Obtain the current server file digest for
   `/srv/suxiaoyou/shared/config/20261116-0235-auto/compose.override.yaml` and
   fill `OLD_OVERRIDE_SHA256`. Do not output the file contents or rendered
   Compose configuration; these can contain credentials.
4. If `incident-runtime.py` changes, update `HELPER_SHA256`. Run the offline
   controller tests and independently review the finalized controller. Record
   its final SHA256 before upload. Do not edit it after `build` starts.
5. Upload only the reviewed new archive, controller, and helper into the private
   staging area. Verify all three hashes, regular-file ownership/modes, and
   place them under `/srv/suxiaoyou/incoming/` as
   `incident-20261117.tar.gz`, `upgrade-incident.py`, and `incident-runtime.py`.

Local validation:

```sh
python3 -m unittest deploy/cloud/test_upgrade_incident.py -v
```

## Fixed baseline and phase behavior

The previous source/current release is `20261116-0235-auto`. Its API, worker and
tenant runtime are deliberately pinned to `20261116-0205-taskfix`; its web image
is pinned to `20261116-0235-auto`. The controller contains the exact image IDs
from the previous deployment acceptance. It obtains the original offline
Node/cache provenance from the immutable `20261115-2144/build-recipe.json`.

`build` verifies the current symlink, serving image identities, fixed private
environment, origin/TLS/proxy metadata, source whitelist and disk space. It
stages a new release/config and creates new runtime, API and web images, offline,
with fixed local bases. No serving container changes. API/runtime builds add
only the reviewed application tree; the web uses the previously verified
offline recipe. Rendered Compose comparison permits only those image tags,
recipe paths, source contexts and the API's runtime-image reference to differ.

`activate` first checks idle tenant engines, no enabled automation, no active
cloud jobs, fully settled/released model ledgers and zero reserved quota. It
closes proxy ingress, then repeats the full snapshot and requires equality.
It writes private fsynced PostgreSQL and online SQLite backups, recreates
API/worker/web, and refreshes only the original running tenants. The helper
retains each user's volume and verifies ownership, isolation, origin, image,
generation and readiness. Billing, quota, sessions and the ledger digest must
remain identical at every stage. It reopens ingress only after internal checks,
then verifies HTTPS and switches `current` atomically.

Every phase must be invoked as the existing dedicated `suxiaoyou` UID/GID 1001
with the existing rootless Docker socket. Commands are operator actions, not
instructions to run while this document remains in preparation:

```sh
python3 /srv/suxiaoyou/incoming/upgrade-incident.py build --source-sha256 FINAL_ARCHIVE_SHA256
python3 /srv/suxiaoyou/incoming/upgrade-incident.py activate --source-sha256 FINAL_ARCHIVE_SHA256
```

## Failure and rollback

There is no automatic rollback, migration, seed, model invocation, task replay,
reservation release, or database restore. A failure persists `failed_phase` in
the new private config's `upgrade-state.json`; private logs are under the new
version's log path. After ingress closure a failure can leave the entry closed
until an operator reviews state and runs explicit rollback. Inspect the durable
phase before taking further action; do not rerun `build` or old controllers.

Rollback retains the current databases and original volumes, restores the
original mixed image versions and auto-mode source symlink, and refreshes the
original running tenants after a refresh attempt. If the forward operation
only reached `snapshotted`, rollback instead requires `applied=[]` and exact
original tenant inventory/container/image/volume identities, then restores the
old services without restarting any tenant. It requires the unchanged idle billing/session
baseline. If new work has changed the baseline, review is required and this
one-shot rollback refuses to proceed. It never restores stale backups.

```sh
python3 /srv/suxiaoyou/incoming/upgrade-incident.py rollback --source-sha256 FINAL_ARCHIVE_SHA256
```

The existing helper conservatively rejects unknown or otherwise unsettled
requests and nonzero reservations. The new incident collector reports that all
73 current requests are settled and reserved quota is zero, so no relaxation
was added. If this changes, preserve the records and investigate rather than
editing the ledger to satisfy a deployment gate.

Controller tests cover fail-closed unfinalized execution before host I/O, exact
source-byte allowlisting, unreviewed/dependency changes, the mixed previous
versions, optional Docker `State.Health`, and rejection of mount/credential
drift in rendered Compose. These tests do not replace the main incident
regressions, real provider acceptance, or post-deployment source/ledger checks.

The existing public domain certificate automation remains a separate pending
item. This release preserves TLS material and performs internal HTTPS checks
using the existing private CA and IP SAN; it does not claim public-domain trust.
