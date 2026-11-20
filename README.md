# suxiaoyou-online

[![CI](https://github.com/keliihall/suxiaoyou-online/actions/workflows/ci.yml/badge.svg)](https://github.com/keliihall/suxiaoyou-online/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

<p align="center">
  <img src="Suxiaoyou-Logo/mascot.png" width="120" alt="苏小有 Logo" />
</p>

苏小有 Online 是面向浏览器的 AI 工作助理，基于[苏小有客户端](https://github.com/keliihall/suxiaoyou)改造，复用完整工作台与 Agent 引擎。

支持多用户登录、独立云工作区、后台任务、模型配置、管理员控制台，以及 Token 用量统计和额度限制。文件与会话按用户隔离；生成的文件可直接在页面预览、下载。

模型服务商和 API Key 可在管理界面配置，密钥只写入、不回显。平台通过统一模型网关调用真实模型，并记录用量。

## Online 部署与使用

需要 Docker Engine / Compose v2、Node.js 22 和对应镜像要求的 Python 环境。完整部署使用专用 rootless Docker daemon，为每个用户创建独立运行时、网络和持久卷。

1. 按[云端部署与测试说明](docs/cloud-testing.md)从 `.env.cloud.example` 生成私有配置。
2. 构建用户运行时镜像，再启动 `compose.cloud.yaml` 中的平台服务并执行迁移。
3. 使用管理 CLI 创建首个管理员，访问 `/cloud` 登录。
4. 在 `/cloud?manage=1` 配置真实模型、密钥、用户及额度；登录后进入 `/c/new` 使用完整工作台。

相关文档：

- [完整部署、升级、测试与备份恢复](docs/cloud-testing.md)
- [中央模型网关与模型配置](docs/cloud-model-gateway.md)
- [客户端能力移植与逐项验收](docs/cloud-parity/README.md)

实际调用、受控 API 回归和外部环境待验项目分别记录在验收文档中。不要提交 `.env.cloud`、供应商密钥、数据库、会话令牌、租户文件或构建产物。

## 能做什么

- **读懂办公文件**：理解 Word 文档、Excel 表格、PPT 演示稿、PDF、本地文件夹和多份资料。
- **生成工作成果**：根据材料生成调研报告、会议纪要、工作计划、项目复盘、邮件草稿、表格分析和汇报提纲。
- **整理本地资料**：围绕本地文件进行归纳、改写、对比、提取重点和任务拆解。
- **辅助数据分析**：读取表格数据，发现异常、提炼结论、整理指标说明和行动建议。
- **延续工作上下文**：在同一条对话里继续补充、修改、追问和生成后续材料。
- **跨多轮推进目标**：在本地桌面对话中输入 `/目标 <要完成的结果>`，让助手在预算和现有权限内自主续跑，并可随时查看、编辑、安全暂停、继续或清除。

## 适合场景

- 把多份资料整理成一份结构清晰的报告。
- 从会议录音稿、纪要或文档中提取重点和待办事项。
- 分析 Excel 表格中的数据变化、预算差异和业务线索。
- 根据 PPT 或方案材料生成讲稿、摘要和汇报邮件。
- 批量读取、分类和汇总本地办公文件，并生成整理建议或受支持的输出文件。

## 桌面版与云端版

本仓库保留原桌面端代码。Online 的文件、对话和任务运行于服务器上按用户隔离的云工作区；浏览器通过上传、预览和下载处理文件。

下方安装与本地开发说明适用于保留的桌面版；Online 的启动、任务持久化和运行边界以[云端说明](docs/cloud-testing.md)为准。

## 桌面版安装与使用

> 发布状态：`v1.1.0` 以公开的 `UNSIGNED-DEGRADED` 预发布配置交付。七个安装包均在文件名中带有
> `UNSIGNED-DEGRADED`，Release 不设为 latest；Windows/Linux 包未做发行方签名，macOS 应用仅为
> ad-hoc 签名，DMG 未做 Developer ID 签名、Apple 公证或 staple。发布清单与能力声明会以机器可读
> 形式记录这些边界。
>
> v1.1 开放 ACP、worktree、checkpoint/rewind、Hooks 和只读验证 Agent，并增强 Office 模板、图表、
> 复杂格式及预览/视觉验证闭环。公开包不携带项目冻结的 authoritative Office renderer，因此权威
> Office authoring/视觉提交仍会 fail-closed；若本机存在兼容 LibreOffice，只可提供明确标为近似的预览。
>
> 声明式 `write`、`edit`、`apply_patch`、Office 与版本恢复在各桌面平台使用受守卫的目标文件
> 流程，并由对应原生 runner 做故障注入和冻结后端验证。Bash/Python 在 Linux 使用 Bubblewrap、
> 在 macOS 使用 Seatbelt，并先在应用私有事务副本执行；Windows 使用 Job Object 回收进程树，但命令
> 直接作用于已批准工作区，不提供文件系统或网络隔离，失败可能留下改动，因此界面和发布说明不会把
> Windows 命令描述为隔离或可回滚执行。
>
> checkpoint/rewind 使用耐重启的工作区身份 v2：POSIX 优先使用目录 xattr，
> 不支持时使用安全标记文件；Windows 使用原生卷序列号和文件 ID。旧 `stat-v1` 数据会在
> 启动时以“外部历史先校验、数据库后提交”的方式幂等迁移。目录缺失、被替换或身份不可证时，
> 旧 checkpoint 不会绑到同路径的新目录；只保留并阻断该工作区的恢复，不影响其他工作区或本地服务启动。

系统要求：macOS 11.0 或更高版本（Apple Silicon 与 Intel 分别提供原生安装包）；Windows x64、Linux x64 与 Linux ARM64 安装包由对应平台的原生 CI runner 构建与验证。

1. 下载并安装苏小有桌面端应用。
2. 在设置中选择可用的模型服务商、本地模型或自定义接口。
3. 新建对话，上传或选择要处理的办公材料。
4. 直接提出目标，例如“生成一份调研报告”“整理会议纪要”“分析这张表格”“写一封汇报邮件”。
5. 根据结果继续追问、修改或生成新的文件。

原桌面版目标模式的发布范围为本地单客户端，详细的命令、安全边界、恢复语义和验证状态见[目标模式说明](docs/goal-mode-development-plan.md)。Online 的持续任务由用户云运行时执行，具体移植状态见[云端验收表](docs/cloud-parity/parity-acceptance.md)。

## 项目结构

```text
desktop-tauri/    桌面端外壳与系统集成
frontend/         应用界面、对话、设置与文件展示
backend/          助手服务、工具调用、文件处理与模型连接
backend/app/cloud/ 云端身份、模型网关、配额与运行时管理
deploy/cloud/     容器构建、部署、升级与验收脚本
compose.cloud.yaml 云端服务编排
```

## 本地开发

建议使用 Node.js 22、Python 3.12 和 Rust 1.96.1。

```bash
npm ci
npm --prefix frontend ci --legacy-peer-deps
npm --prefix desktop-tauri ci
python3.12 -m venv backend/venv
backend/venv/bin/python -m pip install --require-hashes -r backend/requirements.txt
backend/venv/bin/python -m pip install -e backend --no-deps
```

常用命令：

```bash
npm run dev:all
npm run build:frontend
node --test scripts/*.test.mjs
cd backend && venv/bin/pytest -q
```

请勿提交 `.env`、模型服务商密钥、会话令牌、数据库和构建产物。

## 开源信息

苏小有 Online 基于苏小有客户端持续开发，保留原项目源码与提交历史。

项目采用 [Apache License 2.0](LICENSE)。
第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
安全问题请按[SECURITY.md](SECURITY.md) 私下报告。
