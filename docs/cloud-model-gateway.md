# 完整引擎的模型与 Mac 节点网关

云端原完整引擎的每一步模型请求经控制平面计量，子 Agent、记忆、压缩、Goal 和后台任务使用相同 Provider 注册表。浏览器只持有平台会话；runtime 持有可撤销 capability，平台供应商 API Key 与 Mac 节点令牌均不进入 runtime。

## Chat、个人模型与图片

内部接口以 runtime capability Bearer 认证，不接受云登录 Cookie 替代：

| 接口 | 行为 |
|---|---|
| `GET /api/cloud/internal/inference/v1/models` | enabled chat 路由，仅共享或本人私有；含真实模型能力/价格元数据，无 Key |
| `POST /api/cloud/internal/inference/v1/chat/completions` | OpenAI Chat Completions，支持文本、推理、函数工具、stream/非stream、usage |
| `POST /api/cloud/internal/inference/v1/images/generations` | 原图片工具调用中央 SiliconFlow，operation_id 单次提交；PNG base64返回原事务 |
| `GET/POST /api/cloud/user/models`、`PUT/DELETE /api/cloud/user/models/{id}` | Cookie+CSRF；个人 BYOK加密，仅本人可见/调用，禁止引用平台 Key 环境变量 |
| `POST /api/cloud/images/generations` | Cookie+CSRF；用户显式确认的真实生成测试，同样占图片额度 |

每次 Chat 调用：完整输入预估→用户行锁及所有 active request 并发准入→月额度原子预占→发送前标 dispatched→真实 receipt 结算。默认真实模型为软额度；远端 tokenizer/多模态实际用量可能超过估算，不能把估算当硬 Token 上界。没有完整 usage 的发送中断保留 unknown，管理员对账；SDK重试关闭，不自动重发。每个API进程有独立lease owner，60秒租约只回收过期请求，活跃进程不受其他API启动影响。

图片数量独立记录在 `ImageQuotaBucket/ImageReservation`，`User.image_limit` 默认100/月；请求 Token=0，usage标明 `unit=image,count=1`。价格快照含 `image_price_microusd`，零值表示未配置价格；成功只证明图片交付，不能声称供应商提供了原生费用回执。管理员unknown图片对账使用 `usage:{count:1}`，Token字段不能替代图片数量。

平台共享/个人 BYOK 使用中央 SDK 原生receipt。用户自有订阅/Ollama由私有引擎观察，并在receipt中注明 `tenant_runtime` 来源。原配置界面对付费 BYOK 指向云管理入口；订阅授权、Ollama和专属MLX操作保留。用户自行在Bash/MCP调用自己的外部Key不属于可精确观测的平台供应商用量。

## 专属 Mac 节点部署与管理

Linux runtime不执行Apple Metal。operator在真实Apple Silicon Mac上准备独立服务账号、私有Python环境和节点根目录。完整原Rapid-MLX模型catalog可通过重复 `--model` 选入白名单；`qwen2.5-0.5b` 为低资源真实验收模型。节点不执行网络安装指令、不接受租户请求任意repo/URL/缓存路径、不接管其他进程。操作者可通过重复 `--model-repo alias=owner/repository` 批准目录外模型；仅允许严格HuggingFace两段ID，拒绝本地路径、URL、revision、CLI参数和覆盖bundled/default别名。该映射只由节点启动参数配置，不能从HTTP修改。大模型需要operator确认Mac内存、磁盘及下载资源。

在backend目录使用具有云端依赖的Python运行节点服务，Rapid-MLX二进制可来自另一独立venv：

```sh
.venv-cloud/bin/python -m app.cloud.mlx_node_server init \
  --root /absolute/private/mac-node --hostname mac-node.example.com
.venv-cloud/bin/python -m app.cloud.mlx_node_server serve \
  --root /absolute/private/mac-node --host 0.0.0.0 --port 18443 \
  --inference-port 18081 --model qwen3.5-4b \
  --binary /absolute/private/venv/bin/rapid-mlx
```

`init` 生成私有 token/tls.key/tls.crt（0600，目录0700），不打印值。默认serve仅loopback；开放网络需适当防火墙/可信operator网络，TLS证书SAN匹配配置URL。默认缓存预算256MB、最多4个原生并发、默认输出8192，控制平面另执行账号月额度及并发限制。HF模型缓存仅位于指定节点根目录，不读取宿主默认HF缓存。stop/uninstall只管理节点自身Process；uninstall保留operator安装的二进制。

管理员在用户行“专属Mac节点”配置，API为 `GET/PUT /api/cloud/admin/users/{user_id}/mlx-node`。PUT `{url,enabled,token?,certificate_pem?}`；第一次需要Token+leaf PEM，后续省略/空字符串保留已有值。禁用保留加密凭据。GET只返回configured/enabled/url/token_configured/certificate_fingerprint，不返回令牌或PEM。节点origin与证书fingerprint都唯一，防止给多个用户分配同一节点。URL是固定HTTPS origin，不接受请求级覆盖；只信任所配置非CA leaf并验证hostname，TLS握手通过后才发送Bearer，不跟重定向、不继承环境代理。

节点认证 `GET /rapid-mlx/catalog` 返回操作者批准的alias/repo；中央在同一用户固定pin连接中取catalog与实际 `/v1/models` 求交，发现新映射并在额度预占前拒绝未批准/未运行模型，不使用静态目录过滤未知的已批准模型。升级中央API时须同步更新节点服务以提供该catalog协议。

原MLX界面经 `/api/cloud/internal/mlx-node/rapid-mlx/*` 转发 status/start/stop/cached/remove/uninstall。原 `RapidMLXProvider` 使用中央 `/api/cloud/internal/mlx-node/v1`，实际推理再次由中央SDK预占和结算，`source=mlx-node`，receipt标注 `mac_node_proxy` 并保留完整原生usage JSON。该专用Provider跳过runtime observer以避免重复计量。节点未配或未就绪时，状态/错误如实报告不可用。

## 验证与证据

`tests/test_cloud/test_mlx_nodes.py` 覆盖SQLite/PostgreSQL、管理员+CSRF+专属绑定、真实TLS socket/pin、错误证书不发送Bearer、实际OpenAI SDK函数与receipt、外部进程端口不接管、私有缓存删除、原MLX接口适配与中央额度。供应商MockTransport仅用于协议测试，不代表真实推理。

真实节点验收脚本使用全新私有验证数据库，不修改运行中用户库：

```sh
PYTHONPATH="$PWD" .venv-cloud/bin/python scripts/verify_mlx_node.py \
  --node-root /absolute/private/mac-node --node-url https://mac-node.example.com:18443 \
  --model qwen2.5-0.5b --output /absolute/private/real-evidence.json
```

须先真实start并ready。它要求实际算术回复5、原始usage进入中央账本、额度归零下一次HTTP429。脚本不输出令牌。测试DB的ASGI控制平面与生产HTTP/完整工作台验收分别记录，不能互相替代。现场真实证据位于工作区 `assessment/mlx-node-real-evidence.json`。

## Linux租户内Ollama

完整引擎安装 `PrivateOllamaManager`，原Ollama setup/start/stop、模型库/pull/delete/info/warmup和配置界面沿用。安装和模型目录均在tenant私有state中；无需root或写/usr/local，镜像提供zstd。二进制仅从官方GitHub发布HTTPS下载，逐跳检查origin，不继承环境代理；下载记录SHA256和来源，不冒充已核对供应商签名。tar解包先检查每项路径/链接/设备，再使用Python data filter，stage完成后原子发布。子进程只接收私有HOME/cache、loopback端口和资源环境，不继承CLOUD能力令牌或vendor Key。停止/卸载只作用于本manager的Process和私有目录。 引擎重启沿用原ollama_auto_start默认开启及tenant私有.env显式关闭配置，不再强制覆盖为False；已有last_model时保留原空prompt预热行为。

Ollama官方最新ARM64二进制包本次HEAD约1.60GB，安装需另留足展开空间。默认最多2GB压缩包、10GB解压、1个原生并发/1个驻留模型、1分钟keep-alive。用户选择模型前需要有足够的tenant磁盘/内存；模型下载不按Token额度收费。Ollama原Agent请求由tenant-provider observer计量；用户主动用Bash直接调用其私有loopbackAPI属于自有provider可观测边界，不能宣称可对所有原生脚本硬计量。

节点的实际Python/MLX/vision依赖必须与白名单模型架构匹配。现场0.7.26文本venv及0.5B仅用于低资源真实验收，不代表全部视觉/大模型都已在该机器验收。完整Agent14k上下文下0.5B算术回答错误，真实链路和settled账本仍可核对；模型质量与软件连通性分开记录。

实际Linux验收：独立UID10001、只读rootfs、1.5GiB内存的租户，经原setup/pull接口完成Ollama0.40.16与qwen2.5:0.5b安装；原provider真实算术返回5，中央tenant-provider账本结算34 tokens。节点进程无CLOUD/vendor秘密环境。原完整Agent同样完成SSE和11,339 tokens真实结算，但0.5B小模型在完整上下文中算术答错；CPU首次prefill超过5分钟，原服务超时后缓存继续完成。该次原SDK隐式重试发生在同一wrapper内，账本仅记录最终成功回执；最终实现关闭owned provider的SDK自动重试，使外层processor重试也分别准入/保留未知attempt。正式任务需要按租户资源选择更强模型。详见assessment中的真实运行证据。

## 原Local兼容服务的Web连接条件

LM Studio、vLLM、llama.cpp等OpenAI-compatible服务，通过操作者配置的云端可达HTTPS反向代理接入个人或共享模型route；认证头由中央安全存储并发送。原桌面的HTTP localhost地址不能原样迁入云端，它在服务器上指向租户/服务器，并不指向浏览器所在电脑。Web服务不会自动访问用户本机，也不透传任意私网地址；端点、TLS和网络可达性须由操作者准备。

最终统一镜像恢复验证：专用已安装账号runtime重启后3.34s恢复Ollama running与已缓存模型，中央新增请求0；Mac节点以operator自定义别名映射已缓存0.5B，5.11s实际启动就绪，中央固定TLS pin/catalog交集与原工作台模型列表相符。此阶段只验证恢复/管理，不追加聊天推理。公开证据见 [Ollama恢复](cloud-parity/ollama-live-evidence.json) 与 [Mac节点映射](cloud-parity/mlx-live-http-evidence.json)。
