# 接口与配置约定

状态：提案。JSON Schema 只验证设计样例的结构，不证明模型能玩游戏，也不能替代动作来源与观测权限的运行时检查。

## 数据对象

| 对象 | 必需信息 | 写入方 |
| --- | --- | --- |
| GameSpec | 游戏版本、时钟、观测权限、动作语义、原分、目标、终止/截断条件 | 平台与游戏适配器；GEPA 只读 |
| GameBrief | 背景、规则、目标、注意事项、攻略、出处、适用版本、事实/假设标签 | 首局前的 KnowledgeBuilder；局后产生新版本 |
| HarnessBundle | 绑定的 GameSpec、文字策略、特征白名单、记忆配置、知识摘要 | LLM 初始化及 GEPA 候选编译器 |
| EpisodeSpec | 回合类型、场景、种子、运行版本、模型、预算、数据分割 | 调度器/独立评测器 |
| DecisionRequest | 请求 ID、观测序号、当前状态、完整候选、指令、截止时间 | 固定运行器 |
| DecisionReceipt | provider、返回模型版本、选择、原始响应哈希、候选哈希、耗时 | provider 适配器；不能由候选生成 |
| ActionEvent | 回执 ID、请求动作、实际动作、引擎输入序号、执行前后时间 | 固定执行器 |
| EpisodeResult | 原分、目标指标、结果原因、基础设施状态、制品/轨迹引用 | 环境裁判与记录器 |
| OptimizationJob | 触发回合、父版本、固定训练/选优清单、预算、run_dir、状态 | 局后调度器 |
| PromotionRecord | 新旧版本、评测证据、门槛版本、expected_parent、结论 | 独立发布器 |

## GameAdapter：不仅仅是 Gymnasium wrapper

```text
describe() -> GameSpec
reset(EpisodeSpec) -> ObservationSnapshot
observe() -> ObservationSnapshot
enumerate_actions(snapshot) -> ActionCatalog
execute(selected_action, decision_receipt, input_sequence) -> StepResult
finalize() -> EpisodeResult
close()
```

`enumerate_actions` 只能依照已冻结的动作接口和游戏硬规则枚举，不允许使用求解器或策略评分删选。目标游戏允许的坏动作仍然保留。`execute` 仅做既定映射。环境本身若忽略反向/不合法按键，须同时记录 requested/applied，不能把引擎转换描述成模型主动选择。

观测以不可变快照保存；绝不直接持有引擎随后还会修改的 numpy view。不得把 `info`、随机数内部状态、封存种子、未来事件或不可见对象整包放进 `state`。针对结构化观测与视觉观测，使用不同 GameSpec ID。

`reset` 用于评测种子的随机流与模型决策随机流分别记录；模拟器确定性不意味着托管模型也确定。

## 决策请求与回执

内部请求概念示例（不是任何后端可直接发送的 API JSON）：

```json
{
  "request_id": "episode-0001:step-0007",
  "observation_revision": 7,
  "runtime_version_id": "resolved-at-start",
  "state": {"board": "immutable-visible-board", "memory": "bounded-history"},
  "action_catalog_id": "snake-four-directions-v1",
  "candidate_ids": ["up", "right", "down", "left"],
  "instructions": "compiled-from-frozen-harness",
  "deadline_monotonic_ms": null
}
```

转换成 StartLux 时，provider 将候选写入 `questions.primary_action.criteria`，并传入 `type=choice`、`instructions` 与 `state`。Jev 官方 API 与 OpenRouter 代理是不同 transport 配置，不按名字猜 URL。

回执至少返回：`request_id`、`observation_revision`、`provider`、`requested_model`、`returned_model`、`choice_id`、`candidate_hash`、`response_hash`、`elapsed_ms`、`validation_status`。概率与 confidence 按 provider 实际能力可选；存在时必须是有限数值且符合协议，不得用 confidence 冒充胜率。

执行前校验：

1. 回执确实来自玩家进程中的 provider 调用，不能由 LLM 或特征配置构造。
2. 请求、候选集、观测序号、模型及 Harness 版本均一致。
3. choice 属于对应完整候选集；不以“第一个选项”兜底。
4. 实时模式未超过截止时间，动作输入序号未执行过。
5. 若动作被引擎拒绝，未提交的语义记忆更新同时撤销；实际观察历史仍记录拒绝事件。

只有一个动作时，可作为固定游戏约束显式记录，不能由 Harness 策略裁剪产生。即使后端对单选题优化成直接返回，这也不构成模型具有策略能力的证据。

## Harness Schema 之外的语义校验

`harness.schema.json` 约束结构；编译器还必须校验：

- `game_spec_id` 与 `action_catalog_id` 匹配；环境、计分器和动作参数域不在候选中。
- 每个 `feature_id` 在该游戏已审查的事实算子白名单中，不能引入最佳动作分数或搜索器。
- 模板引用的字段都属于许可观测；文字中不得插入已知封存场景答案。
- 时长若为固定游戏动作语义，不允许 Harness 改写；若为可变参数，则必须进入模型 Choice 选项。
- `strategy_notes` 是供模型阅读的文字，不执行；`history_window` 只控制有界历史观察，不能调用 LLM 总结。
- `phase_labels` 如启用，阶段转换由决策模型选择，不由脚本判断“该进攻了”。

## 计分与终局

`raw_score` 总是注明来源；`search_objective` 与其分别保存。主分数、辅助奖励、可靠性、成本不共享同一个模糊的 score 字段。

`termination_reason` 建议枚举：`win`、`death`、`board_full`、`time_limit`、`step_limit`、`no_progress_limit`、`provider_failure`、`environment_failure`、`invalid_output`、`manual_stop`。同时保存 Gymnasium 的 `terminated` 与 `truncated`，以固定映射解释。

每个游戏输入有固定步骤/时间预算，防止蛇绕圈、方块拖延、马里奥重复刷物品。非完整回合仍保留最后一次确认的原分及失败原因，发布评测按预先规则处理，不选择性删除。

## GEPA 接入契约

`EpisodeSpec` 是 GEPA 的 DataInst；candidate 为文本组件字典。`GameGEPAAdapter.evaluate` 在新的隔离环境中运行冻结候选，返回逐回合 outputs/scores/trajectories。`make_reflective_dataset` 生成带证据引用的组件反馈，必须包含触发本项任务的回合，不能依赖随机采样碰巧选中。

全量轨迹留存；给 LLM 的上下文明确标注已包含的失败窗口和省略范围。初版允许它只看摘要与关键片段，不能把这描述为读过完整轨迹。

每个正式游玩回合是一项独立优化作业；该作业自己的评测局使用 `purpose=search_eval/promotion_eval`，不会再创建优化作业。`purpose=sealed_test` 属于冻结版本后的独立里程碑审计，不是每局优化的子局；其结果不回传反思或选版。新优化作业开新 `run_dir`；崩溃恢复只恢复相同数据和配置。

搜索预算包含主模型调用、教练调用、重试、评测局数与总时间。达到停止条件但尚未产生候选必须记录明确状态，不能静默宣称“本轮优化完成”。
