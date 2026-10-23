# 秘境 RSI 离线准备工具

独立读取 BetterGI 秘境目录和 Bamboo RSI 原始运行证据，生成禁用的目标配置、未来独立对照草案和证据审计报告。标准库 Python 3.11+，原创代码采用 MIT。

**本工具不操作游戏、不消耗树脂、不修改运行中的 Agent、配置、每日对照或晋级指针，也不调用模型。** 当前每日委托的 14 游戏日对照继续由原协调器负责。这里完成的是其他秘境的配置准备和离线评测组件，尚未完成秘境实机注册、执行、晋级接线。

## 目标配置

从本地 BetterGI `GameTask/AutoTrackPath/Assets/tp.json` 读取圣遗物、武器材料和天赋材料秘境名称，并绑定原文件 SHA-256。不复制地图资源进开源包，也不把目录存在当作账号已解锁或能打赢的证明。世界 Boss、地脉不属于本次接入。

`prepare` 要求目标在该目录内；可填写游戏内准确关卡名和推荐等级。未知关卡保留 `null`，配置标为不完整，绝不猜一个名称启用运行。输出中的 `disabled_resin_plan.enabled` 始终为 `false`，只允许原粹树脂。它是供后续审核的嵌套配置，不能直接作为现有 `resin-plan.json` 使用。

```powershell
$env:PYTHONPATH = (Resolve-Path resin-lab/src).Path
platform/.venv/Scripts/python.exe -m bamboo_resin_lab.cli catalogue --assets .runtime/bettergi-flight/BetterGenshinImpact/GameTask/AutoTrackPath/Assets/tp.json --output .runtime/resin-review/catalogue.json
platform/.venv/Scripts/python.exe -m bamboo_resin_lab.cli prepare --home .runtime/genshin-home --assets .runtime/bettergi-flight/BetterGenshinImpact/GameTask/AutoTrackPath/Assets/tp.json --domain "仲夏庭园" --output .runtime/resin-review/target-draft.json
```

以后确认准确关卡后，可用 `--stage`、`--level` 生成新的草案文件。不会修改现有目标，也不会重置预算。输出必须是新文件，不能指向游戏 home 或输入资源目录；报告通常保存到 Git 忽略的 `.runtime`。

目前实机动作包仍使用 `region=Mondstadt` 前置条件，原生树脂会话共用一份固定队伍战斗脚本。因此目录中的其他地区不能据此宣称已接入成功；跨地区前置条件、与目标适配的战斗脚本，以及材料本当天的开放关卡，都列入后续部署核验。默认等级 90 也只是待核验配置，不能替代游戏内识别。

## 原始证据核验

```powershell
platform/.venv/Scripts/python.exe -m bamboo_resin_lab.cli audit --home .runtime/genshin-home --output .runtime/resin-review/audit.json
platform/.venv/Scripts/python.exe -m bamboo_resin_lab.cli review --home .runtime/genshin-home --draft .runtime/resin-review/target-draft.json --output .runtime/resin-review/readiness.json
```

审计保留成功、失败、不明领奖、启动中断及无法读取的目录，不用最后一次成功覆盖之前的失败。独立重算原粹树脂扣减，包括战斗期间自然恢复的情况；核对账号/服务器/游戏日、目标关卡、原始截图哈希和录像哈希。账号标识仅输出作用域哈希，原始图像和密钥不输出。

模型选择核验包含：请求画面绑定、动作目录哈希、Decision 回执、命令所依赖的前置条件、会话/控制代际、终态回执及输入释放。录像须在 Decision 请求前开始，来自游戏窗口且不含音频。录像解码状态来自原生录像器；本工具核验文件一致性，不声称重新解码了视频，也不独立识别截图像素中的奖励。因此这些是对受信采集器记录的证据审计，不是恶意本机程序无法伪造的证明。

`verified_original_resin` 表示原始机器回执通过算术和截图工件核验的数量，不等于该运行无开发者介入。`developer_reported_original_resin` 单独列出与原始轨迹、录像报告绑定的人工核验数量，不把它改写成机器确认。两个数量不能在未排除重叠时简单相加。原始 `resin-report.json` 的错误状态和原因保留在报告里。

目前全部历史树脂记录只能作开发分析。独立实机 enrollment、持续介入覆盖和固定队伍装备证据尚未接入；审计器始终返回 `autonomous_sample_eligible=false`，不会凭“没找到人工记录”推断无人介入。

## 独立评测设计草案

每个秘境单独登记，草案最早开始日为现有每日对照最后一天的次日，且必须是未来日期。当前草案不是真实登记，也不会在该日期自动启动。

- 保持 14 个连续游戏日，父版本/候选交替，各 7 天；同日重复试跑不能凑样本。
- 固定账号与服务器、秘境/关卡/等级、队伍装备、Decision 模型、录像器和资源规则。父程序/候选及可改动的战斗策略另行冻结；候选不得修改评测器。
- 每个实验日目标为一次真实领取、消耗 40 原粹树脂，以确保可比较；这只是试验预算，不替代日常刷取的 200 上限。少于 40 点、只通过战斗但未核验领奖、20 点领取或未知扣费均不作为该日成功。
- 主指标为从运行开始到结果核验结束的总秒数，包括移动、决策等待、战斗和领奖；失败按 600 秒计入，不能通过快速失败获得更好成绩。每日至多两次失败。
- 候选须 7 天全部成功。父版本也全部成功时，平均耗时至少减少 10%；父版本有失败时，候选须修复失败且平均代价不得超过父版本的 110%。
- 缺样本、重复日期、错臂、非有限数、资源量不一致或缩短门槛均拒绝。结论限定为单账号、单秘境、不同日对照，不承诺适用于其他游戏或其他队伍。

`evaluation.compare_metrics()` 是这些门槛的**纯离线算术组件**。调用者提供的汇总数不具备证据资格，即使计算通过也始终返回 `evidence_verified=false`、`eligible_for_release=false`。`review` 列出实际缺少的接入条件，绝不会回填历史记录或修改每日对照。

每日对照正常结束后，还需要停止协调器并释放输入，再部署单独的秘境执行适配器：执行前持久登记分组与冻结版本、持续记录介入覆盖、从原始证据生成日摘要、实机通过后提交晋级并验证下一轮继承。这个阶段必须保留现有失败与预算，录像覆盖全过程；游戏选择和输入仍只由 Decision 与 BetterGI 完成。

## 验证

```powershell
platform/.venv/Scripts/python.exe -m pytest resin-lab/tests -q -c resin-lab/pyproject.toml
```

单元测试中的截图、录像字节和运行数据均为合成夹具，只验证拒绝路径和算术边界，不能用于实机验收。
