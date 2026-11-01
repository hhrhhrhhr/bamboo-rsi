# 信贷账户客服：模拟数据与话术 RSI

该独立模块根据 aichatcore 的会话与话术字段生成 **360 段模拟会话**，通过现有 `rsi-sdk`、`rsi-core` 和 `rsi-http` 执行：数据入队 → 预登记对照 → 自动学习候选 → 可复现构建 → 独立核验 → 晋级 → 从已接受父版本继续优化。

**这是离线模拟验收。** 没有真实客户、外呼、短信、支付或生产发布。客户反馈由固定情景规则生成，回复由受限策略执行器输出，当前模型调用次数为零。不能将结果解释为真实回款提升、模型权重训练、完整语言理解能力或法律合规认证。

## 与 aichatcore 的对应关系

已检查的源版本与文件承诺见 [source-contract.json](source-contract.json)：

| aichatcore 对象 | 本模块产物 |
| --- | --- |
| `SessionTurnEntity` | `history/session-turns.jsonl`：保留 `sessionId`、`roundId`、`turnIndex`、`userQuery`、`botReply`、`referenceScriptId`、流程步骤、结构化输入输出等；数据库自增 `id` 不伪造 |
| `PlaybookSessionController.TurnsQueryResponse` | `history/session-turns-api.jsonl`：按会话分组的 `callId/sessionId/turns` 结构 |
| `StructuredOutput` | `output/step/utterId/handoff`，`END` 或 `HUMAN` 后不会继续编造同场对话 |
| `PlaybookDO` / `UtteranceDO` | `accepted/aichatcore-bundle.json`：剧本 Prompt、话术、步骤、转人工字段 |
| 原有算法预检/后检与路由 | 独立的固定约束、路由策略及核验器；本模块未修改 Java 服务 |

导出仅保证所说明的字段映射；不是可直接执行的 SQL、数据库迁移或 Java 生产配置。新增 `routingPolicy` 需要业务侧适配，并进行会话版本固定、缓存刷新及发布回滚。没有复制 aichatcore 的实现代码；保留源仓库用于检查。原 Java 项目依赖企业 Maven 父工程、数据库、Redis 和外部算法/模型服务，本次没有连接这些环境，也没有声称运行了它的 Spring 应用。

## 数据和优化目标

三批各 120 个不同模拟客户，其中 60 段用于训练、60 段用于验证。十类情景包括明细查询、已还款、金额异议、困难求助、支付渠道、预约联系、转人工、拒绝联系、错人、未核身。新批次使用变化后的表达，例如“还过了 → 银行已扣 → 转账成功”。所有客户标识、金额和逾期天数均为构造数据。

训练和验证按整个客户/会话隔离，使用不同的表达模板族。每轮先登记新验证快照及 12 个配对组，再把该批及此前的训练反馈交给优化器；未来批次和验证对话不出现在提案上下文。模拟数据仍存在模板依赖，不能代表自然对话分布。

当前优化器从训练记录自动统计重复、意图特有的汉字短语，并按观察到的反馈选择固定话术库中的版本。保留父版本已接受的短语，生成完整 `collection-policy.json` 替换；没有写死“第几轮必须选哪个候选”。相同反馈不能支持新修改时返回 `no_change`，不能为了增加代数造一个变化。

候选只能改意图路由词和允许的话术选择。拒绝联系、错人、未核身、明确转人工的处理不在可修改范围；账户身份核验取可信输入字段，不能因为客户说“核验过了”就视为已核验。话术不以恐吓、联系亲友单位、借新还旧、私账转账或未经授权的减免承诺提高得分。

核验器从原始 `botReply`、结构化动作和独立情景标签重算：

- **处理损失**：路由错误、缺少必要解释、错误转接；要求候选至少减少 10%。
- **正确处理数**：满足该模拟情景的全部要点；不能倒退。
- **约束事件**：不得增加，且候选必须为零。
- **配对诊断**：12 组差值的单侧符号检验，阈值按批次 `0.05/(n*(n+1))` 分配。因为存在共享模板依赖，它仅作为固定工程门槛，不能声称获得真实客户总体的统计置信度。

候选不可修改情景标签、核验器、固定话术库、预算、指标或门槛。无法解决问题时，正确转人工可以是合理服务结果；没有“模拟客户说会还款 = 已回款”的指标。

## 运行

仓库根目录：

```powershell
platform\.venv\Scripts\python.exe -m pip install --no-deps -e ./collections-lab
bamboo-collections demo --destination .runtime/collections-demo-new
```

`demo` 用回环地址启动临时 `rsi-http` 控制 API，逐批入队，再经 HTTP 创建实验、运行三轮、查询结果。重复相同幂等键不会重复执行。运行完成后停止临时服务。凭据只在进程环境中随机生成，不落入输出或日志。

主要产物：`report.html`、`report.json`、`comparisons.json`、`history/`、`accepted/`，以及 `control/experiments/collection-shadow/workflow/` 下的预算、提案、原始对话、对照结果、晋级与版本血缘。

后续新批次可独立进入同一个 intake 和 workflow：

```powershell
bamboo-collections generate --destination .runtime/collections-more --start-batch 4 --batches 1 --seed 7
bamboo-collections ingest --intake .runtime/collections-demo-new/intake --snapshot .runtime/collections-more/wave-4.json
bamboo-collections step --intake .runtime/collections-demo-new/intake --home .runtime/collections-demo-new/control/experiments/collection-shadow/workflow
```

第四批复用已学会的表达时，合理结果是 `no_change`，仍保留提案预算消耗。`step` 只有显式调用时执行；没有安装新的定时任务。12 次提案、288 次执行的持久预算不会因失败或进程重启退款。

## 增量数据的冻结方式

不可变的 `stream.json` 标识数据源；每次 `ingest` 写入内容寻址快照和带前驱哈希的接收记录，再更新接收进度。同一批相同内容幂等，相同批 ID 不同内容拒绝；跨批客户重复拒绝。

集成指纹冻结执行器、核验器、评测器、话术库和数据源身份。**整个未来数据源不是一个永不变化的文件**：每轮计划独立冻结本轮快照哈希和会话 ID，训练上下文另存训练快照哈希。新批次到达不会改变旧对照或旧版本的验收，修改旧快照会失败。旧验证数据永不自动回流到训练。此设计无需修改现有 SDK，也不会修改原神等游戏的冻结试验。

当前入口明确只接受 `synthetic_fixture`。接入真实业务前，应通过 aichatcore 的只读会话查询接口导出脱敏记录，提供授权范围与独立人工/业务结果标签，并另外建立真实回放或影子评测集成；不能只把模拟标签改成生产标签就宣称完成验收。生产回款需要独立账务证据，真实话术发布需要业务自己的发布控制。

## 回归

```powershell
platform\.venv\Scripts\python.exe -m pytest collections-lab/tests -q -c collections-lab/pyproject.toml
platform\.venv\Scripts\python.exe scripts/check_public_tree.py
```

测试覆盖完整晋级/继承、增量数据冻结、模板和客户隔离、固定约束、重复导入、快照与对话篡改、失败预算、无改进不晋级和回滚留痕。原创模块采用 [MIT](LICENSE)。
