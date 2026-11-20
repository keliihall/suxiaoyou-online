# 苏小有 Online 与客户端能力逐项核验：Agent 与运行时

初始差异核验日期：2026-11-15。以下矩阵保存改造前基线，实施后状态另见文末与 `parity-acceptance.md`，不可把基线“缺失”当作当前状态。核验对象是当前工作区 `suxiaoyou/` 的源码；这是源码可达性核验，不是本轮重新执行了客户端所有测试。没有读取真实密钥，没有调用付费模型。初始审计阶段只生成评估文件；本轮后续已按下述方案实施业务代码。

## 初始结论与实施边界

目前 Online **不具备客户端完整能力**。`backend/app/cloud/CONTRACT.md:3` 明确说明使用独立 cloud 应用和 `cloud_*` 表、不挂载桌面路由；`backend/app/cloud/worker.py:127` 的 `_payload` 只把历史文本和只读附件交给模型，`execute:172` 只消费 text-delta/usage/error。它没有启动 `SessionPrompt`、`SessionProcessor`、`ToolRegistry`、Goal 控制器、调度器、技能/插件/连接器注册中心。真实 DeepSeek 接入并不能补齐这些缺失。

应保留现有 Online 身份、后台、计量模块，**为每个用户运行独立的原版完整引擎实例，再把原版前端的完整工作流适配为浏览器交互**。原客户端完整引擎已具备大部分所需能力，重写 cloud worker 的工具循环会再造多套状态机，且无法可靠覆盖 v1.1 的检查点、事务、暂停、委派权限、恢复语义。

独立进程是解决 Python 单例及 cwd/HOME 混用的必要条件，但**同 UID 普通子进程不是租户安全边界**。完整工具面包括 Bash、Python、本地 MCP、项目 Hook，因此生产应采用每用户独立容器/OS 身份与独立卷；本地调试可使用受控独立进程，但必须标明这一层尚未达到敌对多租户隔离验收。不能因为绑定 loopback、设置 HOME 或 ToolContext.workspace 就声称已经完成租户隔离。

客户端中已有代码、已发布功能、具备外部依赖后可运行，是三种不同状态。当前客户端源码中渠道和远程隧道仍关闭发布门，不应误报成 Online 丢失的已发布功能；它们也不能作为满足“完整移植”的理由静默永久删除，需在总表保留当前状态。

## 基线逐项核验矩阵

以下接口均省略原版统一前缀 `/api`。Online 栏指当前 `/api/cloud`，不是仓库是否仍保留原文件。

| 编号 | 客户端实际能力及源码 | 客户端可达性/依赖 | Online 当前差异 | 迁移实现及验收场景 |
|---|---|---|---|---|
| A01 | 多轮 Agent 工具循环：`session/prompt.py:271`、`session/processor.py:1077`、`session/llm.py:23` | `POST /chat/prompt`；Build/Plan 模式、reasoning、工具规格、上下文/步骤/循环限制 | 缺失；当前为一次模型回答，没有任何工具规格 | 路由到独立原引擎；真实模型根据用户请求连续调用 read→edit→验证，前端显示过程与最终文件，浏览器刷新可恢复 |
| A02 | Build/Plan、Explore/General、隐藏 Compaction/Summary/Title/Validator：`agent/agent.py:31` | `GET /agents`；自定义 agents 从 Settings 和 `.suxiaoyou/agents/*.md`、`.agents/*.md` 加载 | 无 Agent 选择、无自定义 Agent | 原注册表复用；Plan 禁止写入、可提交计划并切 Build，独立用户自定义 Agent 仅本人可见 |
| A03 | Task 子 Agent、显式并行/串行批量委派：`tool/builtin/task.py:34`、`api/chat.py:556` | `POST /chat/task-batch` 最多 12 项；workspace、权限上限、取消/深度/超时继承 | 缺失 | 复用 TaskTool 和批量执行；两个子任务并行、用户可查看子会话，父取消停止子执行，子不能越过父拒绝规则 |
| A04 | 过程中追加、排队、steer、排序与删除输入：`api/session_inputs.py:135`、`session/input_queue.py` | `/chat/inputs` POST/GET/PATCH/DELETE；持久化输入与请求幂等 | 缺失；单会话同一时刻一个 Job | 原接口完整代理；生成中追加两条、重排、取消其中一条，steer 在安全边界进入；重试不能重复执行 |
| A05 | 流式文本、推理、工具输入/输出、进度、子任务、状态与重连：`streaming/events.py`、`streaming/manager.py:71`、`api/chat.py:879` | GET/POST `/chat/stream/{stream_id}`，Last-Event-ID/last_event_id；活动作业 `/chat/active` | 当前只支持 queued/started/delta/终态的 cloud JobEvent；缺少原完整事件协议 | 保持原事件协议的无缓冲代理并恢复完整 UI；断网重连不重复工具调用；对方 stream_id 不可订阅 |
| A06 | 工具授权、询问、计划批准/拒绝与持久化响应：`tool/context.py:26`、`agent/permission.py`、`api/chat.py:1155` | `POST /chat/respond`；来源能力上限、会话权限、父子权限交集；`ask` 无回调默认失败 | 没有交互等待与授权组件 | 浏览器展示原权限/Question/SubmitPlan 交互；允许一次/拒绝均生效；刷新后仍能作答；跨用户响应 404/403；相同响应幂等、冲突响应 409 |
| A07 | Todo 与自动继续完成未完成任务：`tool/builtin/todo.py`、`session/prompt.py` | `todo` 工具，continuation/步骤/循环/no-progress 边界 | 缺失 | 保留 Todo 卡和续行；多步任务必须按完成情况更新；用户停止后不得因 Todo 自动重启 |
| A08 | 持久 Goal 创建/编辑/暂停/恢复/清除、预算及自动续行：`api/goals.py:95`、`session/goal_controller.py`、`session/goal_manager.py`、`session/goal_guard.py` | `GOALS_RELEASED=True`、`AUTONOMOUS_GOALS_RELEASED=True`；`/chat/goal`、`/sessions/{id}/goal`、`.../usage`、`.../pause`、`.../resume` | 全部缺失 | 原控制器与 Goal 表保留在用户引擎 DB；交互等待时持久化 waiting_user；暂停即阻止下一次模型/工具；恢复不重复已执行步骤；Goal 预算与用户月额度同时生效 |
| A09 | 文本/文件读写与查找：`main.py:1175` 注册 read/write/edit/apply_patch/glob/grep；`tool/builtin/*` | 工作区边界、附件白名单、事务、版本历史/恢复；FTS search 由 fts_enabled 条件注册 | 仅只读附件提取；没有实际可编辑工作树、文件版本 | 云文件树映射到用户卷，不接受宿主绝对路径；编辑生成差异、恢复指定版本，用户 B 不能访问 A 的路径/附件 |
| A10 | Bash 终端与 Python 数据计算：`tool/builtin/bash.py`、`code_execute.py`、`tool/sandbox.py:590`、`execution_workspace.py` | 已注册；执行前运行时探测，Linux 要求 bwrap 可用及命名空间能力；macOS Seatbelt；默认 ask | 当前明确未开放任意代码执行 | 云 runtime 配 Python/Node/git/命令运行环境并保留内部沙箱；运行 pandas 生成图、依赖安装、终止进程树；读不到平台 DB、平台主密钥、其他租户卷；网络不越过平台策略 |
| A11 | Web Fetch/Search：`tool/builtin/web_fetch.py`、`web_search.py`；`session/processor.py:559` | 已注册；网络、搜索供应商/代理/公开回退及每日搜索额度；web_fetch 有公网地址检查 | 缺失；DeepSeek 调用没有附带搜索工具 | 原工具复用；网页提取与引用、搜索结果接入对话；私网/云元数据地址拒绝；搜索计数按用户隔离 |
| A12 | Artifact、PresentFile、图片生成、Office 文件工具：`tool/builtin/artifact.py`、`present_file.py`、`image_generate.py`、`office.py` | 已注册；图片有单独 paid 授权与 image_generation ledger；Office 还有渲染/校验/预提交协调器依赖 | 只读 Office 提取，不能创作或展示原 Artifact/文件工具输出 | 原完整事件/文件 API 与浏览器预览复用；生成真实 docx/xlsx/pptx/png 并下载、可继续编辑；无 renderer 时不能误报权威验证已完成；图片费用不能漏出平台统计 |
| A13 | 自动/手动 Compaction 与历史编辑重发：`session/compaction.py`、`api/chat.py:620,759` | `/chat/compact`、`/chat/edit`；摘要模型同 ProviderRegistry；标题目前直接取第一条文本（`prompt.py:2342`） | 超上下文直接报错，没有压缩、历史改写与重跑 | 长会话压缩后继续工具任务；编辑旧消息会删除下游并重新生成；幂等与任务取消顺序一致；压缩 Token 也入平台额度 |
| A14 | Workspace Memory 持久化、注入、去抖自动更新、手动编辑/刷新/导出：`memory/workspace_memory_*`、`memory/injection.py`、`api/workspace_memory.py` | `/workspace-memory`、`/list`、`/refresh`、`/export`；默认启用、200 行 cap；后台模型更新默认 10 秒 debounce | 没有长期记忆；只有会话文本 | 用户 runtime 独立 DB 中按 workspace 维护；新会话自动带入同工作区记忆，跨工作区/用户不串；手动刷新与后台刷新 Token 计入对应用户 |
| A15 | 技能发现、查看、启停、安装及商店检索：`skill/registry.py:31`、`api/skills.py`、`tool/builtin/skill.py` | bundled→HOME→外部 project→project 优先级；`/skills`、`.../enable/disable`、`/skills/install`、`/skills/store/search` | 没有技能目录、安装或技能工具 | 容器保留 bundled skills；每用户 HOME、project 单独持久化；上传/安装真实 SKILL.md，模型调用后可完成其任务；停用和覆盖重启后不丢失 |
| A16 | 插件加载、列表/详情、启停并带入技能/Agent/连接器：`plugin/__init__.py`、`plugin/loader.py`、`plugin/manager.py`、`api/plugins.py` | bundled、`~/.suxiaoyou/plugins`、project plugins；默认 disabled，状态持久化 | 没有插件中心；不是仅少一个菜单，而是注册流程完全未启动 | 启用 bundled 插件使对应 Agent/skills 可用，禁用撤销；用户安装目录独立；保持 provenance，用户插件不能伪装内置可信连接器 |
| A17 | 连接器目录、定制连接器、MCP stdio/SSE/streamable HTTP、工具发现/调用：`connector/registry.py:71`、`mcp/client.py:50`、`mcp/tool_wrapper.py`、`api/connectors.py` | `/connectors` CRUD/enable/disable/connect/disconnect/reconnect；本地命令 fingerprint 审批；动态 ToolSearch 由 `connector/registry.py:589` 注册 | 全缺失 | 原管理页面适配云凭据和回调；远程 MCP 能调用真实工具，本地 MCP 只在租户容器内启动；参数变化重新审批，停止运行时阻止启动；调用过程和结果回流 |
| A18 | MCP OAuth、Bearer/Raw Authorization、token 存储和刷新：`mcp/oauth.py`、`mcp/token_store.py`、`api/mcp.py`、`api/connectors.py` | `/mcp/status`、`.../auth-start`、`/mcp/oauth/callback`、`.../auth-callback`；Google 特殊入口另有 `api/google_auth.py` | 当前管理员模型 Key 与用户连接器凭据不是同一能力 | 回调 state 服务端绑定 user/runtime/connector，一次性消费；公网回调转入正确用户，不能信任回调传入 user_id；令牌不暴露给其他用户或模型，刷新后可继续调用 |
| A19 | Cron/interval/循环自动化、模板、历史执行：`api/automations.py`、`scheduler/engine.py:37`、`scheduler/executor.py` | `/automations` CRUD、`.../run`、`.../runs`、templates/loop-presets；30 秒轮询，单实例调度、并发 3、重启中断恢复 | 全部缺失 | 用户关闭浏览器后 runtime 仍驻留/能被唤醒；定时只执行一次，手动运行可看结果和历史；停用用户/额度不足时不得启动模型；保留 scheduler 来源只读能力上限，不能假装定时器具有桌面全权限 |
| A20 | 安全中心、工具开关、紧急停止和审计：`api/security.py:39`、`security/control.py`、`security/audit.py`、`security/capabilities.py` | `/security/overview`、audit、PUT tools、POST emergency-stop；停止调度/连接器/进程的控制流 | 只有平台 admin/audit 和 Job cancel，缺少完整引擎安全状态/审计 | 租户安全中心与平台后台并存；紧停阻止下一工具及后台任务，重启仍停；后台能停用用户 runtime；不把租户紧停变成全站紧停 |
| A21 | 检查点、事务/重启恢复、Rewind、Workspace Worktree：`runtime/checkpoint_runtime.py`、`runtime/rewind.py`、`worktree/runtime.py`、`api/runtime_control.py` | v1.1 gates=True；`/runtime/context`、checkpoints、rewind preview/execute、worktrees inspect/create-bind/release/gc；SQLite lease、文件身份标记 | 全缺失 | 复用用户引擎 DB/私有事务目录与卷；生成修改后看到检查点并预览/执行 rewind；git 工作区建立独立 worktree，不越租户；中断恢复不重复提交也不破坏原文件 |
| A22 | 项目 Hook、信任指纹、审批、生命周期联动：`hooks/config.py`、`hooks/registry.py`、`hooks/trust.py`、`hooks/runtime.py`、`api/security.py:308` | v1.1 Hook gate=True；`/security/hooks`、`.../revoke`；实际执行经 dispatcher/runner 与来源限制 | 缺失 | 租户工作树加载 Hook；首次执行需原确认路径，文件变更重新确认；pre/post tool 可观测；Hook 进程同样不能触及平台凭据/其他用户 |
| A23 | 自动检查点验证 Agent 与 Office 预提交 Repair：`validation_agent/service.py`、`main.py:994`、`office_validation/repair_agent.py` | gate=True；Validator 独立只读注册表和来源能力上限；Repair 有专门预算及 observer | 缺失 | 完成修改后存在验证结果；Validator 不可借工具注入写文件；Repair 次数/Token 计入用户与 Goal，取消/紧停后不再发送请求 |
| A24 | ACP stdio Beta、协议会话、反向权限：`acp/cli.py:39`、`acp/server.py`、`acp/session_bridge.py` | gate=True；console script `suxiaoyou-acp`；要求独占 file-backed SQLite lease，不是浏览器 HTTP 路由 | 缺失 | 若提供 Online ACP 外部客户端入口，应在已有用户 runtime 内桥接协议或独立适配，不能并启另一个 lifespan 抢同 DB；经用户身份绑定，保留 ACP 能力上限和 reverse-permission；浏览器使用本身不需要强行把 stdio 模拟成 UI |
| A25 | 渠道消息入口：Telegram/Discord/Slack/飞书/钉钉/微信等 `channels/*`、`api/channels.py` | **当前客户端发布门 `MESSAGING_CHANNELS_RELEASED=False`，未挂 API/未起消费者**；配置项也默认 false | 同为未开放；不能宣称这是已经可用的客户端功能 | 总能力表明确“源码存在但客户端未发布”；若用户要求连这些也上线，需要独立完成渠道账号隔离、回调验证、后台推理计量与 source ceiling，不能仅改 False→True |
| A26 | 远程隧道/手机伴随访问：`api/remote.py`、`remote/*`、auth middleware | **`REMOTE_ACCESS_RELEASED=False`**，启动会强制关闭；不是现有完整 Web 云服务 | Online 已有另一套网页登录，不能复用旧 remote token 当多用户身份 | Online Web 身份替代隧道入口；不需要在云上启动一人一条 cloudflared，但保留移动使用能力并测试 |

补充准确性：`tool/builtin/memory.py` 已是废弃兼容说明，不在当前内置工具注册表中；本轮应迁移的是 Workspace Memory，不能把旧 memory 文件当成还可调用的全局记忆工具。Agent 的 Title 模板存在也不代表每次自动标题都发生付费调用；目前标题直接截取首条用户文本。

## 为什么必须独立运行实例

1. `dependencies.py` 的数据库 factory、Settings、Provider/Agent/Skill/Tool/Connector/Plugin/Stream/IndexManager 全是 module 级变量，后一次 `set_*` 会替换之前实例。`create_app(settings)` 也调用全局 `set_settings`。
2. `config.get_settings()` 用 `lru_cache`；`security/control.py:170` 有全局 SecurityControl；`memory/workspace_memory_queue.py:364` 有全局队列；`memory/config.py` 有全局 config；凭据 store 是进程级单例；搜索 quota 在 SessionProcessor 模块中是桌面单用户追踪器。
3. 原数据库 `session`、Goal、Task、记忆等无 `user_id`。内存 StreamManager 按 `stream_id` 取 Job，没有 cloud user 维度。因此直接把桌面 API 挂进当前 cloud FastAPI 会打开跨用户读写和执行面。
4. 路径解析依赖进程 cwd 和 HOME。Skills/plugins 从 `Path.home()` 发现；Connector state 常在 project `.suxiaoyou`；MCP token 在 cwd/data/credentials；private root、security-state、session token、SQLite、FTS/事务/检查点均与 cwd/工作区相关。进程中切 `os.chdir`/改 `os.environ` 不可能支持并发多租户。
5. 同一引擎不能用多个 Uvicorn worker 指向同一 SQLite：原版有独占数据库 lease、内存生成和响应状态。独立用户实例应 `workers=1`；横向扩展首先按用户路由，而不是加进程共享同文件。

## 可直接落地的引擎宿主管理契约

建议分离平台 control plane 与用户 runtime，不重写原模型/工具协议。

- 平台 PostgreSQL 继续存 User/AuthSession/ModelRoute/额度/审计，增加 RuntimeInstance(user_id 唯一、instance_id、state、private endpoint、generation、last_seen、image_version) 和用户 workspace 绑定。endpoint/token 只服务端可读。
- 每用户持久卷固定布局：`/tenant/private`（cwd、SQLite、session token、credentials、事务等）、`/tenant/home`（租户 HOME）、`/tenant/workspaces`（用户可见内容）。private 与 workspace 必须是互不包含的 sibling，原沙箱明确拒绝 workspace 覆盖 private root。
- 运行前设置 `HOME=/tenant/home`、`XDG_*`、`SUXIAOYOU_PRIVATE_DATA_DIR=/tenant/private`、`SUXIAOYOU_DATABASE_URL=sqlite+aiosqlite:////tenant/private/data/suxiaoyou.db`、`SUXIAOYOU_PROJECT_DIR=/tenant/workspaces/default`、`SUXIAOYOU_SESSION_TOKEN_PATH=/tenant/private/session_token.json`、`SUXIAOYOU_EXTRA_ALLOWED_ORIGINS=<Online Origin>`。**这些必须在 import app.main/app.config 前完成**。`backend/run.py:351` 已展示这个初始化顺序，可提取复用；不要在已载入的云进程内执行它。
- 初始化时只创建平台生成的目录/ID；用户提供“项目名称”不能变成任意宿主路径。确保 runtime HOME/cwd 不继承操作者的 `~/.suxiaoyou`、`.env`、keychain、SSH agent/socket、Docker socket、平台服务账号文件。
- Runtime 只监听内部 loopback/UDS 或 private network；gateway 根据已认证 user_id 选择端点。平台使用隔离的控制通道保存/读取 rotating runtime token，再注入 Authorization；浏览器永不获得此 token、端口或租户内部地址。
- **内部 peer 边界需要适配**：`auth/local.require_local_session` 还要求请求 peer 为 loopback 且 token source=local。若 gateway 位于另一个容器的 TCP 地址，单纯代理 token 会 403。采用每用户 sidecar/UDS 本地代理，或显式引入“内部平台已认证 ingress”判定；不能全局移除此检查、不能相信公网 X-Forwarded-For。
- 复用原 source policy。`security/capabilities.py` 当前 invocation_source 没有 `cloud_interactive`。浏览器交互等价客户端操作时，应新增 server-owned source 且显式映射完整交互能力；临时由网关私有适配复用 desktop source 也须有服务端信任边界。用户 JSON 不得自行选择 source。Goal/scheduler/acp/validator 原独立上限保持。
- 未登录、停用用户、销毁实例、generation 变更后，不得继续代理旧会话/SSE；每次请求重新查 active/runtime ownership，SSE 周期检查会话撤销并关闭。运行中的任务取消/紧停使用 control channel，不依赖浏览器仍在线。
- 自动化与 Goal 不能因为 idle eviction 消失。第一版可保持 runtime 常驻；后续唤醒服务必须理解下次调度时间，不能以“无浏览器连接”判断空闲。平台重启根据 RuntimeInstance 重建一次，不启动重复 scheduler。
- Cloud 自有 Conversation/Message 和原 Session/Part 类型不同。新功能以原 Session 为源；旧 cloud 会话可保留历史入口或通过显式可追踪导入创建原 Session + TextPart，原 Job 记录不自动推断成 Goal/工具执行。文件迁移要复制到用户卷并建立稳定资源映射，不给用户暴露原 object_key。

## SSE 与原前端协议接缝

原完整工作台依赖的不是 cloud JobEvent 格式。应提供同域 gateway 路由，例如 `/api/runtime/*` 转为对应原 `/api/*`，浏览器 API client 切换 base path。使用 fetch 流读取或 cookie 认证的 EventSource，禁止把 runtime bearer 放入 URL。原 `/chat/stream/{id}` 的响应 headers、event/id、Last-Event-ID、heartbeat、连接断开要透传，代理禁用 buffering；正文不能先 `.json()`/`.read()` 聚合。`/chat/respond`、`/chat/abort`、`/chat/inputs` 与 SSE 必须命中同一用户实例和同一 generation。

原 StreamManager 的 SSE 缓冲在内存里；它能断网重连但不等于跨进程永久回放。重启应读取原持久会话/Goal/执行状态，返回明确中断/恢复结果，不能把同 stream_id 自动重发一遍付费请求。现有 cloud 持久 JobEvent 的优点可作为后续事件落盘适配，但不可因此舍弃原事件类型及交互状态。

## 平台计量网关的具体接入点

只在 `POST /chat/prompt` 或 `SessionProcessor._stream_llm_with_retry` 扣一次 Token 会漏计子 Agent、自动压缩、记忆、Office Repair、验证器、Goal 续行和自动化。应在 Provider 边界统一计量，且要处理脚本和 MCP 直接网络出站绕过模型网关的问题。

源码调用位置（本轮 `rg stream_chat` 核验）：

- 主交互、子 Agent、Goal、scheduler、validator 最终经 `session/llm.py:66` → `BaseProvider.stream_chat`；调用上下文有 Session/Goal/turn/checkpoint 身份。
- 手动/自动 compaction 在 `session/compaction.py:387,406` 直接调用 Provider。
- Workspace Memory 在 `memory/workspace_memory_queue.py:285` 直接调用 Provider，且独立于活跃生成任务；当前只写 synthetic assistant usage，不写平台 ledger。
- Office precommit Repair 在 `office_validation/repair_agent.py:214` 直接调用 Provider；其 observer 会记 Goal usage，仍不能代替平台统一扣额。
- 图片由 `image_generation/siliconflow.py` 独立调用、`image_generation/ledger.py` 独立记账，不能归到聊天 Token 为零就漏掉 paid 调用。

实施方式：

1. 为用户 runtime 注入 `GatewayProvider`（实现原 BaseProvider 接口）或统一 `MeteredProvider` 包装注册表中的每个 Provider。更稳妥的生产边界是 runtime 只有用户绑定的短期网关凭据，真实供应商密钥留在平台；管理员在 Online 配置的 DeepSeek Key 继续由 cloud secret vault 管理。
2. 每一次模型 HTTP attempt 都产生唯一 platform request_id；上下文至少 user_id、runtime_id/generation、session_id、root_turn_id、child_session_id、goal_id/run_id、source（chat/subagent/compact/memory/office_repair/validator/automation）、step/attempt、model route version。平台从凭据绑定身份，不能信任请求体中的 user_id。
3. 复用 `cloud.metering.reserve` / `mark_dispatched` / `settle` / `mark_unknown`，当前 RequestRecord.job_id 已为 nullable logical ID，可增加明确 runtime/session 关联而不伪造 cloud Job。每步调用单独预占及结算，保持 immutable price/credential route snapshot、并发原子准入、幂等 settlement。
4. 计费输入包含 messages、system、tool schemas、附件/图像及请求额外体；现 `providers.input_reservation` 是轻量问答 payload 使用，接入完整 Agent 时必须扩展为多模态和 tools 规格，不能沿用只统计文本的估值。真实供应商 strict quota 目前明确拒绝，不能把估计值包装成硬上限。
5. 最终 usage 缺失/取消/断线保持 unknown 预占；重试建立新的 attempt 并单独计账，SDK 隐式重试禁用。原 SessionProcessor 自身有 retry，平台不能只按“同一用户消息”去重，否则第二次付费请求会漏账。
6. 月额度耗尽和账号停用不仅拦 HTTP 新会话，还必须拦每一步 Provider（含 background memory/automation/Goal）。平台不能让 runtime 缓存一个长期“已批准总额度”而失去中央准入。
7. 保留原 GoalBudget 作为任务局部预算；平台月额度是额外上限。先通过平台 reserve 才可出网，Goal pause/permission/工具 admission 仍在原执行锁范围内协调。失败不应删除已计账记录。
8. 出站限制必须确保原 `/config`、自定义 endpoints、任意环境变量、Bash/Python/MCP 不能取得平台供应商 Key 或绕过平台网关消耗平台额度。用户自带外部账号/外部 MCP 的费用与平台模型额度分开标识；在缺乏可观测供应商调用时不要声称“所有外部 Token 均可精确统计”。

## 镜像与运行依赖需要补齐

目前 cloud 专用 requirements 与 Dockerfile 是轻量问答部署，不包含完整引擎的运行依赖保证。应基于原 `backend/pyproject.toml`/平台锁文件做新的 runtime 镜像，而不是只向 cloud image 加一个入口。

- Python 包：MCP、croniter、PyYAML、keyring、ACP SDK、SQLite/Alembic/FTS、Office 依赖、numpy/pandas/matplotlib、模型 SDK 等按原锁验收。现 cloud SDK 版本与 desktop lock 不同，需隔离虚拟环境/镜像，不能直接覆盖当前线上 API venv。
- OS：Linux 的 bubblewrap、可用 user/pid/network namespaces、Shell、Node/npm、git、证书、字体、Office renderer/相关协议要求。rootless 容器里 bwrap 可能由于嵌套 user namespace/seccomp 被拒绝，必须以真实 `sandbox_self_test` 验收，不能用 `privileged: true` 解决。
- Bundled 资源：完整 `app/data/skills`、`app/data/plugins`、连接器 catalog、prompts、Office 模板及校验数据，不可沿用排除 app/data 的轻量构建上下文。
- 凭据：无头容器 native keyring 不一定可用，原 private 0600 fallback 需要独立租户卷；平台主密钥只在 control plane，租户 Shell/MCP 不得读取。Google/OAuth redirect URI 需要云回调适配，不能继续回到用户浏览器 localhost:<runtime port>。
- 本地模型：Ollama/Rapid-MLX 管理的是运行引擎所在机器。云端可提供托管/远端兼容接口，但浏览器不能直接继承用户 Mac 的 Apple Silicon 进程。完整能力总表应写清等价云运行方式，不能伪称云 Linux 能原样运行 Rapid-MLX 的本机管理。

## 建议实施顺序与必须通过的验收门

1. **Runtime 启动骨架**：独立 HOME/cwd/SQLite/卷启动原 create_app，健康探测与销毁；两个用户实例并存，原 `/tools` `/agents` `/skills` `/plugins/status` `/runtime/context` 均能通过平台身份访问。拒绝通过公网指定 endpoint/runtime_id。两用户文件/会话/密钥/紧停互不影响。
2. **真实模型统一网关**：管理员 DeepSeek 配置驱动两个原引擎；真实工具调用完成工作；每一步含 child/compact/memory 的 usage 正确入中央 ledger；额度不足时两实例并发都不能越过原子准入。先完成这一层，才可称真实可使用。
3. **完整交互面**：原工作台会话、文件树、工具详情、推理、Question、权限、Todo、计划、Task/SubAgent、编辑重发、流重连；不要以功能清单或按钮存在作为完成证据，逐一执行矩阵场景并保存记录。
4. **持续任务/扩展面**：Goal/后台自动化/Memory/skills/plugins/MCP/连接器/OAuth；浏览器关闭仍能运行，失效凭据可重新连接，账号禁用立即阻止下一模型/工具。
5. **v1.1 完整运行时**：Office 创作/预览/验证、图片、检查点/rewind、Hook、worktree、验证 Agent、ACP 适配；运行原相关 regression suites + 新增 gateway/两租户/浏览器流程验证。原 tests/test_goal、test_api/test_goals、test_scheduler、test_mcp、test_connector、test_skill、test_plugin、test_memory、test_runtime、test_hooks、test_worktree、test_validation_agent、test_acp 已有可复用用例，但本轮未重跑，不能写为通过。
6. **正式多租户验收**：真实 Linux 镜像执行沙箱 self-test、邻居租户隔离、无主进程/socket/密钥暴露、资源耗尽限制、runtime 重启中断/恢复、备份恢复/会话撤销、SSE 撤权；渠道/旧 remote 以客户端 gate 状态单独列示，不能悄悄全局开放。

实现决策：应停止在轻量 cloud worker 上逐个复制客户端功能。把它保留为历史轻量会话兼容能力，新的完整 Online 能力统一走原引擎宿主 + 原前端工作流 + 平台身份/计量边界。这是减少行为偏差且真正达到功能对等的路径，但运行隔离、网关、文件语义和浏览器交互都仍需实际开发与验收；本文不表示这些迁移已经完成。


## 本轮已实施的模型接缝与实测（2026-11-15）

原完整引擎已由 `app.cloud.runtime_entry` 在独立用户容器复用，原前端工作台恢复。具体逐项能力的真实验收以 `parity-acceptance.md` 及各实测 JSON 为准，本节不把“原代码可达”视为所有场景都已真实验收。

- 中央 Chat 网关：`cloud/inference.py`，runtime capability Bearer 身份，平台共享/个人私有路由动态模型目录，完整 system/messages/tools/额外参数估算预占，每次模型调用原子并发准入、发送前 durable 标记、原生 usage 结算；文本/推理/function-call/流与非流均支持。DeepSeek 工具流碎片由中央 SDK 组装。错误脱敏、无 SDK 重发，取消/断流不退款在途请求。
- 元数据：`model_metadata.py`、`gateway_provider.py` 让 opaque route id 保留模型名/视觉/工具/推理/上下文/价格，原供应商 vault 迁移不会把网关变成永不激活的 DeferredProvider。管理员可覆盖能力；个人路由只本人可见，不能引用服务端环境 Key。
- 图片：`image_inference.py`、`image_metering.py`、`runtime_image.py` 保留原图片工具权限、PNG 验证、文件事务与操作幂等，通过中央 SiliconFlow 客户端生成；图片独立额度（默认每月100）、独立 used/reserved，不编造 Token。无供应商原生费用回执则价格未知，显式配置单价才估价。真实供应商付费生成尚需对应账号条件，不能用 PNG 协议用例代替付费实测。
- 自有供应商：订阅/Ollama 的原实例保留，由 `tenant_provider_bridge.py` 对 stream 观察并中央预占；账本注明 receipt 来自 tenant runtime。平台共享/个人付费 BYOK 凭据仅留 control plane、加密存储、不能由原 /config 直连绕过中央额度。用户自己通过 Bash/MCP 使用自己的外部 Key 的不可观测调用，不宣称能精确统计。
- Mac 专属节点：`mlx_node_server.py` 独立 TLS 服务，`mlx_nodes.py` 中央每用户专属配置（节点URL和leaf证书均唯一、Token加密且不进入runtime），`runtime_mlx.py` 保留原 status/start/stop/cache/remove/uninstall UI 接口。只允许 operator 白名单与固定推理端口，独立 HOME/HF cache；不接管、扫描或终止非本节点创建的进程，不卸载系统二进制。中央推理 source=mlx-node 直接观测 SDK 原始 receipt，避免 runtime observer 重复计量或被 capability 跳过额度。
- 实际 Mac：本机原先无 rapid-mlx；已在项目私有 `.venv-mlx-node` 安装0.7.26文本运行依赖，独立缓存下载 Qwen2.5-0.5B 4bit，启动TLS节点和真实 Metal 推理。算术2+3返回5，原生prompt30/completion2/total32，中央独立迁移SQLite账本settled，额度归零下一次HTTP429。证据 `mlx-node-real-evidence.json`。这是实际Mac模型+TLS+中央代码实测；生产HTTP与完整工作台配置验收由总表单独记录。
- 回收：RequestRecord 有 API owner/60秒租约，只回收过期 `agent/image/tenant-provider/mlx-node` 请求。未发送释放、已发送标unknown保留预占，不在API启动时全库回收、不自动重发。

最近回归：云端独立锁环境316 passed/1 skipped（SQLite+真实PostgreSQL，skip为缺原MCP依赖的原图片工具整合用例）；原完整runtime依赖环境32 passed/1 skipped（该轮Ollama为5项，之后已增加至7项并包含于316项云回归）（只跳过未配置真实SiliconFlow付费测试）；Mac节点新增13项协议测试包括真实TLS握手、错误pin时Bearer不发送、实际OpenAI SDK工具流与原始usage、独立cache删除边界、foreign port不接管。模型供应商 MockTransport 用例只证明协议行为，真实本地推理证据另列。

中央生产HTTP后续实测：独立账号经管理API绑定Mac节点，真实容器原 `/api/models` 出现Rapid-MLX模型，原 `/api/chat/prompt` 与SSE完成一次原Agent流程，中央source=mlx-node settled 14438 Tokens，证据 `mlx-live-http-evidence.json`、`mlx-live-agent-evidence.json`。0.5B面对完整14k系统/工具上下文把2+3答为4；精简上下文答5。因此此例证明完整链路与计量真实运行，不证明极小模型具备处理完整Agent复杂上下文的质量。正式工作使用已验证的DeepSeek或更强且资源足够的专属节点模型。

Ollama云管理补丁：`runtime_ollama.py` 使用原Ollama API/provider，安装至tenant私有state/bin、模型至私有cache，镜像提供zstd；仅官方HTTPS下载origin、逐跳检查、SHA256来源记录（不是供应商签名）、2GB压缩/10GB解压预算、tar path/link/device检查和Python data过滤、并发/原子stage安装、子进程不继承CLOUD/vendor凭据、仅停止自建进程。7项安全协议用例已通过。真实独立Linux容器在UID10001、只读rootfs/1.5GiB内存下，经原setup接口下载官方ARM64 0.40.16（压缩包1,600,610,073 bytes）并启动，原pull接口下载qwen2.5:0.5b；原provider SDK真实算术返回5，中央tenant-provider账本settled 34 tokens（input32/output2），进程未继承CLOUD/供应商密钥环境。原完整Agent实测SSE step-finish/done、无error，中央tenant-provider结算11,339 tokens；CPU对11,337-token上下文首次prefill超过5分钟发生原服务超时重试，缓存继续后完成。该次运行原SDK隐式重试在同一wrapper内，账本11,339仅是最终成功原生回执，不能证明之前500 attempt的用量完整。现已修复owned provider的真实AsyncOpenAI.max_retries=0，processor外层每attempt单独准入和计量；使用真实SDK+MockTransport500独立复核仅1HTTP/1admit/finish usage=None，1passed。此协议验证没有重跑真实模型，也不回写抹去此前未单独记录的失败attempt；原真实运行证据保留为修复前事实。小模型在完整上下文把2+3答成4，语义质量不合格；精简SDK答5不能替代完整Agent质量。正常任务应使用DeepSeek或更有能力的模型。证据 `ollama-live-evidence.json`。

最后源码复核补充：原Local OpenAI-compatible（LM Studio/vLLM/llama.cpp）部署迁移方式为操作者提供云端可达的HTTPS兼容端点，按个人/共享自定义route接中央计量。原桌面HTTP localhost地址不能原样填到云端：它在云服务中指向租户/服务器，不是浏览器所在用户电脑。此连接条件不等于自动访问用户本机；私有网络由操作者的HTTPS反向代理和自定义认证头暴露受控端点。无公开可达端点时应报告不可达，不能返回虚假模型成功。自定义header/明确无认证路由补齐由独立实现负责。

最终Mac模型扩展补齐：operator-only `--model-repo alias=owner/repository` 可加入原固定目录之外的HF模型，严格校验且不可覆盖bundled/default；网络调用不能写映射、任意repo/URL/路径/CLI参数不能成为启动参数。Node start/inference/cache/remove使用批准映射，同repo多alias不能绕过运行中缓存删除保护。中央经用户固定TLS pin读取节点catalog与实际models求交，在reserve前拒绝未批准/未运行ID。20项Mac定向回归通过（SQLite+真实PG）；其真实节点重载/已有0.5B恢复结果见 `mlx-live-http-evidence.json`。

最终统一镜像实测：`sha256:d2a754e0528386f7e3c6a6ec2a91417f626fb236629a3f38cc6899ff26d5eb86`，专用账号原runtime/restart后Ollama在3.34s自动running，原397MB模型缓存及原models目录保留，未调用setup/start/pull或chat，新增中央ledger=0；原生命周期可按last_model做空prompt加载，不冒称绝无原生控制请求。10项Ollama定向测试含真实create_app默认/持久true/false配置。Mac TLS服务已重载新源码，operator-qwen-smoke映射既有0.5B repo，缓存命中、5.11s真实start ready；原容器工作台model catalog经中央pin/catalog求交实际可见同一Rapid模型。未下载新模型、未增加推理。
