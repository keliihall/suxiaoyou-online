# Online 完整工作台开发状态

源码位于本工作区 `suxiaoyou/`、分支 `feat/cloud-web`；改动仍在本地，未替用户提交或推送。当前已从轻量资料聊天改为**原完整工作台 + 每用户独立原 Agent 引擎 + 云身份/管理/统一计量**。不能继续沿用早期“没有迁移 Bash/Goal/MCP/Office/自动化”的范围说明；也不能把原实现已接通当作所有真实流程已通过。

完整逐项状态见 [F01–F56 移植与验收表](parity-acceptance.md)，首次差异评估保留于[前端基线](parity-frontend.md)、[Agent 基线](parity-agent-runtime.md)、[Office/文件基线](parity-office-files.md)。这些基线中的“当前缺失”指改造前，不覆盖新状态。

## 当前可真实运行的链路

- Web：**http://localhost:3018/c/new**；账户/个人模型/后台：**http://localhost:3018/cloud?manage=1**。使用 `localhost` 与配置 Origin 一致。
- API：`http://127.0.0.1:8018`；完整原 API/SSE 通过 cookie/CSRF 网关转发到当前用户私有引擎。不同用户使用独立容器、卷、网络与原 SQLite。
- DeepSeek 真实托管 Key 已配置，mock 关闭。已有真实聊天 1,727 Token settled 记录，并已实际完成原 Agent write/read/present_file；后续累计以中央请求账本为准。无需再索取或向聊天发送密钥。
- 真实浏览器已确认 DeepSeek 模型可选、输入启用、默认 `/tenant/workspaces` 保留。实际发现的 custom_cloud 被 BYOK 过滤和新对话清空 workspace 两项缺陷已修复并纳入回归。
- 两账号同虚拟路径保存不同文件字节，跨账号 file/session 返回 404。执行实际 Python/pandas/NumPy/Matplotlib 并验证 bwrap 网络/私有路径/环境/子进程隔离，见[运行时证据](parity-runtime-validation.json)。
- Office 已通过真实隔离 Docker 转换、HTTP Ed25519 签名、引擎复验与真实 DeepSeek 三格式高级创作（DOCX 表格/页眉页脚、XLSX 公式/图表/条件格式/验证、PPTX 图表/表格/备注），均通过原 precommit 与 PNG；三格式签名模板及用户 DOCX 导入/确认/实例化也实际通过；跨镜像旧模板重新校验→新revision预览→显式审批恢复可用、旧记录保留与旧CAS拒绝均已实际通过（0新增模型调用），见[Office 证据](parity-office-validation.json)。
- 专用新验收账号已真实完成Goal安全暂停/同会话恢复/完成、npm filesystem2.3.0 stdio MCP精确命令指纹批准与DeepSeek实际调用、插件/skill启停、memory刷新与认证下载字节校验、无浏览器/SSE的manual与1分钟interval独立运行及history对账，见[持久能力证据](parity-persistent-validation.json)。首次TaskBatch揭露父checkpoint未准入；c18f镜像真实复验双Agent及1父2子持久ledger通过；final d2a无LLM重启后Goal/父子消息/ledger/文件/插件/skill均保留且自动化仍停用，最终230342Tokens/30settled/0reserved，失败与修复证据保留。
- 真实原Agent经专属HTTPS Mac节点运行Qwen2.5-0.5B并中央结算14438 Tokens；直接节点0额度429已验，见[Mac原Agent证据](mlx-live-agent-evidence.json)。Ollama已真实安装/拉取Qwen0.5B并完整原Agent实际SSE与11339Tokens结算；小模型答错、CPU首次预填充慢的限制保留于[Ollama证据](ollama-live-evidence.json)。
- 真实浏览器已完成审批、写读和下载、Question刷新后选择、Plan接受前后副作用校验、移动历史及FTS/mention，见[浏览器证据](parity-browser-validation.json)。
- 原运行时控制已实际完成hook指纹审批/marker、Git worktree绑定/检查/释放GC、真实文件checkpoint预览/回退删除、紧停423与恢复后read；12笔112145Tokens均settled，原100000预算超出如实记录，见[运行时控制证据](parity-runtime-control-validation.json)。
- ACP 真实 WebSocket initialize v1/session/new 已执行，见[ACP 证据](parity-acp-validation.json)；stdio 桥接脚本复用同一用户引擎，不创建第二 DB owner。

本机私有账号保存在被 Git 忽略、0600 的 `.cloud-dev.json`；不要复制其内容到报告/日志。供应商 Key、订阅凭据、节点 token 均只写不回显。

## 本轮前端与备份验证

| 验证 | 当前结果 |
| --- | --- |
| 原完整 UI | 原 `(main)`/`(mobile)` 布局恢复；云登录启动完成前不挂业务查询；Cookie 失效/登出完整卸载并关闭流；用户 UI 存储隔离 |
| 云浏览器回归 | `cloud-full-workbench.spec.ts` + `cloud-workspace.spec.ts` **19 passed**（含记忆下载与共享/个人请求头认证）。均为受控 API fixture，不能代替真实模型/OAuth/Mac 节点验收 |
| 存储/幂等 | 新云用户隔离单元回归 **3 passed**；原幂等/输入队列 **12 passed** |
| 静态检查/构建 | TypeScript、定向 ESLint 通过；最新 `.next-parity-delivery` production build 成功，目标 API8018，含记忆下载/账户401清理、Office重验、共享/个人请求头与个人chat连接测试；已在3018运行，API/readyz与/c/new均200 |
| 管理/私有模型 | 共享与个人BYOK、加密只写自定义请求头、header-only及显式无需认证、个人chat连接测试、能力metadata、图片路由/额度/价格、图片 count 对账、专属 Mac 节点只写 token+证书 pin 管理入口已实现 |
| 全量 tenant 备份 | format 2：PostgreSQL + cloud_objects + 所有 owner-labelled tenant 卷；停止准入/原引擎一致归档、finally 恢复原运行状态；空目标恢复撤销旧 cookie/capabilities，保留 unknown。13 项测试通过（追加 manifest/归档父目录 symlink 拒绝），含真实 SQLite/二进制/symlink 归档往返；真实 Compose 完整备份→空目标恢复→新DeepSeek任务通过，目录0700/密钥0600与仅恢复精确原容器已实测，见部署证据 |

辅助说明已在原工作台、设置、插件、自动化、移动和后台相关组件移入源码注释；字段、状态、用量、文件路径、实际错误及需要用户决策的破坏性确认继续展示，未使用全局 small/text-xs CSS 隐藏业务数据。

## 仍需实际环境验收

- 完整 F 表中的子Agent真实TaskTool/取消等组合、自动化cron/并发防重、权限状态组合、真实断线/恢复与rewind/worktree的冲突/脏目录等边界组合，不能用接口可达或fixture替代；Goal/计划/提问/审批已取得实际主链证据。
- Office 三格式真实创作和用户 DOCX 模板主链已通过；浏览器各格式交互、其他模板导入格式与全部 CAS/并发/取消负向组合仍按 F 表另验。
- ChatGPT/Google/其他MCP OAuth、实际SiliconFlow图片仍需各自验收；Ollama模型SDK/完整原Agent推理已有实际结算证据，所有模型管理组合另验；专属Mac节点binary/模型启动和原Agent推理已有实际证据，全模型管理组合另验。
- 完整Compose停机备份→空目标恢复→新DeepSeek任务已实际通过，见[部署恢复证据](parity-deployment-validation.json)；大卷跨宿主恢复、真实反向代理/HTTPS、S3、生产租户对抗/容量另列，不宣称已完成公网生产验收。

完整启动、runtime-image 先构建、专用 rootless socket/GID、模型与 Office 内部 URL、备份恢复命令见[云端测试说明](../cloud-testing.md)。真实供应商调用会产生费用，默认回归不会主动调用付费 API。
