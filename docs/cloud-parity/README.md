# 苏小有客户端能力云端迁移核验

当前实现复用原完整工作台与每用户独立原引擎。业务实现接入、真实调用通过、受控API回归通过和外部环境待验分别记录，不能互相替代。首次差异审计保留为改造前基线。

- [F01–F56逐项移植与验收](parity-acceptance.md)
- [当前开发与运行状态](cloud-development-status.md)
- [前端首次差异基线](parity-frontend.md)、[Agent首次差异基线](parity-agent-runtime.md)、[Office/文件首次差异基线](parity-office-files.md)
- [完整启动、升级与备份恢复](../cloud-testing.md)、[中央模型与Mac/Ollama配置](../cloud-model-gateway.md)

## 冻结公开证据

| 证据 | 范围 |
| --- | --- |
| [parity-delivery-validation.json](parity-delivery-validation.json) | 最终运行环境、镜像、schema与回归结果 |
| [parity-route-inventory.json](parity-route-inventory.json) | 最终实际镜像路由结构（不替代行为验收） |
| [parity-persistent-validation.json](parity-persistent-validation.json) | 真实Goal/双Agent/自动化/MCP/插件/记忆及final镜像持久性 |
| [parity-subagent-checkpoint-validation.json](parity-subagent-checkpoint-validation.json) | 父子checkpoint回归与真实双Agent持久ledger |
| [parity-browser-validation.json](parity-browser-validation.json) | 真实浏览器审批、Question、Plan、下载与移动流程 |
| [parity-runtime-validation.json](parity-runtime-validation.json) | 真实Python/科学计算与沙箱边界 |
| [parity-runtime-control-validation.json](parity-runtime-control-validation.json) | 真实checkpoint/rewind、Git worktree、hooks及紧停 |
| [parity-office-validation.json](parity-office-validation.json) | 真实Office创作、预提交、模板及两次镜像升级重验 |
| [parity-acp-validation.json](parity-acp-validation.json) | 真实ACP WebSocket与stdio桥接 |
| [parity-custom-headers-validation.json](parity-custom-headers-validation.json) | 只写自定义请求头的真实TCP/SDK/网关协议回归 |
| [parity-deployment-validation.json](parity-deployment-validation.json) | 真实Compose完整租户备份与空目标恢复 |
| [mlx-node-real-evidence.json](mlx-node-real-evidence.json) | 真实Mac节点推理与额度 |
| [mlx-live-http-evidence.json](mlx-live-http-evidence.json) | 生产HTTP Mac状态与operator批准的新HF alias |
| [mlx-live-agent-evidence.json](mlx-live-agent-evidence.json) | 原完整Agent经Mac模型实际生成与计量 |
| [ollama-live-evidence.json](ollama-live-evidence.json) | 私有Ollama实际安装/推理/恢复及历史重试边界 |

主cloud回归358passed/4skipped；完整runtime补验12passed覆盖4skip（存在重叠，不求和）；浏览器受控API回归19passed。真实DeepSeek持久能力专用账号最终230342Tokens/30settled/0reserved，含失败与自动memory，final d2a重启复核0新增模型调用。详细边界和未验组合见F表。

仅公开不含密钥的JSON/Markdown。私有账号、供应商Key、Cookie、模型原始请求、数据库、截图和租户卷均未复制到此目录。
