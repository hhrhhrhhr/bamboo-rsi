# Bamboo RSI SDK

面向业务任务的通用优化接入层。它复用 `rsi-core` 的内容寻址版本、CAS 晋级、回滚和证据存储，提供统一任务契约、执行器、独立核验器、可替换评测策略和插件注册。Python 3.11+，运行依赖仅为本仓库 MIT `bamboo-rsi-core`。

首版包含两个使用同一 `Workflow` 的适配器：

| 接入 ID | 执行内容 | 可优化对象 | 独立核验 |
| --- | --- | --- | --- |
| `business.order_reconciliation` | 本地 CSV 订单与已捕获支付对账 | 逐订单扫描或建立批次索引 | 逐条穷举、精确到分、币种隔离、未匹配支付及工作量 |
| `repository.literal_search` | 固定仓库快照的字面检索 | 批次缓存与预筛选 | 全量匹配、文件行号和实际读取/比较工作量 |

业务示例读取文件并生成核对结果，没有支付、退款或生产数据库写入接口。参考演示使用明确标注的示例数据与脚本提案器，不代表真实模型自主改进或生产业务验收。

## 安装和第一个完整流程

建议使用独立虚拟环境，避免改变已有游戏运行依赖：

```powershell
py -3.11 -m venv .runtime/sdk-venv
.runtime/sdk-venv/Scripts/python.exe -m pip install -e ./rsi-core -e ./rsi-sdk
.runtime/sdk-venv/Scripts/bamboo-rsi-sdk.exe list
.runtime/sdk-venv/Scripts/bamboo-rsi-sdk.exe demo-orders --destination .runtime/sdk-orders-demo
```

`demo-orders` 要求新的输出目录，自动执行真实本地 CSV 计算，验证第一轮候选，再让第二轮读取接受版本并返回 `no_change`。`demo-report.json` 明确包含 `real_model_calls: 0`、`real_business_acceptance: false` 和 `scope: offline_reference`。示例用于验证接入和状态机，不可用作生产晋级证据。重复运行会拒绝覆盖旧证据。

开发环境也可以不安装，直接配置本次进程的模块路径：

```powershell
$env:PYTHONPATH = "rsi-sdk/src;rsi-core/src"
platform/.venv/Scripts/python.exe -m bamboo_rsi_sdk.cli demo-orders --destination .runtime/sdk-orders-demo
```

## 接入自己的业务

完整步骤、契约和可复制的插件注册见 [INTEGRATION.md](INTEGRATION.md)。业务方提供执行和真实结果判定，优化器只提出允许范围内的候选。

```text
TaskSpec + TaskAdapter + ResultVerifier + EvaluationPolicy
                         ↓
统一 Workflow：登记 → 提案 → 双次构建 → 执行 → 独立核验 → 晋级/拒绝
                         ↓
accepted version → 下一轮父版本；失败记录、调用预算和回滚历史保留
```

评测数据在模型调用前登记；给提案器的上下文只有任务目标、已接受文件与训练反馈。验证输入、正确答案、晋级阈值不进入提案上下文。适配器必须正确划分训练与验证数据，不能把未来验证记录混入训练反馈。

任务契约固定主指标、辅助指标、方向、改善幅度、允许回退、证据来源、时间与调用预算。默认 `PairedEvaluation` 支持最小化/最大化指标，要求至少三对完整样本；主指标改善不能掩盖辅助指标恶化，候选失败不能晋级。不同领域可注入独立、冻结的评测器，无需改写 `Store` 或协调器。

## 使用模型提出候选

创建自己的数据集描述，复制 [orders.config.example.json](examples/orders.config.example.json) 到私有目录并填写数据集路径。先检查，再在全新实验目录运行：

```powershell
bamboo-rsi-sdk check --integration business.order_reconciliation --config .runtime/orders-config.json
bamboo-rsi-sdk init --integration business.order_reconciliation --config .runtime/orders-config.json --home .runtime/orders-experiment
bamboo-rsi-sdk step --integration business.order_reconciliation --config .runtime/orders-config.json --home .runtime/orders-experiment
```

`step` 默认调用本机已有的 Codex CLI 提案后端；使用其现有登录，支持 `--codex` 和 `--model`。SDK 复用原有结构化提案检查，不传业务账号密钥。也可在 Python API 中传入任何实现 `propose(context, destination)` 的后端，包括自己的 Jev 提案适配器；模型的动作选择接口与补丁提案接口是不同契约，不能只换 URL 就假定兼容。

对账任务目前只允许修改 `reconciliation-policy.json` 的 `join` 字段，模型不会执行生成的 Python。接入任意程序优化时，由业务构建器定义允许修改的程序包，提供可重现构建和隔离执行环境；不得扩大评测器、权限或凭据的可修改范围。

## 分步执行与恢复

长任务可以由已有任务调度服务调用以下入口，SDK 不自行创建系统定时任务：

```text
prepare --integration <id> --config <path> --home <path>
advance --integration <id> --config <path> --home <path> --job <id>
finish  --integration <id> --config <path> --home <path> --job <id>
status  --integration <id> --config <path> --home <path>
recover --integration <id> --config <path> --home <path> --job <id>
rollback --integration <id> --config <path> --home <path> --expected-version <hash> --reason <text>
```

`prepare` 完成登记、一次提案与父/候选双次构建；`advance` 只执行一个预登记槽位，`finish` 在证据齐全后重验全部结果并尝试晋级。主程序可重启，继续相同作业；新的提案不能越过尚未完成的作业。

执行前持久占用槽位和额度。模型失败、执行失败与中断都不退款；恢复不会重发结果未知的外部操作。`recover` 将未知作业标为中断，或恢复已经提交的接受指针。新实验不能被用来绕过同一业务的总预算；需要跨实验的组织配额时，应由业务调度服务提供额外的持久配额管理。

在实验 home 创建 `STOP` 文件可在下一检查点停止。内置 CSV 任务逐记录检查；`RunContext` 的超时是可信执行器的合作式检查，并非操作系统强制中止。外部服务、原生程序或可能挂起的执行器必须由适配器使用独立工作进程和明确的终止协议。

## 版本与证据边界

- SDK 单独存放于 `rsi-sdk/`，没有修改 `rsi-core/src/bamboo_rsi`。原神、LoL、王者的冻结协调器、对照门槛和已有运行目录不会自动迁移。
- 新增业务通过已安装的 `bamboo_rsi.integrations` entry point 注册，配置不会动态导入任意源码路径。安装插件意味着信任其执行器和核验器；这是工程边界，不是恶意插件沙箱。
- 每个实验固定 SDK/核心、Python/平台、任务契约、数据集、执行器、核验器和评测器指纹。变更这些内容需要新实验和明确的迁移记录；没有原地降低门槛的迁移开关。
- 构建绑定完整工件目录，父版本和候选各构建两次并检查一致性。内置适配器执行版本中经过校验的受限 JSON；有编译产物的业务适配器还必须绑定并运行准确产物。
- 发布时重新读取原始工件、执行绑定、独立测量与评测结果；读取已接受版本时仍然核验。哈希和本机租约不防恶意管理员同时重写整套记录。
- 接受指针表示本任务契约下的内部晋级，不会自动部署生产、发送消息、扣款或改变外部账户状态。生产发布、幂等键、资源权限和必要的业务审批由具体集成负责。
- `offline_reference`、`local_benchmark` 与 `business_validation` 的证据范围分别记录；前两者不会转换成真实生产验收。跨领域泛化默认未验证。执行优化不更新模型权重。

## 回归

```powershell
platform/.venv/Scripts/python.exe -m pytest rsi-sdk/tests -q -c rsi-sdk/pyproject.toml
platform/.venv/Scripts/python.exe -m pytest rsi-core/tests -q -c rsi-core/pyproject.toml
platform/.venv/Scripts/python.exe scripts/check_public_tree.py
```

测试包括独立插件、两个领域复用同一协调器、精确对账、候选越权、未知结果、工件篡改、指标回退、故障预算、中断恢复、重新打开实验、父版本继承和回滚。离线测试中的脚本提案器不算真实模型参与。
