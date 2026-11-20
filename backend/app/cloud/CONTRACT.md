# Cloud implementation contract

This is a separate cloud application (`app.cloud.main:create_app`), with its own
SQLAlchemy `CloudBase` and `cloud_*` tables. The complete original desktop
router/lifespan runs in one private container per user (`runtime_entry`), behind
the authenticated control-plane proxy; it is never mounted inside the shared
API process. Original sessions/Goals/automations live in each engine database.
The older cloud Jobs/Worker remain as a historical compatibility surface. API prefix: `/api/cloud`. Time values are UTC datetimes, IDs are
UUID hex strings. Each user owns a private tenant (user_id is the tenant boundary).

## Shared modules

- `config.CloudSettings`: `database_url`, `data_dir: Path`, `allowed_origin`,
  `cookie_secure`, `session_hours`, `auto_migrate` (dev only), `allow_mock_provider`,
  `max_upload_bytes`, `max_context_chars`, `default_token_limit`,
  `worker_poll_seconds`, `worker_lease_seconds`, `s3_bucket`, `s3_endpoint_url`,
  `s3_region`, `storage_backend` (`local`/`s3`), `model_master_key` (optional SecretStr).
  Prefix `SUXIAOYOU_CLOUD_`. Current schema revision: `cloud_0007`.
- `database.Database(settings)` owns `engine` and `sessions` async_sessionmaker;
  `initialize()` verifies schema (or migrates only when auto_migrate=true);
  `close()`. `get_db(request)` yields a session, no implicit auto-commit.
- `models`: models below, plus `utcnow()` and `new_id()`; no cascade deletion
  of usage records. Use SQLite-compatible BigInteger PK where needed.
- `identity.router`: auth + admin user APIs below. `require_user(request, db)`
  dependency returns `User`; `require_admin` checks role and active state.
  All unsafe cookie-auth requests need `X-CSRF-Token`. Auth sessions revocable.
  identity password helpers and `create_user(...)` usable by CLI/tests.
- `metering`: `reserve(db, user_id, model_id, job_id, amount)` -> RequestRecord;
  commits atomically and uses current month bucket. `mark_dispatched(db,id)`,
  `settle(db,request_id,usage,cost_microusd=None)` (server price snapshot by default),
  `release(db,request_id)` (only before dispatch), `mark_unknown(db,id,error)`;
  `usage_summary(db,user_id)`. `QuotaError` has code/message/status_code.
- `providers`: `build_provider(route,settings)` reuses existing provider factory;
  test-only deterministic mock is explicitly enabled. `normalize_usage(...)`
  returns input_total/output_total/cache_read/cache_write/reasoning/total_tokens.
  No SDK hidden retries. Real routes default to soft quota unless an input bound
  contract is configured. Full original tools execute inside isolated tenant
  engines, with their original approvals and sandbox policy.
- Root owns `main.py`, `routes.py`, `storage.py`, `worker.py`, deployment and integration.

## ORM models / field names

- `User`: id,email(unique lowercased),display_name,password_hash,role(user/admin),
  is_active,token_limit(BigInt),image_limit(BigInt default100),max_concurrent_jobs,created_at.
- `AuthSession`: id(hashed token),user_id,csrf_token,expires_at,created_at.
- `Workspace`: id,user_id,name,revision(default0),created_at.
- `Conversation`: id,user_id,workspace_id,title,created_at,updated_at.
- `Message`: id,user_id,conversation_id,role,content,created_at.
- `FileAsset`: id,user_id,workspace_id,filename,content_type,size,sha256,
  object_key,created_at. Files immutable; no public paths.
- `Job`: id,user_id,conversation_id,state(queued/running/completed/failed/cancelled/needs_review),
  prompt,model_id,max_output_tokens,file_ids(JSON list),idempotency_key,
  answer(default empty),error(default empty),cancel_requested(defaultFalse),
  lease_owner(nullable),lease_until(nullable),fencing(default0),attempt_id(nullable),
  created_at,updated_at. Unique(user_id,idempotency_key).
- `JobEvent`: id(integer auto PK),job_id,user_id,kind,data(JSON),created_at.
- `ModelRoute`: id(alias),name,provider,model,enabled,api_key_env,base_url(nullable),
  api_key_encrypted(nullable, server-only),custom_headers_encrypted(nullable, server-only),
  allow_unauthenticated(defaultFalse),credential_version(default0),
  strict_quota(defaultFalse),input_price_microusd_per_million(default0),
  output_price_microusd_per_million(default0),kind(chat/image defaultchat),
  image_price_microusd(default0),owner_user_id(nullable/shared),capabilities(JSON),created_at.
- `RequestRecord`: id,user_id,job_id(nullable logical id),model_id,source(default chat),
  state(reserved/dispatched/settled/released/unknown),reserved_tokens,
  total_tokens(default0),raw_usage(JSON),normalized_usage(JSON),cost_microusd(default0),
  error(default empty),price_snapshot(JSON),lease_owner(nullable),lease_until(nullable),
  created_at,dispatched_at(nullable),settled_at(nullable).
- `QuotaBucket`: id,user_id,period(YYYY-MM),limit_tokens,used_tokens(default0),
  reserved_tokens(default0),revision(default0). Unique(user_id,period).
- `Reservation`: id,request_id,bucket_id,amount,state; Unique(request_id,bucket_id).
- `UsageEvent`: id,request_id(unique),user_id,model_id,input_tokens,output_tokens,
  cache_read,cache_write,reasoning,total_tokens,cost_microusd,raw_usage(JSON),created_at.
- `AuditEvent`: id,actor_user_id(nullable),target_user_id(nullable),action,details(JSON),created_at.

## HTTP contract (all JSON datetimes ISO, arrays bare unless noted)

- GET `/auth/me` -> `{user:{id,email,display_name,role,is_active,token_limit,max_concurrent_jobs},csrf_token}`
- GET `/auth/captcha/challenge` -> `{challenge_id,background_image,piece_image,image_width:320,image_height:160,piece_width:48,piece_height:48,piece_y,expires_in:120}`;
  images are PNG data URLs. The target x stays server-side. An HttpOnly browser
  cookie binds the challenge; refreshing rotates it and invalidates old proofs.
- POST `/auth/captcha/verify` `{challenge_id,x}` -> `{proof,expires_in:120}`;
  x is the piece's left edge in original image pixels (0..272), tolerance ±4.
  A submitted position consumes the challenge, including a wrong position.
- POST `/auth/login` `{email,password,captcha_proof}` -> same as `/auth/me`;
  HttpOnly session cookie. Proof is consumed before password/rate-limit checks.
  Missing/expired/replayed proof: 403 `captcha_required`; wrong/expired puzzle:
  403 `captcha_failed`; independent captcha limit: 429 `captcha_rate_limited`
  with `Retry-After`. These errors use `{detail:{code,message}}` with Chinese copy.
- POST `/auth/logout` -> `{ok:true}`. POST `/auth/password` `{current_password,new_password}`.
- GET/POST `/admin/users`; POST accepts email,display_name,password,role,token_limit,max_concurrent_jobs.
- PATCH `/admin/users/{id}` `{is_active?,role?,token_limit?,max_concurrent_jobs?,reason}`.
- GET `/workspaces`; POST `{name}` -> workspace; GET/POST `/conversations`
  (POST `{workspace_id,title?}`); GET `/conversations/{id}` -> `{...conversation,messages:[...]}`;
  DELETE conversation doesn't delete usage.
- GET `/files?workspace_id=...`; POST `/files?workspace_id=...` multipart `file`;
  GET `/files/{id}/download`; DELETE `/files/{id}`. Metadata excludes object_key.
- GET `/models` -> public enabled route fields including strict_quota (no secret/env).
- Shared/admin and personal model writes accept `custom_headers`: omitted/null
  preserves, nonempty object replaces all, `{}` explicitly erases. Only
  `custom_*` and direct compatible `openai` chat routes accept headers; values
  are bounded, encrypted and write-only. Views expose only
  `custom_headers_configured`, `authentication_configured` and computed
  `authentication_mode` (`api_key`, `headers`, `api_key_and_headers`, `none`,
  `missing`). `key_configured` continues to describe only API Key credentials.
  `allow_unauthenticated=true` is explicit custom-chat-only public access and
  rejects any retained API Key, environment key or custom headers. Headers-only
  and public routes send no SDK placeholder Authorization. Rotations increment
  credential_version; audits/price snapshots never contain header values.
- POST `/jobs` `{conversation_id,prompt,model_id,max_output_tokens?,file_ids?,idempotency_key}`;
  GET `/jobs` list; GET `/jobs/{id}`; POST `/jobs/{id}/cancel`.
- GET `/jobs/{id}/events?after=0` SSE: `id: integer`, `event: kind`, `data: JSON`.
  Kinds: queued,started,delta,completed,failed,cancelled,needs_review; terminal states stop SSE.
- GET `/usage` -> `{period,limit_tokens,used_tokens,reserved_tokens,remaining_tokens,
  unknown_requests,unpriced_requests,total_cost_microusd,by_model:[{model_id,total_tokens,cost_microusd,unpriced_requests}],daily:[{date,total_tokens}]}`.
- GET `/usage/requests` bare request array (no secrets). GET `/admin/usage` ->
  `{users:[{user_id,email,...summary}],total_tokens,total_cost_microusd}`.
- GET `/admin/jobs`; POST `/admin/jobs/{id}/cancel`; GET `/admin/audit`.
- GET/POST `/admin/models`; PUT `/admin/models/{id}` -> ModelRoute public
  configuration (api_key_env allowed admins, secret values never returned).
  POST/PUT additionally accept write-only `api_key` and `clear_api_key`;
  omitted/blank api_key preserves the current key, clear explicitly removes it.
  Responses expose `key_configured` and `credential_source` (managed/environment/none),
  never api_key/api_key_encrypted. Managed keys are Fernet-encrypted in the DB,
  preferred over optional environment keys; decryption errors fail closed.
  `ModelSecretVault` uses a shared 0600 file in data_dir/secrets (0700), or an
  externally injected `SUXIAOYOU_CLOUD_MODEL_MASTER_KEY`. Credential changes
  increment credential_version and create a redacted audit event. Prepared
  requests compare that version before reservation. Save needs no restart.
  Admin connection tests use normal workspace/conversation/job/usage APIs and
  are charged against the administrator's quota; they do not bypass metering.
- GET `/admin/usage/export` CSV; POST `/admin/requests/{id}/reconcile`
  `{action:'release'|'settle',usage?:{},reason}` with audit, unknown only.

Errors: `{detail:{code,message}}` or `{detail:string}`. 401 unauthenticated,
403 forbidden/CSRF,404 inaccessible resource,409 conflict,429 quota/concurrency.

## Frontend

`NEXT_PUBLIC_CLOUD_MODE=true` uses cloud cookie authentication and routes signed-in
users to the complete original `/c/new` workbench. `/cloud?manage=1` contains account,
private BYOK models and admin users/quotas/usage/jobs/models/audit/Mac nodes. Original
API/SSE traffic goes through the authenticated per-user runtime proxy.

See `docs/cloud-model-gateway.md` for full-engine inference, image-count quotas,
private-route ownership and managed Mac-node protocols. These augment the legacy
Job contract above; a Job is not the execution unit of the complete Agent engine.
