# 苏小有 Online：完整能力移植与验收状态

本表延续[首次前端审计](parity-frontend.md) F01–F56 编号，按本轮工作区代码和已取得的证据更新。**“已接入原实现”表示业务实现已迁到私有完整引擎并恢复原 UI；“实际验收”另列。二者不等同，未执行的真实场景仍待验。** 本轮目标是可真实使用、具备完整功能测试条件，不能把表中所有行宣称为全部验收通过。

架构已由轻量聊天壳改为：原完整工作台 + 云身份/CSRF + 每用户独立完整引擎/命名卷/网络 + 同源 API/SSE 代理 + 中央模型/图片额度。旧轻量历史数据保留在 cloud 表，不把旧 Job 伪造为原 Agent Session。

## 已取得的验证证据

| 代号 | 证据 | 可证明范围 |
| --- | --- | --- |
| U | `frontend/tests/ui/cloud-full-workbench.spec.ts`、`cloud-workspace.spec.ts`，19项浏览器受控API回归通过（含记忆下载及共享/个人请求头认证） | 原工作台云登录启动、custom_cloud 模型选择、workspace 保留、cookie/CSRF、401 卸载、移动免桌面配对、runtime 操作、后台/私有Key、只写请求头保留/替换/清除、显式无需认证、图片数量/价格/核对、Mac只写配置。fixture 不代表真实模型/节点成功。 |
| Q | 后端云回归358 passed、4 skipped；完整runtime补验12 passed（覆盖4 skip，含重叠，不求和） | 身份/额度/网关/镜像/隔离等自动回归；外部依赖或付费真实场景按各公开证据另列。 |
| S | `frontend/tests/unit/cloud-session-storage.test.ts` 3 项；原幂等/输入队列单元回归 | 用户 localStorage 命名空间、匿名不读旧桌面配置、CSRF 仅内存；退出销毁流/缓存由完整导航完成。 |
| P | 最新 `.next-parity-delivery` 生产构建通过（含记忆下载/账户401清理、Office重验、共享/个人请求头与个人chat连接测试），API rewrite 8018；TypeScript/定向 ESLint 通过 | 云模式可编译为真实 Next production 服务；后续生产包须使用与验收匹配的 dist。 |
| I | [最终交付环境](parity-delivery-validation.json)、[实际镜像路由清单](parity-route-inventory.json) | schema0007、3个immutable镜像、最终生产dist/浏览器/测试结果；共享当前原routers179/Online183、missing0仅证明路由结构存在，不替代原aef基线逐行为验收。 |
| R | 真实 Web3018 → API8018 → 两个私有 Docker engine → DeepSeek；操作者保留的 `last-agent-parity-smoke.json` | 已真实 write/read/present_file、同虚拟路径不同账号文件字节、跨账号 file/session 404；原模型 metadata 返回 custom_cloud。不是仅 GET 接口或 fixture。 |
| B | [真实运行时验证](parity-runtime-validation.json) | 原 bubblewrap 中 Python/pandas/NumPy/Matplotlib、PANDAS_SUM6、文件结果正确、禁止网络、隐藏私有 state/HOME、清洗模型环境、收拢 setsid 子孙。 |
| O | [真实隔离 Office 转换](parity-office-validation.json) | 实际 Docker 一发转换容器；DOCX/XLSX/PPTX 均生成 PDF/PNG，经 HTTP Ed25519 签名和 engine provider 复验。真实DeepSeek三格式创作/签名模板与用户DOCX模板批准后实例化主链已通过；其他导入格式、浏览器交互及CAS/并发组合另验。 |
| A | [真实 ACP 验证](parity-acp-validation.json)、`scripts/cloud-acp.py` | WebSocket initialize v1 与私有 session/new；stdio 桥接复用本人引擎，不启动第二 SQLite owner。 |
| K | `backend/tests/test_cloud/test_backup.py` 13 项、[真实 Compose 恢复](parity-deployment-validation.json) | 完整停机备份→空目标恢复→旧 session 撤销→私有文件与加密模型 Key 恢复→新 DeepSeek Agent 任务 settled；目录0700/密钥0600及仅精确恢复原容器已实测。 |
| C | [真实持久能力验证](parity-persistent-validation.json)、`scripts/cloud-persistent-smoke.py` | 独立新账号、真实 DeepSeek：Goal 暂停/恢复/完成、真实 npm filesystem MCP 工具、插件/skill 开关、记忆刷新与鉴权下载；manual+interval无浏览器实际独立触发及history对账。预算与失败证据保留；最终镜像双Agent及父子持久ledger实际通过。 |
| V | [真实浏览器协议](parity-browser-validation.json) | 实际审批允许一次与写读、鉴权下载、Question 刷新后选项、Plan 接受前无文件/接受后正确字节、移动历史、真实 FTS/mention 搜索。 |
| T | [真实运行时控制](parity-runtime-control-validation.json) | 实际hook指纹审批与marker、Git worktree bind/inspect/release/GC、真实write/read后checkpoint预览/rewind删除产物、紧停423及恢复后read；12笔112145 Tokens settled，原100000验收预算超出记录保留。 |
| M | [真实 Mac 原 Agent](mlx-live-agent-evidence.json)、[真实 Mac 节点与额度](mlx-node-real-evidence.json)、[真实 Ollama 安装](ollama-live-evidence.json) | 专属 HTTPS Mac/Qwen0.5B 经完整 Docker 原 Agent 实际生成与中央 settled；0额度拒绝另测。Ollama实际安装/下载Qwen0.5B并完整原Agent生成与中央11339 Tokens settled；小模型回答错误、CPU首次预填充缓慢如实保留。 |

持久能力专用账号截至本次对账累计实际 **230,342 Tokens，30笔 settled、0 reserved**（含失败排查与自动memory）；内部开发实测上限300,000，月额度500,000用于原工具schema的保守预占，两者独立。错误空permission_snapshot验收协议的两次真实失败消耗92,752，已保留并改为原UI `/chat/goal` 协议；不隐去消耗，不重复已知错误路径。

真实浏览器已确认 `/c/new` 显示“DeepSeek 办公助手”、输入可用、默认 `/tenant/workspaces` 保留。早期 custom_cloud 过滤导致“无模型”和新对话清空工作区的实际问题均已修复并纳入 U。

## F01–F56 逐项结果

“待”均有明确场景，测试不需要重写功能。外部账号/模型/节点未准备时保持待验，不能隐藏按钮或返回空成功。

| ID | 客户端能力 | Online 移植状态 | 实际验收状态及下一场景 |
| --- | --- | --- | --- |
| F01 | 完整工作台/侧栏/多面板 | 已恢复原 `(main)` 布局与 AppProviders，登录后进入 `/c/new`，保留后台入口 | 真实浏览器通过；宽屏/移动基础 U；全部面板组合待逐项。 |
| F02 | 会话创建/历史/改名/删除 | 已接原 Session API；每用户引擎独立 DB | R 已创建真实会话；改名/归档/删除与旧 cloud 历史迁移另验。 |
| F03 | 搜索/项目分组/快捷键 | 原 Session 搜索/分组 UI 与 API 直接复用 | 待真实长历史搜索与跨用户负向场景。 |
| F04 | 消息分页/大纲/轮次定位 | 原消息/turn-index 与滚动逻辑复用 | 待真实长对话分页/大纲；原 fixture 仅作 UI 回归。 |
| F05 | 多轮 Agent 与工具执行 | 原 SessionPrompt/Processor/ToolRegistry，中央 GatewayProvider | R 真实 write→read→present_file 成功并计量；更长组合继续验。 |
| F06 | Ask/Plan/Auto | 原工作模式/权限预设复用 | V 真实Ask允许一次与Plan审核接受Auto后执行通过，接受前无产物、接受后正确文件；拒绝/撤销与其他模式组合另验。 |
| F07 | 模型/供应商/推理/恢复 | 云中央模型映射 custom_cloud→BYOK，capabilities 原契约 | 真模型列表/可发送已过；U 验 payload model/provider/workspace；推理/恢复待。 |
| F08 | 工具事件/活动/错误 | 原 SSE 全事件与 Activity/parts 复用 | R 工具事件实际产生；各类型展开/错误显示待真实浏览器逐测。 |
| F09 | 执行审批/记住/撤销 | 原 respond/权限 UI；浏览器权限存储按 user 隔离 | V 真实浏览器显示审批、允许一次后写读与文件字节通过；拒绝/撤销与记住的完整组合待验。 |
| F10 | 澄清问题/选择题 | 原 Question 与 respond 状态机复用 | V 真实 Question 显示、刷新保留、选择B并验证回答；重复提交/全部题型另验。 |
| F11 | 方案审核/接受转执行 | 原 PlanReview/SubmitPlan 与 respond 复用 | V 真实 PlanReview、接受前产物不存在、接受Auto后文件字节正确；拒绝与断线组合另验。 |
| F12 | 输入排队/steer/排序/取消 | 原 session_inputs，草稿/幂等 ID 按 user 存储 | 原队列单元回归通过；真实执行中队列/steer 待验。 |
| F13 | 串行/并行子任务 | 原TaskTool/task-batch/子会话复用，调用统一计量；父checkpoint准入及project继承修复 | C 最终c18f真实parallel双Agent各read正确sentinel并持久化；1父+2子TurnRun全部completed且parent/root/workspace一致、均有finish时间，4笔14328Tokens settled。首次失败原证据保留；63项真实SQLite回归验证串行/并行/取消/project/TaskTool边界，final d2a重启后两子会话各1条completed read part与3个completed TurnRun保留；容器真实TaskTool与取消组合另验。 |
| F14 | 历史编辑重发 | 原 chat/edit 与消息逻辑复用 | 待真实历史分支/清理/断线恢复。 |
| F15 | Compaction/上下文 | 原压缩逻辑 Provider 统一网关 | 待长对话真实压缩并核中央账本。 |
| F16 | Goal/预算/后台续跑 | 原 Goal 状态机/DB/控制器复用；显式恢复保留 Token/Cost/Time 与 revision gate，自动续跑次数只约束自动续跑 | C 真实 original chat/goal→安全暂停→同会话恢复→read/get_goal/update_goal完成，active→paused→active→complete；额度不足调用前拒绝有负向证据。修复 max_continuations=0 错误阻止手动恢复；4项预算回归通过。final d2a重启后原Goal仍complete；跨断线/所有阻塞组合另验。 |
| F17 | 停止/后台多会话/SSE | 原无缓冲 SSE，proxy 会话撤权，退出 abort 流 | R 实际 SSE；U 401 卸载；真实断线/重连/取消/SSE 撤权仍待。 |
| F18 | 会话 Markdown/PDF 导出 | 原导出 API 与浏览器 Blob 下载复用 | O 实际会话导出Markdown12070字节、PDF82426字节/6页，记录SHA256；中文长对话、全部工具排版与浏览器导出交互专项另验。 |
| F19 | 思考/引用/代码/Mermaid/Todo/统计 | 原 parts/renderers/stats 直接复用 | 待每种真实事件/输出；辅助说明已移源码注释，业务数据保留。 |
| F20 | 草稿/快捷键/幂等/通知 | 用户命名空间 userStorage，登出清内存/流/Query | S 与 U 通过指定隔离场景；真实通知/重复网络发送待。 |
| F21 | 多文件/拖拽/粘贴/图片附件 | 云浏览器 File 上传到本人卷，保留 FileAttachment | R 普通文件上传通过；目录层级/粘贴/视觉供应商附件待验。 |
| F22 | 工作区选择/切换/目录 | 共用 CloudDirectoryProvider，安全列表/新建，webkitdirectory 上传 | 真实 directories/create 200；R 同路径隔离；完整浏览器目录上传/切换待。 |
| F23 | @引用/索引/全文搜索 | 原 FTS/ingest/mention 复用，索引在本人卷 | V 真实 FTS ingest 与 mention 文件名搜索通过；长文件/增量删除更新另验。 |
| F24 | 工作区/Agent 产物列表 | 原 session files 与 Artifact parts | R present_file + 文件字节通过；实时列表刷新浏览器待。 |
| F25 | Read/Write/Edit/Bash/Python | 完整原工具，私有 runtime 与原内部 bwrap | R read/write；B 真实 Python/科学库/文件/沙箱通过；edit/version 组合待。 |
| F26 | Artifact 分屏/源码/修复 | 原 Artifact store/panel/TryFix 复用 | 待真实成品分屏/错误→修复回流。 |
| F27 | HTML/React/SVG/Mermaid 运行预览 | 原浏览器 renderer/iframe 复用 | 待真实内容隔离、React 依赖可达与交互。 |
| F28 | PDF 预览/分页 | 原认证二进制 API + PDF.js worker 生产资源 | P 资源打包；O 真实 PDF 成品；浏览器分页/缩放待。 |
| F29 | Word/Excel/PPTX 预览 | 原 renderer 与完整 runtime 工具/依赖，签名 Office 转换 | O 已真实 DeepSeek 创建 DOCX 表格/页眉页脚、XLSX 公式/原生图/条件格式/数据验证、PPTX 原生图/表格/备注；均读回 OOXML、authoritative_pass 与 PNG，PPTX 页面已目视确认。浏览器各格式缩放/页导航另验。 |
| F30 | Office v2 渲染/校验/页面 | 无网一发任务/签名响应/引擎复验、precommit 原协议 | O 三格式真实 Agent precommit 通过；DOCX 编辑生成版本，旧版恢复使预览 stale，恢复最新字节成功；跨用户 context/validation 均404。162项原 Office 与安全回归通过；各并发/取消组合另验。 |
| F31 | Office 用户模板 | 原导入/审批/实例化/预览接口与每用户持久卷 | O 三格式签名第一方模板实际创建；用户 DOCX 模板导入→未确认禁止实例化→PNG→批准→真实 DeepSeek 实例化→精确占位替换通过；跨用户模板页404。模板阶段21调用/153204Token均settled；连续两次镜像升级rev1→2→3重新校验/PNG/显式批准、旧证据保留及CAS409已实际通过，升级0新增模型调用；真实跨镜像旧rev1→重新校验rev2→PNG→显式批准恢复可用，旧来源/批准/证据保留，旧CAS重放409且0模型调用。其他导入格式/并发与取消负向另验。 |
| F32 | 图片/音视频/CSV/代码预览 | 原 renderer/content APIs 原样复用 | B 输出文件与 O 页面图实际存在；逐格式/Range/大文件待。 |
| F33 | 文件/Artifact 下载与导出 | 原浏览器认证 Blob 下载；导出文件保留真实内容 | V 原浏览器真实观察 download 与正确文件名；C 记忆文件认证下载逐字节比对通过；中文导出与大文件待。目录打包不是原客户端已有单文件动作，若提供应单列。 |
| F34 | 文件版本/恢复 | 原 file-versions 在本人引擎 DB/卷 | O 真实 DOCX 编辑→新版本→恢复旧版使预览stale→恢复最新字节通过；通用代码文件/冲突另验。 |
| F35 | 检查点/rewind | 原 runtime/checkpoints/rewind 与事务目录 | T 真实模型write/read→finalized/pinned checkpoint→preview可执行且无冲突→rewind删除产物并清4条消息，文件确认不存在；外部副作用不回退，冲突/多轮组合另验。 |
| F36 | Git worktree | 原 Git/worktree runtime，镜像含git，每用户项目 | T 实际git seed commit→原API create/bind→inspect可用/clean→release/GC complete→removed且非registered通过；脏工作树/diff冲突与重绑另验。 |
| F37 | 本机应用打开/目录定位 | Web 原分支只显示预览/下载；云目录选择替换服务端 OS dialog | 已核 IS_DESKTOP 门禁；Web 等价路径已接。客户端本机应用需用户自己的桥，网页不伪启动服务端 Finder。 |
| F38 | 语言/主题/外观 | 原设置复用，theme/language/appearance 按 user 键 | S 与 U 指定语言/存储通过；跨账号全部外观项待。 |
| F39 | BYOK/自定义供应商 | 管理员共享+个人私有路由/只写Key与加密自定义请求头；custom_*可显式无需认证，个人chat可连接测试，原面板指向云配置 | U 私有BYOK/图片/Key、headers-only创建、请求头保留/替换/清除、失败即清输入、不持久化与明确测试提交通过；真DeepSeek shared已过，其他真实端点待。 |
| F40 | ChatGPT subscription | 原面板保留，中央授权凭据/计量，手工 callback | 适配已接；真实外部订阅授权/续期/注销待账号验收。 |
| F41 | Ollama 安装/模型/启停 | 保留原面板和本人runtime原API，真实SSE由中央网关计量；不操作共享宿主 | M 真实私有Docker安装Ollama0.40.16、拉取Qwen2.5:0.5b；SDK实际34Tokens及完整原Agent SSE11339Tokens均settled；最终d2a镜像重启3.34秒自动running、模型缓存保留，0新增推理账本；原自动启动默认值及显式关闭均保留。SDK隐式重试已禁用，修复前未知attempt按证据保留。0.5B原Agent算术回答错误、CPU首轮预填充慢已保留；删除/重装与不同模型组合另验。 |
| F42 | Rapid MLX 模型管理 | 原面板保留；专属 HTTPS Mac 节点服务/中央代理，管理员节点绑定/证书 pin | M 已真实安装启动 Qwen2.5-0.5B，完整原 Docker Agent→Mac模型生成“4”、14438 Tokens settled；独立直接节点生成“5”32 Tokens、0额度429。U 配置只写通过；operator批准的新HF repo映射已补，真实新alias启动已缓存模型5.11秒就绪，中央catalog与实际models求交后可发现；租户任意repo在计量前拒绝。全模型下载/缓存/删除组合另验。 |
| F43 | 插件列表/启停/详情 | 原 bundled plugin 资源/注册器/页面复用 | C 私有 HOME 实装真实测试插件，详情读取、启用增加命名空间skill、停用移除skill再启用通过；插件具体外部工具逐个另验。 |
| F44 | Skills 列表/查看/安装/启停 | 原 skill registry，用户 HOME/project 独立持久 | C 私有HOME实装SKILL.md、读取正文、独立disable/enable状态与插件注册联动通过，跨本人引擎重启仍可读；模型主动skill选择与其他安装源另验。 |
| F45 | Connector/MCP/Google OAuth | 原实现 + cloud trusted-origin callback/私有凭据分流 | C 真实 npm filesystem2.3.0 stdio 连接、16工具发现、DeepSeek tool_search→read_text_file读正确sentinel通过；Google/其他外部OAuth另验。 |
| F46 | 本地 MCP 启动审批 | 原 approve-local-startup，在本人容器中运行 | C 未显式confirmed拒绝、旧fingerprint拒绝、核对Node argv/cwd/HOME后精确批准，模型工具另需一次交互审批且实际成功；限定新验收目录。 |
| F47 | 自动化创建/启停/cron/模板 | 原 scheduler/lifespan/DB 常驻本人 runtime；API同事务同步next_run_at | C 真实创建1分钟interval，manual后独立schedule于下一分钟触发，无浏览器/SSE；history与持久消息正确，验后停用。修复提交前另连接不可见导致next_run_at丢失，创建/模板/启停事务回归通过；cron时区/并发防重另验。 |
| F48 | 自动化手动运行/历史/跳转 | 原 automation run/runs/session UI | C 手动及定时两次history均success，各session持久消息包含正确私有sentinel；真实中央回执settled，任务已disabled。浏览器历史跳转与超时/失败展示另验。 |
| F49 | 工作区记忆注入/编辑/刷新/导出 | 原 memory queue/DB/provider；Web 导出追加认证文件下载 | C 原后台memory队列真实模型刷新、中央新增settled回执、导出→认证二进制下载→与当前正文逐字节及SHA256一致；浏览器下载另有U回归；所有注入场景另验。 |
| F50 | 安全状态/工具开关/紧停/hooks | 原用户runtime安全面板，平台管理审计并存 | B 沙箱边界通过；T 项目hook实际指纹审批并执行marker，紧停后新prompt423、恢复后真实read成功；hook撤销/全部工具开关组合另验。 |
| F51 | 会话/模型/日期用量和成本 | 原 usage UI + 中央 Token/图片账本；unknown 与快照保留 | 真 DeepSeek settled 已取得；U 图片计数/价格/核对；后台调用均已计量；当前中央回执source统为agent，未伪称已分memory/scheduler来源，完整分源/成本对账另验。 |
| F52 | 消息渠道 | 原代码与发布 gate 保留在完整引擎 | 原客户端 CHANNELS_RELEASED=false，不误报 Online 删除；开放须真实渠道凭据另验，当前未发布。 |
| F53 | 远程访问/二维码/token | Cloud `/remote` 改云地址及本人 runtime start/restart/stop；desktop 原分支保留 | U 启停/CSRF/不启 tunnel 通过；原客户端 REMOTE_ACCESS_RELEASED=false，云直接登录是等价访问。 |
| F54 | 移动任务/发起/详情/待审批 | `(mobile)` CloudSessionBoundary，保留原完整 task UI | U 云 cookie/免 pairing/窄屏通过；V 390×844真实移动历史与task详情通过；相机附件/预览另验。 |
| F55 | 移动模型/目录/网络恢复 | cloud 同源模型/私有目录，忽略旧 desktop tunnel localStorage | U 旧 token 不转发、模型入口与401通过；真实断网/目录交互待。 |
| F56 | 窗控/托盘/日志/重启 | Web 使用浏览器窗控/通知/入口，云 runtime 控制；不伪造 Tauri | U 启停通过；C 本人runtime已重启至final d2a，Goal/父子消息/3个TurnRun/文件SHA256/插件与skill状态实际保留，自动化仍disabled，0新增模型调用；日志交互与通知待。 |

## 不能合并为“已验收”的环境项目

1. Goal、MCP、Question、Plan、审批已有上述真实主链证据；子Agent真实TaskTool/取消等组合、自动化cron/并发防重、所有权限状态组合及跨断线恢复仍逐项验。
2. 三格式 Agent Office 创作和用户 DOCX 模板主链已实际通过；全部导入格式、浏览器交互、并发/取消仍逐项验；跨镜像重新校验与旧CAS拒绝已实际通过。
3. 实际 ChatGPT/Google/其他 MCP OAuth、SiliconFlow图片供应商；Ollama与Mac节点已有真实原Agent推理证据，所有模型管理组合仍另验。
4. 完整 Compose backup→空目标 restore→旧session撤销→私有文件/加密Key恢复→新DeepSeek Agent任务已真实通过；大卷、不同宿主迁移与故障中断等生产恢复组合另验。
5. 公开生产所需 HTTPS、依赖/镜像/租户对抗、容量、OIDC/组织/RLS/S3 是平台生产验收，不冒充客户端已发布功能缺失，也不默认宣称已完成。

启动及恢复操作见[完整云端测试说明](../cloud-testing.md)。后续取得新证据时，逐行填写具体会话/产物/账本/测试记录并缩小“待”范围；不要通过把“已接入”改成“通过”来补空白。
