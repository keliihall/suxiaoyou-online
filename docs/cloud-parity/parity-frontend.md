# 苏小有客户端 → Online 前端逐项核验

首次静态核验（迁移前基线）。基于当前本地 `suxiaoyou` 工作树源码；本文件为静态实现核验，没有使用真实模型密钥或调用付费 API。表中“部分”表示已有同类入口，但不满足客户端行为对等；源码存在不代表云端已通过验收。

> 本文逐项表保留首次评估的缺口基线。原完整 UI/每用户引擎/云网关的迁移现已落地，最新“移植状态”和“真实验收状态”请以 [F01–F56 最终验收表](parity-acceptance.md) 为准；不要把下方旧缺口读作改造后的现状。

## 首次结论与对等标准

首次评估时 Online 为独立的轻量聊天工作台，复用了少量 Markdown 和 Provider 能力，没有挂载原客户端 Agent 工作台。缺失内容包括多轮工具执行、Ask/Plan/Auto、执行审批、输入队列与 steer、任务批处理、Goal、完整 Artifact/Office、运行时回退与 Git worktree、插件/Skills/MCP/连接器、自动化、工作区记忆、安全面板等。不能将目前的 Online 定义为完整苏小有客户端的云端版。

客户端的 React 前端本来就包含 Web 分支。最短完整迁移路线是：恢复原完整工作台，保留云登录和管理员控制台，通过同源网关将原 `/api/*` 契约路由到用户自己的完整运行时。不要继续在 `cloud-app.tsx` 里逐个重写客户端功能，也不要把原单用户后端直接开放成所有用户共享的进程。

“对等”应以用户能完成的工作为标准：输入 → Agent/工具真实执行 → 审批与继续 → 可预览/下载的产物 → 中断恢复 → 用户隔离 → 所有模型调用计量。浏览器不能读取未上传的客户端磁盘、启动本机 Finder/Office 或提供 macOS Metal；这些能力必须给出明确 Web 等价路径，不能以隐藏按钮、返回空列表或占位成功算完成。

## 改造前被主动断开的接缝

| 接缝 | 源码证据 | 现状与迁移动作 |
| --- | --- | --- |
| 页面路由 | [middleware.ts](../../frontend/src/middleware.ts) | `NEXT_PUBLIC_CLOUD_MODE=true` 时，所有非 `/cloud` 页面重定向到 `/cloud`。必须恢复 `/c/*`、`/settings`、`/plugins`、`/automations`、`/remote`、`/m/*` 的认证访问。 |
| 主界面布局 | [(main)/layout.tsx](../../frontend/src/app/(main)/layout.tsx) 与 [desktop-main-layout.tsx](../../frontend/src/app/(main)/desktop-main-layout.tsx) | 云模式在服务器布局中直接 redirect；原 Sidebar、Workspace、Activity、Artifact、PlanReview 均未挂载。恢复原布局，并置于云认证边界内。 |
| 应用 Provider | [app/layout.tsx](../../frontend/src/app/layout.tsx)、[app-providers.tsx](../../frontend/src/components/providers/app-providers.tsx) | 云模式完全省略原 Theme/Query/StreamRegistry/i18n/ErrorBoundary/Toaster。原 `useBackendLifecycle()` 在非 Tauri 环境本来就为 ready，可以继续使用；云登录完成前不得启动业务 Query 与 SSE。 |
| API 网关 | [api.ts](../../frontend/src/lib/api.ts)、[constants.ts](../../frontend/src/lib/constants.ts)、[next.config.ts](../../frontend/next.config.ts) | 原 Web 请求为相对 `/api/*`，可用同源网关直接复用契约。现 Next rewrite 将全部 `/api` 指向独立 cloud API，而该 API 只挂载 `/api/cloud`。需要 cloud 管理 API 与完整运行时 API 双通道。 |
| SSE | [constants.ts](../../frontend/src/lib/constants.ts)、[sse.ts](../../frontend/src/lib/sse.ts) | 普通 Web、没有 dev token 时，`API.CHAT.STREAM` 已为同源 `/api/chat/stream/{id}`。应使用 HttpOnly cookie 网关；不要把实例 Bearer 或模型 Key 发到浏览器，也不要注入 `NEXT_PUBLIC_*` token。网关必须流式转发、允许重放、禁缓冲，并处理登出/停用后的中止。 |
| 桌面桥 | [tauri-api.ts](../../frontend/src/lib/tauri-api.ts)、[use-backend-lifecycle.ts](../../frontend/src/hooks/use-backend-lifecycle.ts) | 绝大部分 native 操作已受 `IS_DESKTOP` 限制；不是重写全部 UI 的理由。浏览器应保持 `IS_DESKTOP=false`，不伪造 Tauri。 |
| 移动连接门禁 | [(mobile)/desktop-mobile-layout.tsx](../../frontend/src/app/(mobile)/desktop-mobile-layout.tsx)、[remote-connection.ts](../../frontend/src/lib/remote-connection.ts) | 现移动页面强制桌面隧道 URL/Bearer 存入 localStorage，缺少云 cookie 会话分支。为 cloud session 新增独立分支，保留原远程桌面功能，禁止以假 remoteConfig 绕过登录。 |

## 逐项能力对照

原 UI/API 路径均为仓库相对位置。以下按可验收的用户行为拆项，ID 可直接复用为迁移测试用例编号。

### 会话、Agent 与执行控制

| ID | 客户端能力及原实现证据 | 原 API/事件契约 | 改造前 Online | 迁移与验收要求 |
| --- | --- | --- | --- | --- |
| F01 | 完整工作台、侧栏、多面板：[desktop-main-layout.tsx](../../frontend/src/app/(main)/desktop-main-layout.tsx) | Session/Workspace/Activity/Artifact 各 store | 缺失 | 认证后进入原工作台；宽屏面板和窄屏抽屉均可用，保留管理员入口。 |
| F02 | 会话创建、历史、重命名、删除：[use-sessions.ts](../../frontend/src/hooks/use-sessions.ts)、[session-item.tsx](../../frontend/src/components/layout/session-item.tsx) | `/api/sessions`、`/api/sessions/{id}` | 部分，独立 cloud conversations | 使用原 Session 契约恢复完整菜单行为；旧轻量会话要明确迁移或历史访问。 |
| F03 | 会话搜索、项目/目录分组：[session-list.tsx](../../frontend/src/components/layout/session-list.tsx)、[search-command-dialog.tsx](../../frontend/src/components/layout/search-command-dialog.tsx) | `/api/sessions/search` | 缺失 | 搜索标题/内容、分组、快捷键、分页一致；不得搜到其他用户。 |
| F04 | 消息分页、长会话、轮次索引与大纲：[use-messages.ts](../../frontend/src/hooks/use-messages.ts)、[conversation-outline.tsx](../../frontend/src/components/messages/conversation-outline.tsx) | `/api/messages/{session}`、`/turn-index` | 缺失 | 长会话加载和大纲跳转、滚动定位、更新续接可用。 |
| F05 | 真正的 Agent 多轮推理与工具调用：[use-chat.ts](../../frontend/src/hooks/use-chat.ts)、[session-stream-registry.ts](../../frontend/src/lib/session-stream-registry.ts) | `/api/chat/prompt`、`/stream/{id}` 多类型 SSE | 仅单次模型文本/文档参考 | 原 Agent 循环和全量流事件必须接通；用真实模型完成读、写、命令、产物任务。 |
| F06 | Ask/Plan/Auto 工作模式：[settings-store.ts](../../frontend/src/stores/settings-store.ts)、[chat-form.tsx](../../frontend/src/components/chat/chat-form.tsx) | agent、permission_presets、permission_rules | 缺失 | 模式切换真实影响工具执行；Plan 只读、Ask 等待审批、Auto 在用户私有运行时执行。 |
| F07 | 模型/供应商/推理模式选择与会话恢复：[use-provider-models.ts](../../frontend/src/hooks/use-provider-models.ts)、[use-session-model-restore.ts](../../frontend/src/hooks/use-session-model-restore.ts) | `/api/models`、`/api/config/providers`、PromptRequest | 部分，只有 cloud route 下拉 | 维持 provider_id/model/reasoning 原契约，中央管理员模型配置对 Agent 生效；无密钥值回显。 |
| F08 | 工具实时活动、用时、参数、结果和错误：[activity-panel.tsx](../../frontend/src/components/activity/activity-panel.tsx)、[message-content.tsx](../../frontend/src/components/messages/message-content.tsx) | 原 Tool/Reasoning/Compaction/Subtask SSE part | 缺失 | 完整渲染真实工具事件，不得降级为普通 Markdown 答案。 |
| F09 | 工具执行授权及记住/撤销：[permission-dialog.tsx](../../frontend/src/components/interactive/permission-dialog.tsx)、[permissions-tab.tsx](../../frontend/src/components/settings/permissions-tab.tsx) | `/api/chat/respond`，SavedPermissionRule | 缺失 | 审批后真实继续；拒绝即不执行；规则按用户/工作区/会话限定，切账号不继承。 |
| F10 | Agent 澄清问题、选择题和回复：[question-prompt.tsx](../../frontend/src/components/interactive/question-prompt.tsx) | `/api/chat/respond` question | 缺失 | 提问、回答、幂等重试与刷新后待答恢复一致。 |
| F11 | 方案审核、接受计划转执行：[plan-review-panel.tsx](../../frontend/src/components/plan-review/plan-review-panel.tsx)、[plan-accept-prompt.tsx](../../frontend/src/components/interactive/plan-accept-prompt.tsx) | `/api/chat/respond` plan | 缺失 | 能打开方案文件、接受后真实继续执行、拒绝不误执行。 |
| F12 | 执行中追加队列、steer、改顺序、取消待输入：[use-chat.ts](../../frontend/src/hooks/use-chat.ts)、[session-inputs.ts](../../frontend/src/lib/session-inputs.ts) | `/api/chat/inputs`、`/{session}`、`/{session}/{input}` | 缺失 | 运行期间可入队/steer，顺序与已消费状态可回读，网络重试不重复执行。 |
| F13 | 顺序/并行任务批处理：[chat-form.tsx](../../frontend/src/components/chat/chat-form.tsx)、[types/chat.ts](../../frontend/src/types/chat.ts) | `/api/chat/task-batch` | 缺失 | 多任务可选顺/并行、子任务活动可见，所有调用进入同一用户账本。 |
| F14 | 编辑历史输入并重发：[user-message.tsx](../../frontend/src/components/messages/user-message.tsx)、[use-chat.ts](../../frontend/src/hooks/use-chat.ts) | `/api/chat/edit` | 缺失 | 编辑后会话分支/清理与客户端一致，断线后从权威消息恢复。 |
| F15 | 压缩上下文、容量显示：[context-indicator.tsx](../../frontend/src/components/chat/context-indicator.tsx) | `/api/chat/compact`、`/api/sessions/{id}/compact` | 缺失 | 真实压缩后继续对话；压缩模型调用计量。 |
| F16 | Goal 创建、编辑、暂停/恢复、自主延续、预算：[use-session-goal.ts](../../frontend/src/hooks/use-session-goal.ts)、[goal-card.tsx](../../frontend/src/components/goal/goal-card.tsx) | `/api/chat/goal`、`/api/sessions/{id}/goal/*` | 缺失 | 根据实际 release flag 开放；状态机、revision、预算及后台续跑真实接通，不能仅显示卡片。 |
| F17 | 停止执行、后台多会话、重连与事件回放：[session-stream-registry.ts](../../frontend/src/lib/session-stream-registry.ts)、[sse.ts](../../frontend/src/lib/sse.ts) | `/api/chat/abort`、`/api/chat/active`、SSE last_event_id | 部分，仅 cloud Job | 恢复全 Agent 流语义，路由切换不中断、刷新可重连、停用账号断开流和后续调用。 |
| F18 | 会话 PDF/Markdown 导出：[use-session-export.ts](../../frontend/src/hooks/use-session-export.ts) | `/api/sessions/{id}/export-pdf`、`/export-md` | 缺失 | 真实导出并浏览器下载，中文/工具结果/长文不截断；禁止导出其他用户会话。 |
| F19 | 思考、引用、代码、表格、Mermaid、任务清单与统计：[components/parts](../../frontend/src/components/parts)、[session-stats.tsx](../../frontend/src/components/chat/session-stats.tsx) | 原消息 parts、`/todos`、usage metadata | 部分，基础 Markdown | 原消息组件与 SSE parts 全量复用；业务小字仍展示，辅助说明改代码注释。 |
| F20 | 草稿、幂等发送、快捷键、后台通知：[chat-form.tsx](../../frontend/src/components/chat/chat-form.tsx)、[prompt-idempotency.ts](../../frontend/src/lib/prompt-idempotency.ts)、[background-notify.ts](../../frontend/src/lib/background-notify.ts) | localStorage、Notification、原 request id | 部分，轻量发送幂等 | 按用户隔离草稿与请求 ID；登出清理，通知不暴露前一个账号内容。 |

### 文件、工作区、产物与 Office

| ID | 客户端能力及原实现证据 | 原 API/事件契约 | 改造前 Online | 迁移与验收要求 |
| --- | --- | --- | --- | --- |
| F21 | 多文件选择、粘贴、拖拽、图片附件：[upload.ts](../../frontend/src/lib/upload.ts)、[chat-form.tsx](../../frontend/src/components/chat/chat-form.tsx) | `/api/files/upload`、`/attach`、FileAttachment | 部分，上传参考材料 | 浏览器 File 上传进入用户运行时；视觉模型图片真实输入；客户端绝对路径不可原样解释为服务器宿主路径。 |
| F22 | 项目/工作区目录选择、切换与绑定：[workspace-toggle.tsx](../../frontend/src/components/chat/workspace-toggle.tsx)、[directory-browser.tsx](../../frontend/src/components/mobile/directory-browser.tsx) | `/api/files/browse-directory`、`/list-directory`、session.directory | 缺失，只有逻辑 Workspace | 用云目录选择器、创建目录和目录上传等价替代 native picker；路径根必须用户私有，不允许浏览宿主根目录。 |
| F23 | 文件 @mention、目录搜索与全文索引：[file-mention-popup.tsx](../../frontend/src/components/chat/file-mention-popup.tsx)、[use-index-status.ts](../../frontend/src/hooks/use-index-status.ts) | `/api/files/search`、`/ingest`、`/api/fts/index/*` | 缺失 | 已上传/生成文件可索引和 @引用；索引按用户隔离。 |
| F24 | 工作区文件、工具生成产物列表：[files-section.tsx](../../frontend/src/components/workspace/files-section.tsx)、[file-artifact-card.tsx](../../frontend/src/components/parts/file-artifact-card.tsx) | `/api/sessions/{id}/files`、工具 Artifact parts | 部分，原始上传列表 | Agent 真实生成文件能立即出现、打开、预览、下载，不能仅显示上传原件。 |
| F25 | Read/Write/Edit/Bash/代码执行的交互反馈：[activity-panel.tsx](../../frontend/src/components/activity/activity-panel.tsx)、[tool-metadata.ts](../../frontend/src/lib/tool-metadata.ts) | 原工具调用、权限事件 | 缺失 | 复用完整工具运行时，在用户隔离环境完成实际文件编辑/命令执行。文件编辑能力不能用只读文档抽取替代。 |
| F26 | Artifact 分屏、源码/预览、复制与修复：[artifact-panel-content.tsx](../../frontend/src/components/artifacts/artifact-panel-content.tsx)、[try-fix-button.tsx](../../frontend/src/components/artifacts/try-fix-button.tsx) | Artifact store 与 chat fix 输入 | 缺失 | 恢复分屏、尺寸调整、错误后“修复”回流到原 Agent。 |
| F27 | HTML/React/SVG/Mermaid 可运行预览：[renderers](../../frontend/src/components/artifacts/renderers) | HTML iframe、React sandbox、Mermaid | 缺失 | 复用渲染器并验证内容隔离；React 依赖要可达/本地打包，不能只挂 UI 宣称可运行。 |
| F28 | PDF 文档预览与分页：[pdf-renderer.tsx](../../frontend/src/components/artifacts/renderers/pdf-renderer.tsx) | `/api/files/content-binary` | 缺失 | 二进制内容正确传输、分页/缩放可用；用户只可读本人会话授权文件。 |
| F29 | Word/Excel/PPTX 内容预览：[docx-renderer.tsx](../../frontend/src/components/artifacts/renderers/docx-renderer.tsx)、[xlsx-renderer.tsx](../../frontend/src/components/artifacts/renderers/xlsx-renderer.tsx)、[pptx-renderer.tsx](../../frontend/src/components/artifacts/renderers/pptx-renderer.tsx) | `/api/files/content-binary`、`/pptx-preview` | 仅抽取文本作参考 | 恢复原 Office 预览并安装运行时依赖；生成/编辑后可预览成品。 |
| F30 | Office v2 渲染、校验、页面图：[office-v2-renderer.tsx](../../frontend/src/components/artifacts/renderers/office-v2-renderer.tsx) | `/api/office-v2/context`、`render`、`validation`、`page` | 缺失 | 真实服务端转换与页面验证，revision、workspace binding 一致；失败必须真实错误。 |
| F31 | 用户 Office 模板导入/审批/删除/分页预览：[user-office-template-card.tsx](../../frontend/src/components/workspace/user-office-template-card.tsx) | `/api/office-v2/user-templates/*` | 缺失 | 浏览器上传模板，绑定用户/工作区/版本，审批后生成真实文档。 |
| F32 | 图片、音频、视频、CSV、代码、通用文件预览：[artifact-panel-content.tsx](../../frontend/src/components/artifacts/artifact-panel-content.tsx) | `/api/files/content`、`content-binary` | 缺失 | 逐格式真实 fixture 验证，媒体范围请求/大文件行为另验。 |
| F33 | 下载产物、另存副本、会话/Artifact PDF 导出：[file-artifact-card.tsx](../../frontend/src/components/parts/file-artifact-card.tsx)、[browser-files.ts](../../frontend/src/lib/browser-files.ts)、[markdown-renderer.tsx](../../frontend/src/components/artifacts/renderers/markdown-renderer.tsx) | `/api/files/content-binary`、`/api/artifacts/export-pdf` | 部分，原始上传下载 | 原已有 Web Blob 下载分支，直接复用；增加云目录 ZIP 下载作为目录“取回”。 |
| F34 | 版本历史、恢复文件：[file-version-history-dialog.tsx](../../frontend/src/components/parts/file-version-history-dialog.tsx) | `/api/file-versions`、`/{version}/restore` | 缺失 | 编辑产生版本、比较历史、恢复内容，权限与并发冲突一致。 |
| F35 | 运行时检查点、预览回退与执行回退：[runtime-control-card.tsx](../../frontend/src/components/workspace/runtime-control-card.tsx) | `/api/runtime/context`、`checkpoints`、`rewind/*` | 缺失 | 在隔离工作区生成检查点；回退恢复实际文件，不能只改数据库。 |
| F36 | Git worktree 检查、创建绑定、释放：[runtime-control-card.tsx](../../frontend/src/components/workspace/runtime-control-card.tsx) | `/api/runtime/worktrees/{inspect,create-bind,release}` | 缺失 | 私有云 Git 仓库真实创建与释放；下载/导出变更或提供可审阅 diff，不访问宿主项目。 |
| F37 | 默认应用打开、选择应用打开、Finder/资源管理器定位：[file-artifact-actions.ts](../../frontend/src/lib/file-artifact-actions.ts) | native bridge、`open-system` 等 | 无等价能力 | 浏览器内预览+下载+在云目录定位；对客户端本机应用的调用只能由可选本地桥接器完成，不能在云端弹服务端应用。原 UI 对 Web 已仅保留 preview/saveCopy。 |

### 配置、扩展、自动化、安全与移动端

| ID | 客户端能力及原实现证据 | 原 API/事件契约 | 改造前 Online | 迁移与验收要求 |
| --- | --- | --- | --- | --- |
| F38 | 主题、语言、排版/外观自定义：[general-tab.tsx](../../frontend/src/components/settings/general-tab.tsx)、[appearance-customize.tsx](../../frontend/src/components/settings/appearance-customize.tsx) | Theme/i18n/appearance store | 缺失 | 复用设置；按用户保存并避免账号间覆盖。 |
| F39 | BYOK、多供应商、自定义模型端点：[providers-tab.tsx](../../frontend/src/components/settings/providers-tab.tsx)、[providers](../../frontend/src/components/settings/providers) | `/api/config/providers/*`、`/api/config/custom*` | 部分，管理员模型路由/Key配置 | 复用个人供应商能力，区分管理员共享路由与用户私有 BYOK；所有请求经过租户/用量边界，不能绕开平台账本。 |
| F40 | ChatGPT 订阅登录、手工 callback、登出：[chatgpt-sub-panel.tsx](../../frontend/src/components/settings/providers/chatgpt-sub-panel.tsx) | `/api/config/openai-subscription/*` | 缺失 | 云回调地址与 token 存储按用户隔离；真实授权流程与续期待端到端验收。 |
| F41 | Ollama 安装、启动/停止、模型库、拉取/删除、预热：[ollama-panel.tsx](../../frontend/src/components/settings/ollama-panel.tsx)、[ollama](../../frontend/src/components/settings/ollama) | `/api/ollama/*` | 缺失 | 在用户云运行时或用户授权的自有推理端点提供等价管理；不能从浏览器控制用户未授权的本机 Ollama，也不能让普通用户卸载共享宿主运行时。 |
| F42 | Rapid MLX 模型缓存、启动/停止/卸载：[rapid-mlx-panel.tsx](../../frontend/src/components/settings/rapid-mlx-panel.tsx) | `/api/rapid-mlx/*` | 缺失 | macOS Metal 是运行环境依赖，Linux 云不能冒充支持；可连接专属 Mac 推理节点或本地桥接器，提供真实能力状态。此项在节点未提供前保持未对等。 |
| F43 | 插件列表、详情、启用/禁用：[plugins/content.tsx](../../frontend/src/app/(main)/plugins/content.tsx)、[use-plugins.ts](../../frontend/src/hooks/use-plugins.ts) | `/api/plugins/*` | 缺失 | 完整插件清单和实际工具/资源生效，安装内容限制在用户实例。 |
| F44 | Skills 列表、开关、商店搜索、安装：[use-plugins.ts](../../frontend/src/hooks/use-plugins.ts) | `/api/skills`、`/{name}/enable|disable`、`store/search`、`install` | 缺失 | 真实安装后 Agent 可调用；用户技能独立，不能修改公共只读模板。 |
| F45 | 连接器/MCP 状态、增删、开关、连/断/重连、自定义 token、OAuth：[plugins/content.tsx](../../frontend/src/app/(main)/plugins/content.tsx)、[use-connectors.ts](../../frontend/src/hooks/use-connectors.ts) | `/api/connectors/*`、`/api/mcp/*`、`/api/google/*` | 缺失 | 云回调、浏览器 popup、凭据和 MCP 子进程均为用户私有；连接后真实工具调用，不能以已连接假状态验收。 |
| F46 | 本地 MCP 启动审批：[use-connectors.ts](../../frontend/src/hooks/use-connectors.ts) | `approve-local-startup` | 缺失 | 用户只能批准自己的隔离实例中启动，不能授权服务端宿主任意命令。 |
| F47 | 自动化 CRUD、启停、cron/interval、循环任务、模板：[automations/content.tsx](../../frontend/src/app/(main)/automations/content.tsx)、[automation-dialogs.tsx](../../frontend/src/app/(main)/automations/automation-dialogs.tsx) | `/api/automations*`、`from-template`、`loop-presets` | 缺失 | 后台调度真实运行，浏览器关页仍触发，用户隔离，目录选择为云目录，每次模型调用受平台额度控制。 |
| F48 | 手动运行自动化、运行历史、跳到生成会话：[use-automations.ts](../../frontend/src/hooks/use-automations.ts) | `/{id}/run`、`/{id}/runs` | 缺失 | 真实执行产生会话和产物，重复触发/超时/停用处理一致。 |
| F49 | 工作区记忆查看、编辑、自动/手动刷新、导出、删除：[memory-tab.tsx](../../frontend/src/components/settings/memory-tab.tsx)、[context-section.tsx](../../frontend/src/components/workspace/context-section.tsx) | `/api/workspace-memory*` | 缺失 | 私有记忆真实进入 Agent 提示；刷新也计量，导出能在浏览器获得文件。 |
| F50 | 安全状态、工具开关、紧急停止、审计、项目 hooks 与撤销：[security-tab.tsx](../../frontend/src/components/settings/security-tab.tsx)、[security-api.ts](../../frontend/src/lib/security-api.ts) | `/api/security/*` | 缺失，现仅管理操作审计 | 用户运行时安全面板与平台管理员审计并存；用户只能停止/配置自己运行时。 |
| F51 | 原会话/模型/日期维度用量与成本：[usage-tab.tsx](../../frontend/src/components/settings/usage-tab.tsx)、[use-usage.ts](../../frontend/src/hooks/use-usage.ts) | `/api/usage?days=*` | 部分，cloud中央账本更完整 | 保留原统计体验，与平台实际账本对账；Agent 多轮、压缩、记忆、自动化、子任务均包含，不能双计。 |
| F52 | 消息渠道添加、登录、移除、状态：[use-channels.ts](../../frontend/src/hooks/use-channels.ts) | `/api/channels/*` | 缺失 | 逐渠道真实接入用户运行时，回调路由归属一致；UI hooks已存在，还须由后端清单确认实际发布入口。 |
| F53 | 远程桌面开关、二维码、token轮换：[remote/content.tsx](../../frontend/src/app/(main)/remote/content.tsx) | `/api/remote/*` | 缺失 | 云访问由登录与同源网关实现；原桌面桥接功能另保留，不能发云实例内部Bearer给浏览器。 |
| F54 | 移动任务列表、发起、相机附件、状态/待审批、任务详情：[app/(mobile)](../../frontend/src/app/(mobile)) | 原 Session/Chat/Upload/SSE API | 部分，仅轻量响应式聊天 | 云会话替代隧道连接门禁，恢复完整移动 task页面、执行审批和文件预览；原工作台本身也有响应式布局。 |
| F55 | 手机端模型选择、工作区浏览、网络恢复：[desktop-mobile-layout.tsx](../../frontend/src/app/(mobile)/desktop-mobile-layout.tsx)、[use-remote-health.ts](../../frontend/src/hooks/use-remote-health.ts) | `/api/remote/provider-info`、原 Models/Directory API | 缺失 | cloud 分支通过登录态/用户运行时 capabilities获取供应商，失效回登录，不能依赖 localStorage隧道token。 |
| F56 | 桌面窗控、托盘、native日志与应用重启：[tauri-api.ts](../../frontend/src/lib/tauri-api.ts)、[use-tray-sync.ts](../../frontend/src/hooks/use-tray-sync.ts) | Tauri invoke/listen | 无 Web 等价 | 保留浏览器通知、会话快捷入口、云运行时重连/重启与日志页；窗控由浏览器本身承担，须记录为平台差异，不当业务缺失掩盖。 |

## 浏览器文件能力必须替换的具体位置

1. `upload.ts::browseFiles()`：当前非 Tauri分支请求服务器 native OS dialog，云服务器不应弹窗。Web改为浏览器多选文件并逐个走原 `/api/files/upload`，保持 FileAttachment契约。
2. `upload.ts::browseDirectory()`：不能返回客户端本机绝对路径。实现共用的云目录选择服务；`WorkspaceToggle`、自动化 WorkspacePicker、Onboarding、项目工具栏、记忆目录入口均调用它。现 `MobileDirectoryBrowser` 可复用交互，但其 `/list-directory` 必须受用户运行时根目录限制。
3. `chat-form.tsx` 的粘贴路径/URI、Tauri drag-drop：browser分支仅接受可获得 File的内容；目录上传保留相对路径并验证 `..`/符号链接/超限；原 absolute_path只作实例内标识，不代表宿主路径。
4. `file-artifact-card.tsx`/`file-artifact-actions.ts`：原来已为 Web提供 `preview`、`saveCopy`，可以复用。云增加“在云目录定位”，native“打开应用/在Finder定位”保留客户端分支，不可让网页驱动云宿主桌面。
5. `session-list.tsx`、`session-item.tsx`、`pptx-renderer.tsx` 仍有 open-system调用；确保Web不会出现无效或启动宿主应用的操作。Office预览走服务端转换+浏览器展示，成果通过认证下载。
6. `use-session-export.ts`、`browser-files.ts` 已有 Blob下载，勿另造依赖Tauri的导出通道；大文件逐步用流式下载/带cookie的受控下载URL避免base64放大。
7. `ContextCard` 记忆“导出”现在只返回 exported_to服务端路径；Web应附带下载操作。Worktree产物亦应可浏览、下载或导出Git diff。

## 原 UI 复用时需要补齐的云身份和状态边界

- 保留 `/cloud` 登录及管理员的用户/额度、模型/Key、请求核查、审计页面，抽出 `CloudAuthProvider/CloudAuthBoundary`。普通工作台入口改为 `/c/new`；管理页可以留在 `/cloud` 的独立路由或独立 admin页面，避免登录后仍落回轻量聊天壳。
- Cloud成功登录后，先 `GET /api/cloud/auth/me` 获得身份和 CSRF，再准备用户运行时，待ready才挂载原AppProviders；401/停用/退出时关闭所有SSE、清空QueryClient、清空chat/workspace/activity/artifact/plan store、草稿和待发请求，最后卸载工作台。仅清cookie而保留组件不够。
- `api.ts` 的普通Web请求应显式 `credentials:"same-origin"`；mutation加云CSRF header。`apiFetch`、JSON `request` 两条路径一起处理。`/m/new` 的上传绕过wrapper直接fetch，必须同步处理。下载和Office页图的认证不能遗漏。
- `getRemoteConfig()` 在cloud模式必须忽略旧 `suxiaoyou_remote_connection`，否则旧浏览器配置会将API和SSE发往外部桌面隧道。不要用localStorage存放云认证或实例token。
- `settings-store` (`suxiaoyou-settings`)、chat草稿、`prompt-idempotency`、sidebar/appearance储存目前都是单用户全局。按cloud user id命名空间或服务端偏好存储，remembered permission尤其不能跨用户。module-level `session-stream-registry`、draft mirror也须清空。
- 原QueryKey没有user id，故认证边界须以user id作为React key且重建QueryClient/StreamRegistry；同浏览器A退出再B登录必须做页面测试。
- 中央模型路由必须转换为原 `ProviderInfo/ModelInfo` 契约。客户端原BYOK/ChatGPT/Ollama配置是否由用户自管还是管理员共享要明确；两个来源可以共存，但每次请求须归属同一云用户，不能向前端下发共享真实Key。
- 单用户原后端的权限、紧急停止、插件安装、全局配置必须在用户独立运行时内执行；仅靠浏览器传 user_id不足以隔离。

## 最近“辅助说明不显示”约束的迁移影响

先前去说明文字只覆盖 `app/cloud/*`。原工作台有大量 `*Desc`、`*Hint`、安全/插件/自动化说明段落、onboarding文字。复用后若原样挂载，会重新展示这些内容。迁移时逐组件把辅助说明移为JSX/TS注释，不能用 `text-xs` 或 `small`CSS选择器全局隐藏，因为这些类同样用于Token数、文件大小、工具结果、时间、状态和字段名。

优先检查：`general-tab.tsx` 外观状态/关于说明、`permissions-tab.tsx`说明、`security-tab.tsx`大量说明段落、`plugins/content.tsx`插件介绍/权限提示、`automation-dialogs.tsx` loopHint、`landing.tsx`setupProviderDesc、移动页的mobileTasksRunOnDesktop、Onboarding。字段名称、操作标签、真实错误、用户待确认的具体命令/文件/外发内容、运行状态和业务数据必须保留；工具执行确认不是产品辅助文案。

## 最短实施顺序与交付门槛

1. 先打通完整隔离运行时和云同源网关，使用既有cookie/CSRF/角色/账本；保留原 `/api/*` 形状，避免给几十种页面造新兼容接口。
2. 恢复原AppProviders+`(main)`布局和会话页，增加auth boundary/account menu/admin入口；原API/SSE/所有原始fetch纳入云认证，旧remote配置失效。
3. 替换文件/目录选择、下载、导出和运行时路径边界，确保两个用户真实执行工具时目录/产物/配置完全隔离。
4. 完整恢复Goal/TaskBatch/queue/steer/审批/Plan/Artifact/Office/runtime，复用原工具后端；把每项从“源码复用”推进为“真实端到端测试通过”。
5. 恢复插件/Skills/MCP/连接器/记忆/自动化/安全与原设置体验；配置支持与用户权限边界一起做，不通过空响应让界面假绿。
6. 补移动云会话，清除辅助说明文案，逐项验收F01–F56。现有原UI测试目录已经覆盖Goal、队列、Office、runtime、安全hooks、模板、大会话等场景，可保留原fixture用作UI回归，但还必须新增真实运行时测试，fixture通过不等于移植完成。

前端验收至少包含：两个账号交替登录、宽屏/手机、真实模型多轮工具执行/审批/停止/重连、文件上传/编辑/版本回退/预览/下载、Office真实成品、Goal与自动化后台执行、连接器实际调用、中央账本核对、普通用户不能访问管理页。平台依赖项（Mac推理节点、特定OAuth/渠道凭据、用户本机应用桥接）在未有实际环境前应明确标为未验收，不得算全量对等。
