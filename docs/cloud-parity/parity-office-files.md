# 苏小有客户端与 Online：Office、文件和制品能力逐项核验

核验日期：2026-11-15。源码范围为本地 `suxiaoyou/` 的客户端 v1.1.1 和当前 `feat/cloud-web` 实现；本文件为只读代码核验，不代表已执行所有客户端流程。未读取真实模型密钥，未调用付费模型。

> 本文保留改造前的静态差异基线。完整原工作台、每用户引擎、Office签名转换/模板与真实模型主链现已落地；当前移植与实测结论以[F01–F56验收表](parity-acceptance.md)及[Office真实证据](parity-office-validation.json)为准，不能把下文旧“当前缺失”作为改造后状态。

## 结论

当前 Online 的文件能力只有按用户上传、列出、下载、删除，以及把文件的有界文本提取加入模型消息。它没有接入客户端的 Agent 文件工具、Office 写入事务、制品预览、版本恢复、模板和会话导出。因此“能上传文档并聊天”不能验收为客户端功能一致。

客户端的 Office v1.1、检查点、回退及用户 Office 模板 Beta 在当前源码中的发行门禁均已开启。Office 写入另有严格的运行条件：权威渲染器、冻结发行身份、原生沙箱行为验证、验证服务和提交前协调器必须完整就绪。不能把未就绪运行时说成未发行，也不能为了云端迁移把这些判断改成恒真。

推荐让每个用户使用隔离的完整客户端后端运行时和原有前端能力，通过云身份网关访问；继续使用当前独立的单轮 Cloud Worker，逐项重写同样功能会形成第二套不完整产品。需要同时解决路径、进程权限、文件持久化和模型用量桥接。

## 核验口径

- “客户端开放”表示当前源代码发行门禁与 API/UI 入口存在，不承诺这台源码环境已具备原版打包运行时。
- “部分”表示当前 Online 具有相关基础功能，但缺少客户端可观察行为，不能勾选功能一致。
- 预览不等于可视化文档编辑。客户端本身主要由 Agent 调用声明式工具生成和修改 Office，前端负责预览和下载；没有证据表明客户端提供 Word/Excel/PowerPoint 级所见即所得编辑器。
- 原生选择器、Finder/资源管理器、系统默认应用属于本地宿主集成。浏览器版必须提供上传、下载、虚拟工作区导航等对应行为，不能在云服务器上调用系统打开/显示来假装控制用户电脑。

## 能力矩阵

下表中路径均相对于 `suxiaoyou/`，行号对应本轮核验时源码。

| 能力 | 客户端实际接口和前端证据 | 当前 Online | 必须移植及验收 |
|---|---|---|---|
| 多文件上传、拖放和 SHA-256 去重 | `backend/app/api/files.py:1372` `POST /api/files/upload`；`frontend/src/lib/upload.ts:20` | 部分：`POST /api/cloud/files` 有界上传，不是客户端附件/工具流 | 上传后成为真实 Agent 可读的工作区附件；同名、重复、Unicode 文件正确；中断上传不产生可见半文件；跨用户不可读 |
| 文件及文件夹附件、目录选择 | `files.py:1217` `/browse-directory`、`:1280` `/browse`、`:1316` `/attach`；`upload.ts:46` Tauri 选择器 | 文件上传；无目录附件和目录上传 | 浏览器目录上传或 ZIP 导入保留相对层级，拒绝穿越/符号链接/特殊文件；云端工作区选择器不暴露主机目录 |
| 管理型工作区与附件快照 | `backend/app/session/managed_workspace.py:44` 每会话 `inputs/`、`suxiaoyou_written/`、`.tmp/`；`snapshot_attachments()` 原子复制 | Cloud Workspace 只有元数据和独立对象，没有客户端工作区树 | 每用户、工作区、会话身份有固定映射；附件快照不可意外覆盖原文件；生成文件持续保存、会话重开可见 |
| 文件搜索、@ 引用、目录浏览和即时索引 | `files.py:1245` `/list-directory`、`:1339` `/ingest`、`:1523` `/search`；`upload.ts:155` `searchFiles()` | 缺失 | 在当前工作区按名称搜索、选择相对路径、目录树、检索结果跳转；索引隔离、更新和删除同步；不得向客户端返回其他用户命中 |
| Agent 读取文本、目录、PDF/DOCX/XLSX/PPTX/CSV/TSV/图像 | `backend/app/tool/builtin/read.py:30` `ReadTool`；`tool/extractors.py:13` | 部分：上传材料整体有界提取；无 `read` 工具、目录读取和图片多模态输入 | 真实模型按需调用工具；分页/offset/limit、PDF 页/PPTX 幻灯片/XLSX sheet 选择、结构化 JSON 数据读取均可；图片内容不是仅文件名 |
| 大文件按需阅读与数据 schema/sample | `read.py:46` 参数，CSV/XLSX 有行数、列、样本和分页元数据 | 缺失 | 不把所有附件一次性截断塞入提示词；能够继续读取后续页/行并准确报告范围；限额覆盖多轮调用 |
| 文本文件写入、编辑、补丁和生成文件发现 | `backend/app/tool/builtin/{write,edit,apply_patch}.py`；`api/sessions.py:350` 会话文件收集 | 缺失 | 工具修改真实工作区并更新文件列表；原子提交、变更版本和检查点保持；文本/代码下载字节一致 |
| Office DOCX 基础生成与编辑 | `backend/app/tool/builtin/office.py:2079`；`OfficeTool` 支持段落/表格/分页/本地图片/追加/精确替换 | 缺失 | 真实模型可创建和修改 `.docx`；重新打开验证；不满足唯一匹配等条件不能错误写入；附件及输出可预览下载 |
| Office XLSX 基础生成与编辑 | 同工具：sheet/rows/cells/styles/delete_sheets | 缺失 | 创建/追加/修改单元格/删工作表/样式保真；公式按客户端合同写入但不宣称已重算；结构/数据验证后原子安装 |
| Office PPTX 基础生成与编辑 | 同工具：标题/列表页、文本框、表格、本地图片、追加、精确替换 | 缺失 | 真实模型创建/修改多页演示稿，预览/下载可见；失败和取消不提交半成品 |
| Office v1.1 高级 DOCX | `office.py:2098` 以及 `_extend_office_v2_schema()`：页/节、文本表格格式、静态图表图像及替代/来源信息 | 缺失 | 移植完整原工具 schema 与验证规则，不只基础标题段落；样例验证布局、表格、图像和图表元数据 |
| Office v1.1 高级 XLSX | 同扩展：合并、行高列宽、冻结窗格、筛选、条件格式、数据验证、命名区域、bar/line/pie/scatter 原生图表 | 缺失 | 原工具允许的每类声明式操作有样例；保留公式/数据校验/图表数据；无法表达或不支持的操作明确失败 |
| Office v1.1 高级 PPTX | 同扩展：布局选择、主题相关结构、演讲者备注、原生图表 | 缺失 | 布局、备注、原生图表和数据符合原合同；编辑已有稿件保留非目标内容 |
| Office 事务、预提交视觉校验和自动修复 | `office.py:2426`；`tool/workspace_transaction.py`；`office_validation/{draft,precommit,startup}.py`；`session/processor` Office repairer | 缺失 | 同一事务 staging→结构/视觉校验→修复→重新校验→提交；取消/超时/失败不改可见文件；审计绑定 session/workspace/checkpoint/source digest |
| DOCX/XLSX/PPTX 权威预览与验证状态 | `api/office_v2.py:148` `/context`、`:169` `/render`、`:193` `/validation`、`:217` `/page`；`office-v2-renderer.tsx:113` | 缺失 | 页级 PNG 预览、翻页、刷新、陈旧检测和校验状态；源改变后旧 cache_key 不可继续充当新版本；所有资源需当前用户授权 |
| Office 近似预览降级 | `file-preview-renderer.tsx:37` 包装 OfficeV2，并提供 `DocxRenderer`/`XlsxRenderer`/`PptxRenderer`；`office-v2-renderer.tsx:242` 降级 | 缺失 | 缺权威 renderer 时保留原版近似预览分支；不能把近似结果标注权威验证通过；必要的真实性状态应按产品要求以状态或错误呈现 |
| PDF 预览、页数和下载 | `renderers/pdf-renderer.tsx:53` 调 `/files/content-binary` 后 react-pdf 渲染，`:98` 下载 | 只有原文件下载 | 浏览器预览 PDF 并下载原文件；PDF worker/CMap/字体资源自托管；加密或损坏文件清晰失败；跨用户 URL 不可读 |
| 旧 PPTX 结构化近似预览 | `api/pptx.py:852` `POST /api/files/pptx-preview`；`pptx-renderer.tsx:239` | 缺失 | 文字/表格/图片/图表/主题相关解析使用原接口；原始文件下载保留；近似结果不冒充截图或 Office 编辑器 |
| 文本/代码/Markdown/CSV/SVG/Mermaid/HTML/React 制品预览 | `types/artifact.ts:3` 16 种类型；`artifact-panel-content.tsx:22` 分派；`file-preview-renderer.tsx:52` 按扩展加载 | 只渲染聊天 Markdown | 接入 Artifact 面板、源码/预览切换和对应原渲染器；HTML/React 等主动内容与云登录域/凭据隔离，不得窃取管理会话 |
| 图片、音频、视频预览 | `artifact-panel-content.tsx` `ImageRenderer`/`MediaRenderer` | 缺失 | 本用户媒体预览/播放、正确 MIME、尺寸/体积限制；流式或 range 下载必须重新授权 |
| Artifact create/update/rewrite 和版本导航 | `tool/builtin/artifact.py:20`；`stores/artifact-store.ts`；`artifact-panel-header.tsx:50` | 缺失 | 模型工具产生真实制品，增量替换及重写一致；同 identifier 的历史版本可切换；刷新/重连后恢复；跨用户 state 不复用 |
| 文件卡片、工作区文件列表、预览与另存副本 | `components/parts/file-artifact-card.tsx:189`；`components/workspace/files-section.tsx:11`；`lib/file-artifact-actions.ts:20` | 只有上传文件平铺列表和下载删除 | 模型生成文件出现可点击卡片与工作区列表；二进制/文本都能预览下载；刷新后从持久会话重建 |
| 持久文件版本历史和显式恢复 | `api/file_versions.py:124` `GET /api/file-versions`、`:158` `POST /{id}/restore`；`storage/file_versions.py:167`；`file-version-history-dialog.tsx` | 缺失 | 编辑前版本持久保存；浏览历史、选择恢复；恢复前当前文件也形成恢复版本；并发/取消/越界/旧工作区身份拒绝；重启后可恢复 |
| 第一方签名 Office 模板 | `office_templates/bundled.py`；`office_templates/assets/catalog{,.sig}.json`，3 个真实 DOCX/XLSX/PPTX 资产；`office.py:3722` | 缺失 | 签名及文件摘要验证、只读资源、受支持文字占位符实例化；生成结果经历同样事务和视觉验证；篡改失败关闭 |
| 用户 Office 模板 Beta：导入/预览/确认/删除/实例化 | `api/office_user_templates.py:741` list、`:795` import、`:1047` page、`:1140` approve、`:1317` delete；`user-office-template-card.tsx:163` | 缺失 | 按工作区导入模板、占位符 schema、不可变 revision、人工确认及预览、状态版本 CAS、幂等和审计；确认后模型才可使用；其他用户 template_ref 不可见 |
| 会话导出 Markdown/PDF | `api/sessions.py:681` `GET /sessions/{id}/export-pdf`、`:682` `/export-md`；`hooks/use-session-export.ts:29` | 缺失 | 浏览器下载带 Unicode 安全文件名；内容与当前会话角色/消息顺序一致；PDF 中文字体可用；跨用户会话导出拒绝 |
| Markdown 制品导出 PDF | `api/artifacts.py:22` `POST /api/artifacts/export-pdf`；`markdown-renderer.tsx:68` | 缺失 | 原 Markdown 表格/代码/图文输出 PDF；有界解析、字体与超时；敏感本地图片路径不可读取 |
| 系统默认应用、选择其他应用、定位文件、复制真实路径 | `file-artifact-card.tsx:199`；`files.py:1111`、`:1142`、`:1161`、`:1198`；`file-artifact-actions.ts` 已对浏览器隐藏 | 浏览器无此能力 | 保留浏览器适用的预览/下载/虚拟相对路径，必要时支持下载后本地打开；不能在云主机启动应用或给用户主机绝对路径。若产品明确要求远控本机，需单独客户端代理能力，不能谎报纯 Web 已有 |

## 发行门禁与运行依赖事实

`backend/app/release_features.py:38` 到 `:47` 的检查点/回退/Office V2/模板 Beta 都是 `True`；`release_readiness.py:37` 把 Office authoring 依赖组合为 checkpoints + rewind + validator，把用户模板依赖于 authoring。`runtime/v11_readiness.py:50` 再计算运行服务与 renderer quality。

`office_validation/startup.py:68` 是原版真正的组合入口，会先装近似预览，再尝试冻结包权威身份、签名 renderer、原生沙箱行为探针、golden 文档探针和 precommit 服务。`office_rendering/release_identity.py:135` 明确拒绝非 `sys.frozen` 进程。因此直接 `uvicorn app.main:app` 不能凭借安装 LibreOffice 就获得原版权威 Office 写入能力。云服务必须新增可审查的服务镜像身份/renderer attestation 装配机制，或者运行适合 Linux 容器的原版冻结后端包；不能伪造 `sys.frozen` 或绕过验证。

### 本机只读依赖检查

本轮用 `backend/.venv/bin/python` 只检查 import spec、PATH 和 source gate，结果如下，不代表运行过所有模块：

| 依赖 | 本机现状 | 完整后端/容器要求 |
|---|---|---|
| Python | 3.13.3 | 原版 `pyproject.toml` 要求 >=3.12；完整原版锁和当前 cloud 锁不同，需单独兼容验证 |
| python-docx/openpyxl/python-pptx/pypdf/Pillow/cryptography | 可找到 | 支持基础文档读写；必须保留原版 Office 精度敏感 pin |
| reportlab/markdown/BeautifulSoup | 当前 Cloud venv 找不到 | 会话和 Markdown PDF 导出依赖；完整后端环境需安装 |
| numpy/pandas/matplotlib | 当前 Cloud venv 找不到 | 数据分析、代码执行、图表和 Office 静态图像等路径需要 |
| readabilipy/mcp/acp | 当前 Cloud venv 找不到 | 原版完整 engine 的导入/网络/扩展路径需要，不能只用 cloud 精简依赖启动 |
| Poppler `pdftoppm` | `/opt/homebrew/bin/pdftoppm` 存在 | PDF 页面栅格化可用基础二进制；原版权威流程仍需固定 bundle 里的已验证版本 |
| LibreOffice `soffice` / `libreoffice` | PATH 没有；`/Applications/LibreOffice.app` 不存在 | 近似 Office 服务需安装；权威服务还要求原版签名身份和探针 |
| bwrap | PATH 没有 | Linux worker 隔离策略须提供 namespace/seccomp/cgroup 或等效容器边界，不把 subprocess 当安全沙箱 |
| Docker | CLI 存在 | 未在本核验启动 Docker 或构建镜像，不宣称原版容器已具备测试条件 |
| 发行 renderer 原生 bundle | 仓库 `app/data` 里未见 renderer bundle | 仓库有 staging/校验源码，不等于发布二进制、签名和部署资产已经到位 |
| 第一方 Office 模板资产 | `app/office_templates/assets` 内真实存在 3 个模板及签名 catalog | 镜像必须保留该目录；不能在精简复制/`.dockerignore` 时误排除 |
| 前端 docx-preview/react-pdf/SheetJS | `frontend/package.json` 声明；PDF assets 有 postinstall/prebuild | 复用原构建和 PDF assets 预制脚本，避免浏览器动态请求未授权服务资源 |

## 完整 engine + 云身份代理的接缝

### 1. 用户隔离边界必须包括文件系统和进程

原版是单用户宿主服务。`api/files.py:966` `/content`、`:1002` `/content-binary` 可以接受绝对路径，`:1245` `/list-directory` 默认主机 HOME，`:1316` `/attach` 可引用现存宿主文件。给这些接口只加云 cookie 或把每用户目录设成不同前缀，并不能阻止同 UID 后端读取另一用户目录或主机文件。

可直接复用原 API 的最小可靠方案是每用户 engine 运行在独立容器/受限文件系统，唯一可写持久卷是该用户私有数据；宿主源码、云平台 `.env`、其他用户卷和模型 vault 都不挂载进去。API 网关根据认证后的 user_id 选择进程/实例，不接受用户传入实例地址。内部 engine token 不返回浏览器，不能从外部旁路访问内部端口。若先做同机开发运行器，必须标注其缺少多租户 OS 边界，不能当云端隔离验收通过。

### 2. 固定 engine 内部虚拟路径

建议每个实例看到相同结构，例如 `/workspace/<workspace_id>`、`/data/managed-workspaces/<session_id>` 和私有 `/data`；物理宿主路径由网关/调度器保存。前端显示相对工作区路径，不暴露宿主 `/Users/...`。原版 `SUXIAOYOU_MANAGED_WORKSPACE_ROOT` 和 `SUXIAOYOU_APP_PRIVATE_DIR` 是进程级配置，适合每用户进程设置，不适合在同一进程按请求改环境变量。

云 `Workspace` 与原 `Session`/`WorkspaceInstance` 之间需持久桥接，同一工作区多会话共享文件，folderless 会话沿用原 managed 机制。`workspace_instance_id` 是恢复/预览/模板校验身份，不可只做字符串路径替换。

### 3. 上传与目录导入

浏览器通过网关鉴权后将附件写到对应 engine 的上传/工作区入口。必须保留原版 attachment metadata 字段与输入快照流程，而不是只把 `cloud.FileAsset` 名称加入 prompt。目录导入要保留层级，校验 Unicode、大小写冲突、ZIP 路径、symlink/hardlink/特殊文件、文件和总工作区配额；只有全部验证成功后发布。全局 hash 去重不跨用户返回存在性、路径或 file_id。

### 4. 预览与下载资源代理

前端统一 API 基址需要经过云代理，包括 POST 内容请求、二进制 base64、SSE、Office `page` 图片、用户模板 `page`、PDF 导出和媒体资源。代理不可只照顾 JSON。每条请求需当前账号认证和实例归属校验；缓存键包含用户与 workspace 身份或缓存私有；禁用共享公共静态文件目录。源文件变化后 renderer 的 expected sha/cache_key 合同继续有效。

对于现有 `FileAsset` 对象存储与 POSIX 工具目录，应明确哪个是源：建议运行时持久 POSIX 卷为可变工作区，云对象存储保存不可变上传/制品/版本的副本与备份。不能让 S3 `get/put` 假装支持原版 `os.replace`、inode/FD 锁和同目录原子交换。

### 5. 事务、版本与恢复

`WorkspaceMutationTransaction`、`FileVersionStore`、检查点存储、Office draft/precommit 作为一组保留；原版依赖同文件系统原子操作、真实目录身份和版本 blob 根目录。不能只迁移工具调用入口而跳过版本生成。每个用户 engine 的 `/data/file-versions`、检查点 DB、工作区卷和 Office 缓存按同一静止时间点备份/恢复；恢复后旧 session/workspace 身份必须按原规则重验。

原版文件锁部分为进程内锁；不能让两个同用户 engine 共享卷同时写入。若要多个 worker，需要一用户/工作区明确单写者租约或跨进程锁，并让恢复/rewind 与 Agent 写操作互斥。

### 6. 模型与计量

复用完整 engine 后不再仅是单个 `cloud.Job` 调一次模型：工具循环、修复、验证、子 Agent、压缩等都会产生模型请求。云额度和账本必须挂在原 provider 调用边界，每次调用预占并结算，继承可信 user/job/session 标识。不能把整场 Agent 最后一条 usage 当总用量；Office 修复调用也必须记录。提供给 engine 的模型凭据应由受控网关代理按授权使用，或通过只读私有 secret 提供；主动内容预览和工具进程不应获得主平台密钥。

### 7. 有意保留原浏览器分支

`lib/file-artifact-actions.ts:20` 已明确浏览器只显示 preview/saveCopy；`use-session-export.ts` 已有 browser `downloadBlob` 分支。可直接复用。应替换 `browseFiles()` 的后端原生弹窗 fallback 为浏览器文件输入与目录上传，并让“选择工作区”浏览该用户的云空间；不要把云机器 Finder/资源管理器动作暴露为用户功能。

## 文件/Office 完成功能的验收组

| 验收组 | 最小证据 |
|---|---|
| OF-01 文件闭环 | 用户 A 上传 PDF/DOCX/XLSX/PPTX/图片及多级目录 → 真实模型 `read`/search → 创建和编辑输出 → UI 出现文件卡片 → 对应预览 → 下载后重新解析比对；重启后保留 |
| OF-02 Office 操作一致 | 原 `test_office` / `test_office_v2` /模板/renderer/repairer 测试继续通过，新增经过云代理的 DOCX/XLSX/PPTX 各至少一个完整高级操作场景；真实模型产生工具调用证据 |
| OF-03 原版权威合同 | 运行身份、签名 renderer、原生沙箱/golden 探针、precommit、视觉校验、checkpoint 证据都真实就绪；缺失时不标完成，不能用模拟 renderer 或测试 fixture 作为交付证据 |
| OF-04 回滚恢复 | 对现有文件编辑后查历史、恢复、保留恢复前副本；并发编辑/删除/取消/异常失败的可见文件与版本记录一致；重启或备份恢复可继续使用 |
| OF-05 模板闭环 | 第一方三格式模板签名实例化；用户模板导入→逐页预览→确认→真实模型实例化→修改→预览/下载；篡改/旧 revision/CAS 冲突/未确认拒绝 |
| OF-06 制品与导出 | 每个已声明 Artifact 类型都可重开预览；create/update/rewrite/版本导航持久一致；会话 MD/PDF 与 Markdown PDF 导出、中文字体和文件名正确 |
| OF-07 跨用户拒绝 | B 使用 A 的 session/file/version/template/workspace/cache_key、预览页/二进制/导出/媒体 URL 均拒绝；主动 HTML/React 不能读取 cookie、管理 API 或其他用户数据 |
| OF-08 路径边界 | 相对/绝对/`..`/编码穿越、大小写、Unicode、symlink/hardlink、特殊文件和压缩炸弹覆盖；目录接口不泄露宿主 HOME；工具执行进程无法读另一用户卷或平台凭据 |
| OF-09 用量完整 | 文件任务多轮工具、Office repair、子 Agent、压缩/验证调用用量归属一致并累计；额度不足拒绝新调用，正在执行与取消/未知 usage 遵循原账本合同 |

现有 Online 的单轮文本/只读提取测试不能替代以上验收。测试条件应分别标注“源码回归已过”“本地完整引擎可运行”“容器隔离已验”“真实模型端到端已验”，不能用一个总数掩盖未具备的运行依赖。

## 完整引擎改造后的实现与证据（2026-11-15）

以上内容保留为迁移前基线。当前已采用每用户完整原引擎，容器普通用户 `10001`、唯一持久卷 `/tenant`、工作区 `/tenant/workspaces`；原文件事务、版本、会话/工作区身份和全部 Office 工具/预览接口继续执行。`runtime_files.py` 提供浏览器目录列表/新建/按层级上传，原 `FileAttachment` 元数据直接进入原 Agent 流程。`upload.ts`、全局云目录 Provider 与原目录组件替代原生选择器；目录上传按文件原子发布，后续文件失败时保留并显示已成功的附件，不宣称整个多文件批次原子提交。

运行镜像使用 Python 3.12 与原完整 hashlock，实际安装 Node 22、Git、ripgrep、bash、bubblewrap、LibreOffice Writer/Calc/Impress、Poppler 和固定字体。Python 3.13 无法安装锁定的 NumPy 1.26.4 wheel，不能继续以精简 cloud venv 代替完整引擎验证。

Office 云权威采用独立平台签发链，不改变桌面冻结发行验证器：平台把固定镜像 digest、固定工具/字体、真实 golden 像素和隔离探针绑定到 Ed25519 descriptor；转换在无网络、根文件系统只读、非 root、无租户卷与无凭据的一次性容器运行。引擎只获得公钥和受限调用 capability，复验签名、来源 SHA、参数、固定镜像、PDF/PNG 字节及像素后进入原 cache、draft、policy、precommit、事务与 checkpoint。平台私钥仅在控制面私有 vault，相同 uid/权限/O_NOFOLLOW 与临时文件原子发布防止多进程读到半写密钥。转换输入、输出、页数、像素、内存、进程与时间均有预算；失败不发布创作权威。

真实普通账号已用 DeepSeek 调用原 `office` 工具完成 DOCX 创建/编辑（表格、页眉页脚）、XLSX 创建（公式、原生图、冻结窗格、条件格式、数据验证），通过 `authoritative_pass`，随后由认证 API 读回 OOXML 并获取真实 PNG。DOCX 编辑产生真实 previous-version ID。首个 Excel 图表位于 D1 越过打印页右边界，产生空白比例 `0.9990686` 的第二页，原 `0.999` 门禁正确拒绝；改为 A6 后单页通过，未降低视觉阈值。

真实模型还完成三种第一方签名模板实例化及用户 DOCX 模板导入→未确认禁止实例化→PNG 预览→确认→真实模型实例化→读回占位替换的完整链路。该模板阶段 21 次新增模型调用全部 settled，共 153,204 Token；所有支持调用同样经过中央计量。

高级 PPTX 原生图验证暴露原代码冲突：工具 schema 支持原生图，但结构校验复用模板导入的全禁 embedding 策略，拒绝 python-pptx 的图表数据 XLSX。已仅对 authoring 结构校验新增精确 native-chart workbook 策略：标准路径、chart `externalData`/relationship 归属、内层无宏/外链/再次嵌入、与外层共用 entry/解压预算。模板导入默认策略保持关闭。该修复已在镜像 `f279d880a4ba979718fbdc0e41fea014be82414b0df494230dde2d3070f43de4` 实际重启后通过真实 DeepSeek PPTX 补验：原生图、表格、备注标记均存在，authoritative_pass、PNG与目视布局通过；此阶段6次模型请求全部settled，58,289 Token。19个 native-chart/XML安全案例包含 UTF-16/32 DTD、伪造图表归属、类型与预算拒绝。

可复查证据见 [Office 实测记录](parity-office-validation.json)，实际账号验收脚本为 `scripts/cloud-office-smoke.py`（显式运行会调用真实模型）。当前源码检查结果：云桥接与签名边界 22 项通过；原 Office 模板、验证、v2 工具与新增 native chart 安全回归合计 162 项通过。此前严格一发容器 + HTTP 签名 + 引擎 Provider 的真实三格式转换也已通过。浏览器逐格式视觉交互、中文长会话导出、完整版本恢复/rewind、跨用户所有 preview/cache/template ID 负向组合仍需按 OF 验收组单列，不以这些通过数代替。

### 运行镜像升级后的用户模板

批准记录绑定 renderer/image、字体、参数与来源摘要。镜像升级后，旧批准仍保留为历史证据，但不能授权新 renderer 的实例化；原 `approve` 的幂等重放不会重新生成证据。已新增 `POST /api/office-v2/user-templates/{template_ref}/revalidate`，使用与批准相同的 session/workspace、revision、state_version、source SHA、render cache key 作为 CAS 条件。服务器重新校验旧 registry 的原始字节与占位 schema，再用当前 renderer 生成同一 template_ref 的下一不可变 revision。旧记录的来源、批准时间与渲染证据保留，状态版本递增；新 revision 处于 `needs_confirmation`，必须重新预览并显式批准。

模板卡已提供“重新校验”和 revision 标识。待确认模板也能重新校验，避免镜像升级前未批准的模板卡住。审计失败、来源篡改、外部工作区与旧 CAS 重放均不能发布新批准。Office 用户模板 API、模板与结构验证、工具相关回归合计 163 项通过（与前述 162 项集合重叠，不能相加）。

部署时应先构建镜像，再重启控制面以重新固定与探测 Office digest，随后重启各用户引擎获取新 descriptor。真实排查已确认：旧控制面缓存 digest 不能运行时 `/render` 安全失败；新 OfficeGateway 对相同旧模板来源已真实生成一页 PNG。最终普通账号已在镜像 `c18f546bc41655f6250b851c9a9fb6580c51188981b67bff9dfce6939dcfc5b2` 真实重启后完成旧 rev1→重新校验 rev2→PNG 预览→显式审批→`can_instantiate=true`。旧来源/schema/渲染证据/批准时间精确保留，旧状态版本递增，旧 CAS 重放返回409；用量回执集合前后相同，新增模型调用为0。记录见 `parity-office-validation.json.user_template_upgrade_revalidation`，复验脚本 `scripts/cloud-office-revalidate-smoke.py`。

最终镜像升级补验：用户模板已连续完成image580→c18f（revision1→2）与c18f→final d2a754（revision2→3）重新校验、PNG预览和显式批准，均0新增模型调用；旧revision/source/render/批准时间保留、旧CAS重放409。详见`parity-office-validation.json`的`user_template_upgrade_revalidation`与history。
