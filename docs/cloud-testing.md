# 苏小有 Online 完整工作台开发与测试

Online 现在复用原客户端完整 React 工作台与原 Agent 引擎。登录后进入 `/c/new`，账户、个人模型与管理员控制台位于 `/cloud?manage=1`；`/m/*` 使用同一云登录态。旧轻量 `cloud_jobs`/Worker 保留历史兼容，新完整会话以每用户原引擎的 Session/Part/Goal/自动化数据库为准。

平台 PostgreSQL 保存身份、模型路由、加密凭据、配额、账本、审计与 runtime 元数据。每用户完整引擎运行在独立容器、私有网络和命名卷中，卷含 `/tenant/state`、`/tenant/home`、`/tenant/workspaces`。同源 `/api/*` 网关根据已认证用户选定实例，原 API/SSE 协议透传；浏览器不会获得实例令牌、端口或供应商密钥。工具、子 Agent、压缩、记忆、自动化等通过中央 Provider 网关计量，图片按独立图片额度统计。

移植状态与实测结果分别记录在 [F01–F56 验收表](cloud-parity/parity-acceptance.md)。源码复用、受控 API UI 回归、真实 DeepSeek 调用和真实容器验收是不同证据，不能互相代替。外部 OAuth 账号、图片供应商与专属 Mac 节点需要对应真实环境；未执行的项目继续列为未验收。

## 1. Compose 完整环境

要求 Docker Engine/Compose v2、可下载锁定依赖、足够的镜像/模型/租户磁盘。API 控制平面会创建容器、网络和卷，因此应使用专用 rootless Docker daemon。只有 API 挂载该 daemon socket；Worker、用户引擎、Office 转换容器均不继承 socket。不要把共享生产宿主 Docker socket 提供给测试实例。

在 Linux 部署机上确认专用 daemon 的 socket 路径及其**容器内可见 GID**，填写 `.env.cloud` 的 `CLOUD_DOCKER_SOCKET`、`CLOUD_DOCKER_GID`。rootless 常见路径为 `/run/user/<uid>/docker.sock`，实际以部署机为准；rootless user namespace 中看到的组号可能不同于宿主 `stat`，应验证非 root API UID 10001 能连接，而不是照抄示例的 GID 0。镜像构建、Compose、备份必须使用同一个 `DOCKER_HOST` 或 Docker context；远端/VM context中的bind路径属于daemon宿主。Nginx配置bind可选变量`CLOUD_NGINX_CONFIG`默认`./deploy/cloud/nginx.conf`；远端/VM daemon应先把仓库的`deploy/cloud/nginx.conf`复制到daemon宿主，再在`.env.cloud`设置绝对路径，例如`CLOUD_NGINX_CONFIG=/srv/suxiaoyou/deploy/cloud/nginx.conf`。该路径必须在daemon宿主实际存在，不能指向仅本机客户端存在的文件。

在仓库根生成权限为 0600 的私有配置，不创建任何默认账户：

```sh
python3 - <<'PY'
import os, pathlib, secrets
path = pathlib.Path('.env.cloud')
text = pathlib.Path('.env.cloud.example').read_text()
text = text.replace('CLOUD_DB_PASSWORD=\n', 'CLOUD_DB_PASSWORD=' + secrets.token_hex(32) + '\n', 1)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(text)
print('Created private .env.cloud; configure the dedicated Docker socket and GID.')
PY
```

设置准确 Origin/端口和 socket 后，**先构建完整 runtime 镜像**，再构建并启动平台。常规 `up --build` 不会构建只在 profile 中的 runtime-image：

```sh
docker compose --env-file .env.cloud -f compose.cloud.yaml config --quiet
docker compose --env-file .env.cloud -f compose.cloud.yaml --profile build-runtime build runtime-image
docker compose --env-file .env.cloud -f compose.cloud.yaml up -d --build
docker compose --env-file .env.cloud -f compose.cloud.yaml ps
curl --fail http://localhost:8080/readyz
```

`migrate` 完成 Alembic 后 API/Worker 才启动；API 默认不自动迁移。平台 API 为 Python 3.13，完整 runtime 为 Python 3.12 + Node 22，分别使用自己的依赖锁。runtime 带原 bundled skills/plugins/Office 资源及 Bash/Python/Node/git、字体、LibreOffice；签名 Office 转换额外使用无网隔离任务，不把宿主普通 LibreOffice 转换当权威签名证据。

升级 runtime/Office 镜像时，先构建新 runtime-image，然后重启控制面 API，等待 readiness 与 Office 镜像 pin/probe 成功，再逐个用户通过“云端访问”重启自己的引擎，取得当前 renderer descriptor。不要只重启用户引擎而保留旧 API 进程缓存的镜像身份。升级会使原模板的旧 renderer 批准与当前镜像不匹配，这是应保留的失效边界。用户模板保留旧 revision 和批准记录；点“重新校验”创建新 revision，逐页预览并显式确认后，才能在新 renderer 上实例化。不能沿用旧批准或固定 renderer 身份绕过这一步。

Compose 中 API 自动以 `suxiaoyou-control` 别名加入用户私有网络，完整 runtime 不发布宿主端口。内部模型 URL 为 `http://suxiaoyou-control:8000/api/cloud/internal/inference/v1`，Office URL 为 `http://suxiaoyou-control:8000/api/cloud/internal/office`。这两个地址为容器专用，不应设置成浏览器地址或直接暴露给用户。平台其他容器与数据库不加入用户网络。租户工具仍可按原产品权限访问公网，资源/网络攻击面需要单独对抗验收。

首个管理员通过终端安全输入密码：

```sh
docker compose --env-file .env.cloud -f compose.cloud.yaml run --rm --no-deps api \
  python -m app.cloud.manage create-admin --email admin@example.com --display-name 测试管理员
```

CLI 可从已有 `SUXIAOYOU_CLOUD_ADMIN_PASSWORD` 变量读取（`run` 增加 `-e SUXIAOYOU_CLOUD_ADMIN_PASSWORD`）。密码至少 12 字符；重复建号不会重置既有密码。访问 **http://localhost:8080/cloud**，登录后自动进入完整工作台。`/cloud?manage=1` 进入管理页。Origin 必须与协议、域名、端口完全一致；本机 HTTP 的 `cookie_secure=false` 只用于本机测试，远程使用可信 HTTPS 及 secure cookie。

## 2. 模型、密钥和额度

管理页“模型路由”配置共享模型；“个人模型”配置只对本人可见的 BYOK。已有真实 DeepSeek 托管凭据继续使用，无需重复输入或发送到聊天。创建新路由时选真实供应商和该账号当前可用模型，DeepSeek 兼容 Base URL 为 `https://api.deepseek.com/v1`。模型能力默认 `{}` 从原 catalog/中央 metadata 解析，也可明确覆盖视觉、工具、推理和上下文能力。

API Key 通过密码框仅写入、保存后立即清空，不回显、不存浏览器。平台 Fernet 加密保存；未外部注入主密钥时，共享数据卷 `secrets/model-master.key` 文件 0600、目录 0700。数据库密文与该主密钥必须一起备份；多节点需注入一致的 `SUXIAOYOU_CLOUD_MODEL_MASTER_KEY`。后台也支持 `SUXIAOYOU_MODEL_*` 环境变量兜底，个人模型禁止引用平台环境变量。仅修改环境方式需要重新创建 API/Worker，界面托管凭据保存后立即生效。

OpenAI或`custom_*`的对话路由支持自定义请求头；管理页与个人模型均使用只写输入，保存后即清空，接口不返回头名或头值。默认保留已有请求头，替换提交完整新字典，清除提交`{}`。仅`custom_*`可显式选择无需身份验证，必须同时清除Key、环境变量与请求头；headers-only路由可使用连接测试。

聊天连接测试会真正创建当前账户自己的Worker任务并计量；完整工作台使用原 Agent，经同一中央额度调用。图片路由使用独立 `kind=image`、SiliconFlow `Kwai-Kolors/Kolors`，测试按钮需明确点击“确认生成 1 张”，会真实收费并占图片额度。未配置图片单价时显示价格未知；不把零估价宣称免费。管理员可分别设置每月 Token、每月图片与最大并发。

每次模型 attempt 独立预占、结算，包含主 Agent、子任务、记忆、压缩及后台自动化；取消/断流后没有完整 usage 的请求保留 unknown 预占，不能自动重发可能收费请求。真实模型采用软额度，远端 tokenizer 的实际用量可能超过本地预估，不能宣称严格硬上限。价格按请求快照估算，缓存分档等实际账单以供应商为准。图片 unknown 按确认图片数量对账，不能用 Token 字段替代。

ChatGPT subscription 保留原授权与手工 callback 流程，经中央凭据/计量适配；OpenAI 固定 localhost callback 在云端不能假装浏览器能访问引擎 localhost，按界面使用手工回调。Google/MCP 云回调经可信 Origin 与云登录态回到本人实例，需要真实账号另验。用户自带 MCP/脚本使用自有外部凭据产生的费用不等于平台供应商 Token。

Rapid MLX 保留原模型操作界面。管理员在用户行“专属 Mac 节点”配置 HTTPS 地址、只写节点令牌和 leaf 证书 PEM pin，普通用户只操作自己绑定的节点。Linux runtime 不冒充 Apple Metal；节点服务、证书、网络和真实 Mac 必须准备并验证后才可标记 F42 通过。

## 3. 本机开发与回归

本机控制平面可使用独立虚拟环境与 PostgreSQL/SQLite；**完整 Agent 仍要求 Docker daemon 和已构建 runtime 镜像**。无 Docker 的旧轻量 Worker smoke 不代表完整工作台已运行。

```sh
python3.13 -m venv backend/.venv-cloud
backend/.venv-cloud/bin/python -m pip install --require-hashes -r backend/requirements-cloud-test.txt
(cd frontend && npm ci --legacy-peer-deps)
backend/.venv-cloud/bin/python scripts/cloud-dev.py init
backend/.venv-cloud/bin/python scripts/cloud-dev.py build
```

`init` 保存随机本机账号至被忽略的 `.cloud-dev.json`（0600），默认禁用 mock、保留既有账号。切换专用 PostgreSQL 可传 `init --database-url`，不要指向桌面或生产数据库。三个终端分别执行：

```sh
backend/.venv-cloud/bin/python scripts/cloud-dev.py api
backend/.venv-cloud/bin/python scripts/cloud-dev.py worker
backend/.venv-cloud/bin/python scripts/cloud-dev.py web
```

Web 为 **http://localhost:3018/c/new**，API 为 `http://127.0.0.1:8018`。宿主 API 模式使用 daemon 中的用户引擎 loopback 随机端口，容器内经配置的 control-plane gateway/Office 地址回连宿主；`host.docker.internal` 仅适用于支持该名字的 Docker 环境，Linux 应正确配置可达网关地址，不能把 `127.0.0.1:8018` 当容器宿主。`NEXT_PUBLIC_CLOUD_MODE=true` 和 Next rewrite 目标在构建时固定，`cloud-dev.py build` 已设置；切换 API 目标必须重新 build。独立构建可设置 `SUXIAOYOU_NEXT_DIST_DIR`，不要覆盖正在运行的 `.next-*`。

后端回归（PostgreSQL 用例需要专用 `CLOUD_TEST_POSTGRES_URL`，勿用生产库）：

```sh
(cd backend && .venv-cloud/bin/python -m pytest tests/test_cloud -q)
```

前端受控API回归（19项，含完整工作台、Mac/图片账本、记忆下载及共享/个人请求头认证），与真实浏览器联调分开记录：

```sh
(cd frontend && env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  NEXT_PUBLIC_CLOUD_MODE=true SUXIAOYOU_NEXT_DIST_DIR=.next-parity-test \
  SUXIAOYOU_UI_TEST_PORT=3341 SUXIAOYOU_UI_WORKERS=1 \
  npm run test:ui:headless -- --project=chromium tests/ui/cloud-full-workbench.spec.ts tests/ui/cloud-workspace.spec.ts)
```

未安装浏览器时运行 `npx playwright install chromium`。本机代理可能干扰 Playwright webServer 就绪探测；测试时清除代理不会修改服务运行配置。

真实轻量历史兼容 smoke 为 `scripts/cloud-smoke.py`；完整 Agent/runtime 验收为 `scripts/cloud-parity-smoke.py`，Goal/双子Agent/独立定时任务/插件/真实stdio MCP/记忆分阶段验收为 `scripts/cloud-persistent-smoke.py`。持久能力脚本使用独立账号并限制实际累计消费，复用已有阶段证据；不要在已知失败路径连续付费重试。它们会真实调用供应商并收费，不属于默认零费用测试。完整验收应保留会话 ID、工具事件、文件哈希和中央 request/usage 记录，不把 fixture 回归写成真实模型通过。

## 4. 验收门槛

逐项场景见 [F01–F56](cloud-parity/parity-acceptance.md)，至少涵盖：

| 节点 | 真实执行要求 |
| --- | --- |
| 身份与隔离 | A/B 登录、独立容器/网络/卷、跨账号资源和流拒绝；退出/停用立即撤权；前端草稿/权限/幂等键按用户隔离 |
| Agent 与交互 | 真实模型读写文件/执行命令、工具活动、Ask 允许/拒绝、Plan 转执行、排队/steer、停止/刷新恢复 |
| 文件与 Office | 浏览器多文件/目录上传、私有云目录、生成文件预览/下载/版本；DOCX/XLSX/PPTX 真实签名转换及预提交 |
| 持续任务 | Goal/记忆/自动化关页继续执行、插件/skills/MCP 实际工具、每次后台调用统一计量 |
| 用量与管理 | Token/图片额度、幂等/并发、unknown 保留与核对、密钥不回显、CSV/审计一致 |
| 持久与恢复 | 正常停止/启动保留文件/Session/Goal/记忆；不自动重复付费调用；完整 tenant 备份恢复到独立空目标 |
| 平台依赖 | 真实 OAuth/渠道账号、Ollama 模型、Mac Metal 节点和外部媒体/浏览器兼容分别记录，未测不写通过 |

用户运行时 `/remote` 显示云地址及启动/重启/停止，不开启绕过平台身份的公开引擎隧道。桌面应用仍保留原 remote 分支。窗口、托盘、Finder/Office 本机打开在 Web 由浏览器布局、通知、预览/下载及云运行时控制等价承接，浏览器不会控制用户尚未连接的本机文件系统。

## 5. 完整租户备份与恢复

`deploy/cloud/backup.py` format 2 同步保存 PostgreSQL、旧 `cloud_objects` 和**全部注册用户完整 tenant 卷**。先验证 DB 用户与容器/卷 owner SHA-256、generation、`/tenant` mount；停止 Web/代理/API 新准入，优雅停止原运行引擎，等待旧 Worker 排队任务结束后停止 Worker；再归档 SQLite/WAL、工作区、HOME、MCP/订阅凭据和文件。`finally` 按停止前记录的精确容器ID恢复所有原运行服务与引擎（API健康后恢复引擎），不通过Compose start隐式启动原来未运行的依赖或迁移；不会构建镜像或重建容器。未完成目录保留 `.partial-*`，不会标为有效备份。

会短暂停止工作，请在用户可接受的窗口执行；宿主 Docker CLI 必须与平台使用相同专用 daemon：

```sh
python3 deploy/cloud/backup.py backup backups/cloud/snapshot-001 --drain-timeout 120
```

目录 0700、文件从创建时起 0600，所有归档带 SHA-256；包含密码哈希、加密平台 Key、平台文件主密钥和用户运行时私密凭据，应整包加密并存入受控异地备份。环境注入的主密钥/Secret 不在包内，需单独保管。S3 模式需要协调桶快照，此脚本明确拒绝代替 S3 备份。

恢复只接受空数据库、空对象卷，且不覆盖任何已有 tenant 卷/容器。**仅换 Compose project 不够**：租户卷按用户派生，在同一 daemon 全局同名；演练必须选隔离且无源租户的空 daemon/context。先在目标 daemon 构建相同版本 API/runtime 镜像，准备 0600 的 `.env.cloud.restore.local`（新入口 8081、匹配 Origin/主密钥），再执行：

```sh
# 当前 Docker context / DOCKER_HOST 必须是独立恢复目标。
python3 deploy/cloud/backup.py --env-file .env.cloud.restore.local \
  --project-name suxiaoyou-cloud-restore restore backups/cloud/snapshot-001
docker compose --env-file .env.cloud.restore.local -f compose.cloud.yaml \
  --project-name suxiaoyou-cloud-restore up -d api web proxy
curl --fail http://localhost:8081/readyz
```

恢复先验证校验和及归档路径，再核对恢复 DB 的 user/generation 与 tenant 归档集合。创建新 owner-labelled 卷，拒绝路径逃逸；保留合法venv symlink并在文件写完后创建，写入过程不跟随symlink；安全umask077建目录，写完后倒序恢复归档目录权限，保留secrets0700与密钥0600。恢复删除登录 sessions、提升 runtime generation 并清空旧容器 endpoint，下一次授权启动轮换实例/inference capabilities。unknown 账本与预占保持不变，不自动退款；不自动启动任何用户引擎或 Worker。

管理员重新登录，确认未知用量和源进程已被隔离，再启动 Worker，并按 F 表检查新任务和所有文件：

```sh
docker compose --env-file .env.cloud.restore.local -f compose.cloud.yaml \
  --project-name suxiaoyou-cloud-restore up -d worker
```

恢复环境接替原环境前，必须先阻断旧 API/引擎/Worker 对数据库、对象存储和模型凭据的访问并轮换相应 Secret，不能仅切 DNS。格式 2 兼容无 tenant 的格式 1；旧快照若数据库已有 tenant 却无对应卷归档会拒绝恢复。原版本 SQLite 的运行中 SSE 内存不会跨容器重启永久重放，应从持久 Session 恢复并显示中断状态，不重复执行已收费请求。

## 6. 排查与测试边界

```sh
docker compose --env-file .env.cloud -f compose.cloud.yaml logs --tail=100 api worker migrate
docker compose --env-file .env.cloud -f compose.cloud.yaml stop
```

普通停止保留全部卷，勿用 `down -v` 删除数据库/对象；租户卷由 supervisor 独立管理。代理请求体上限 51MiB，默认单文件 50MiB；调整需同时检查 API/runtime/代理资源限制。不要在共享日志或聊天粘贴 `docker compose config` 展开的 Secret、.cloud-dev.json 或节点令牌。

当前交付目标是具备真实功能测试条件。OIDC、组织共享、PostgreSQL RLS、公网 TLS/容量/恶意租户对抗、S3、真实凭据与恢复演练按实际证据单独验收；不因这些平台生产门槛未完成而删掉客户端业务能力，也不把容器能启动等同于公开生产安全认证。
