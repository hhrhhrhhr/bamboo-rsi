# Bamboo RSI Core

独立于原神、BetterGI 和 Decision 的递归优化控制器。当前提供可运行的真实仓库检索适配器，以及现有原神对照服务的只读状态桥接。**这不表示已经验证了任意游戏迁移，也不表示原神已通过晋级。**

内核使用 Python 3.11+ 标准库，MIT 授权。它不导入 `startlux.scenarios.genshin`；现有原神服务的冻结代码、14 游戏日门槛及已登记对照不会被替换。

## 闭环

```text
读取已接受版本 → 注册未暴露的验证任务 → 收集父版本训练结果
→ 优化器提出文件替换 → 验证允许范围和父文件哈希
→ 两次构建一致性检查 → 独立执行父版本与候选
→ 重算门槛、核验原始工件 → CAS 晋级或保留拒绝记录
→ 下一轮读取新的已接受版本
```

- `Adapter` 负责任务、执行器、构建、训练反馈和独立结果核验；`Proposer` 只返回结构化文件替换。添加其他任务时实现这些接口，不需要修改控制器。
- 版本绑定任务域、父版本、文件内容和评测契约。候选不能修改评测器、任务定义、数据集、预算或门槛。
- 验证任务在调用模型前登记；验证问题、正确答案和门槛不进入模型上下文。已使用或中断的验证批次不重复当作新证据。
- 晋级要求完整且正确的候选输出，并达到预定的实际工作量改善或修复父版本失败。未知结果、未知代价、合成来源、人工介入和篡改工件都不能通过。
- 晋级记录绑定评测摘要和原始结果工件；读取接受指针时重新核验。回滚保留版本、失败及预算历史。
- 模型调用前持久占用额度。失败、超时和进程中断不退款；不因重启清零。可连接现有项目的共享额度。
- 未完成作业阻止继续调用模型。`resolve-interrupted` 明确关闭未知执行，或恢复已提交指针的作业状态，不重放调用。

当前通用门槛适用于**可重复的配对任务**。原神委托不可重复，仍使用现有的未来 14 游戏日、基线/候选各 7 天的独立队列评测。不能用本地检索结果替代原神对照，也不能用这个通用配对门槛对同日游戏进度晋级。

## 运行真实本地实验

在仓库根目录运行（输出目录应位于 `.runtime` 或仓库外）：

```powershell
$env:PYTHONPATH = (Resolve-Path rsi-core/src).Path
platform/.venv/Scripts/python.exe -m bamboo_rsi.cli snapshot-repository --repository . --destination .runtime/search-dataset
platform/.venv/Scripts/python.exe -m bamboo_rsi.cli run-repository --dataset .runtime/search-dataset --home .runtime/search-rsi --rounds 2 --max-calls 2
platform/.venv/Scripts/python.exe -m bamboo_rsi.cli status --home .runtime/search-rsi
```

也可 `pip install -e ./rsi-core` 后使用 `bamboo-rsi`。`--codex`、`--model`、`--proxy` 可指定本机已有 Codex 配置；无需在源码填写 API Key。后端沿用 [Codex 非交互执行](https://developers.openai.com/codex/noninteractive) 的结构化输出流程，禁用 shell 与多代理工具，不将游戏凭据传给提案进程。原始调用日志只保存在私有实验目录。

仓库任务使用 Git 跟踪的真实文本文件快照，完成字面量检索。初始参考算法逐查询读文件；候选只能改变批次缓存和整文件预筛选策略。独立穷举实现核对每一个文件和行号；主指标是实际读取字节数加字面量比较字符数，耗时另行记录。**这是受限策略优化的本地基准，不是任意代码自主改写或第二个游戏实机验证。**

如果第一轮晋级后已无证据支持继续改进，下一轮允许 `no_change`；它仍必须记录实际继承的父版本，不能为了凑代数制造改进。成功只对当前任务域、快照及验证设计成立。

目录包括 `versions/`、`jobs/<id>/plan.json`、`context.json`、`proposal/`、双次构建、两侧原始输出、`evaluation.json`、`releases/`、`rollbacks/` 和持久调用计数。程序版本、评测器或数据集改变后必须使用新实验目录，保留原实验。

```powershell
bamboo-rsi resolve-interrupted --home .runtime/search-rsi --job <job-id>
bamboo-rsi rollback --home .runtime/search-rsi --expected-version <version-hash> --reason "verified regression"
bamboo-rsi genshin-status --home .runtime/genshin-home
```

## 接入另一个场景

适配器至少需要给出：任务域身份、可修改文件集合、冻结的执行/评测指纹、初始版本、独立训练反馈、预登记任务、可复现构建和可核验结果工件。不可重复的游戏还需要单独的跨时段队列设计、资源预算、输入停止确认及录像；当前只读原神桥不会自动创建这些能力。

模型后端通过 `Proposer.propose(context, destination)` 接入，可以替换 Codex。游戏中的动作模型和执行器仍由场景适配器负责，和提出补丁的模型是不同职责。

控制器提供工程上的文件范围与评测隔离，**不是执行任意恶意候选代码的操作系统沙箱**。仓库示例只解释受限 JSON，不执行模型生成的 Python。接入可执行代码时，适配器必须提供受限构建/运行环境和独立证据收集。

## 验证

```powershell
platform/.venv/Scripts/python.exe -m pytest rsi-core/tests -q -c rsi-core/pyproject.toml
```

单元测试使用临时文件与脚本化提案器，明确不代表真实模型或游戏晋级。真实模型实验须另保留调用事件、实际工件、门槛及下一轮父版本证据。
