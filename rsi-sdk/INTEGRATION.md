# 业务接入契约 v1

先定义可被独立核验的目标。任务的执行器完成工作，核验器判断真实结果；优化器只负责提出受限修改。一个业务集成由三个对象组成：

```python
from bamboo_rsi_sdk import Integration, Workflow
from bamboo_rsi_sdk.evaluation import PairedEvaluation

def create(config):
    return Integration(
        adapter=YourTaskAdapter(config),
        verifier=YourResultVerifier(config),
        evaluator=PairedEvaluation(minimum_cases=3),
    )

# proposer 提供 propose(context, destination)，返回结构化文件替换。
# result = Workflow(private_home, create(config)).step(proposer)
```

上面的 `YourTaskAdapter` 与 `YourResultVerifier` 是业务实现占位名称。可运行的完整实现见 [订单对账](src/bamboo_rsi_sdk/adapters/orders.py)；第二个场景见 [仓库检索](src/bamboo_rsi_sdk/adapters/repository.py)。两者无需继承协调器或版本存储类。

## 必须实现的接口

| 对象/方法 | 作用与边界 |
| --- | --- |
| `adapter.describe() -> TaskSpec` | 域 ID、目标、可改文件、主/辅助指标、额度、单次期限、能力声明、证据范围。契约不能随候选变化。 |
| `adapter.fingerprint() -> str` | SHA256 身份，必须覆盖执行实现、依赖、完整数据集/服务版本及影响行为的配置。 |
| `adapter.initial_files() -> dict` | 初始完整候选包；只允许修改 `editable` 中的既有文件。 |
| `adapter.training_context(version) -> dict` | 历史训练反馈的显式投影；不返回凭据、验证集、正确答案或验证结果。 |
| `adapter.plan(used_cases) -> Plan` | 全新验证任务与固定槽位顺序；先登记，再调用提案模型。 |
| `adapter.build(version, destination) -> Build` | 只在目标目录写产物，返回全部相对路径及 SHA256。父/候选各运行两次，必须逐文件一致。 |
| `adapter.execute(version, case, context) -> dict` | 执行该版本和任务，将原始证据写入 `context.directory`，返回含 `origin`、`assisted` 的回执；定期调用 `context.checkpoint()`。 |
| `verifier.fingerprint() -> str` | 覆盖独立结果判定逻辑、依赖和外部裁判身份。 |
| `verifier.verify(version, case, receipt, directory) -> Measurement` | 从原始证据重算正确性及所有指标；不能修改证据，也不能仅信任执行器自报成功。 |

`TaskSpec`、`Case`、`Plan`、`Build`、`Measurement`、`Limits` 和 `Metric` 均可从 `bamboo_rsi_sdk` 导入。对象必须能转成有限 JSON；金额等业务值应使用明确单位，例如整数分，不能靠浮点数模糊相等。

示例任务描述：

```python
from bamboo_rsi_sdk import Limits, Metric, TaskSpec

spec = TaskSpec(
    id="acme.invoice_pipeline",
    objective="准确处理所有发票并减少处理工作量",
    editable=("policy.json",),
    metrics=(Metric("work_units", reduction=0.10, regression=0.0),),
    primary_metric="work_units",
    limits=Limits(proposals=4, runs=48, run_seconds=30),
    origins=("local_execution",),
    capabilities=("local_files",),
    evidence_scope="offline_reference",
)
```

能力声明用于审计与集成选择，不会自动授予系统权限或限制网络。业务执行器应把不可逆的外部操作分成独立、可审计的步骤；在未知结果时先对账，不能因进程重启就再次付款或发消息。

## 替换评测策略

默认指标按所有配对任务求和；`Metric.direction` 支持 `minimize`、`maximize`。`reduction` 表示主指标所需相对改善幅度，`regression` 表示每个指标允许的相对回退。候选必须全部正确；修复父版本失败也必须满足成本回退约束。

其他业务可实现 `EvaluationPolicy` 的三个方法：

```text
fingerprint() -> 固定评测代码与参数的 SHA256
validate_plan(spec, plan) -> 核验样本、分组、顺序、设计及统计要求
decide(spec, plan, measurements) -> {passed: bool, reasons: [...], ...}
```

协调器不会假设所有业务都使用相同指标，但仍要求两组证据、固定分配、未受协助的原始执行、有限指标、独立核验和完整覆盖。分步 `prepare/advance/finish` 可交给外部调度器；对不可重复业务、跨日游戏或长期线上 A/B 实验，业务方还需实现适当的队列计划和独立评测器。SDK 没有把已有游戏的对照要求迁移为三对样本。

## 插件注册

插件自己的 `pyproject.toml`：

```toml
[project]
name = "acme-rsi-integration"
version = "0.1.0"
dependencies = ["bamboo-rsi-sdk==0.1.0"]

[project.entry-points."bamboo_rsi.integrations"]
"acme.invoice_pipeline" = "acme_rsi.integration:create"
```

安装受信任插件后，`bamboo-rsi-sdk list` 显示 ID，`check --integration ... --config ...` 核验其身份。重名、覆盖内置 ID、工厂返回的任务 ID 不匹配都被拒绝。插件应把自己的配置路径解析为绝对路径，并将会改变行为的配置纳入指纹；凭据通过运行时私有存储读取，不能进入模型上下文或候选文件。

仓库附带可安装的 [order_plugin](examples/order_plugin/pyproject.toml)，可运行 `python -m pip install -e ./rsi-sdk/examples/order_plugin`，然后通过 `example.invoice_reconciliation` 接入。它复用对账参考算法，演示独立包的注册与代码指纹，不代表新增了一种已验证的真实业务；实际接入时替换业务执行和独立判定逻辑。

## 对账数据集格式

`manifest.json` 的 `files` 覆盖所有 CSV 的相对路径及 SHA256；每个训练/验证案例使用独立文件和唯一 ID。验证至少六个案例，以便首轮使用三个、下一轮使用另三个。示例：

```json
{
  "schema_version": 1,
  "scope": "offline_reference",
  "files": {"train-orders.csv": "<sha256>", "train-payments.csv": "<sha256>"},
  "training": [{"id": "train-1", "orders": "train-orders.csv", "payments": "train-payments.csv"}],
  "validation": []
}
```

上例仅展示结构，必须补齐验证文件、案例及真实哈希才能运行。`demo-orders` 会生成完整示例。

- 订单表头：`order_id,currency,amount`，订单 ID 唯一。
- 支付表头：`payment_id,order_id,currency,amount,status`，支付 ID 唯一。
- 金额使用非负两位小数字符串，币种三位大写字母。当前示例不处理负退款、汇率或费用分摊。
- 支付状态仅为 `captured`、`pending`、`failed`；只累加已捕获支付。
- 同订单多笔支付全部累加，保留超额差异；没有匹配订单/币种的已捕获支付单独列出。
- 工作量为实际访问支付记录及索引查找次数，不是生产耗时或业务收入。正确性由逐行独立穷举确认。

真实业务数据应放在忽略目录，使用真实的未暴露验证批次并独立审查评测设计后声明 `business_validation`。字段声明本身不证明数据来自生产。

## 接入验收

先运行业务自己的执行与核验测试，再用统一协调器检查：错误输出被拒绝、指标回退被拒绝、篡改原始证据无法发布、失败调用不退款、重启不重复未知操作、下一轮读取晋级父版本、回滚保留历史。生产就绪还需要具体环境的权限、隔离、性能和真实数据验收。

现有 `rsi-core`、原神、LoL、王者试验继续使用旧入口。不要修改冻结文件来让旧试验接受新 SDK 的结果；迁移必须建立新契约并保留原来的样本与预算账目。
