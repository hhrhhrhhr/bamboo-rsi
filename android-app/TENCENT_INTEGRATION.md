# Tencent SDK 接入依据

核对日期：2026-10-25。本文件区分已核对的官方能力与 Bamboo RSI 待实现的连接，不代表已经接通腾讯服务或完成真实对局。

## 官方开源项目

| 项目 | 可核对的能力 | 部署与适用范围 |
|---|---|---|
| [tencent-ailab/hok_env](https://github.com/tencent-ailab/hok_env) | Python 环境接口、1v1 / 3v3、PPO 与训练框架；结构化观测、合法动作和奖励 | Gamecore 是另外获取的运行包；公开说明采用 Windows Gamecore + Linux Python SDK，可用 WSL2/Docker |
| [Tencent/GameAISDK](https://github.com/Tencent/GameAISDK) | 游戏图像识别、UI 处理、触摸动作、模仿学习与 DQN 等 | 公开部署由 PC 上 AIClient 连接手机/模拟器，不能直接满足手机脱离电脑运行 |

GameAISDK 参考提交为 `a089330a30b7bfe1f6442258a12d8c0086240606`。hok_env 参考版本固定为 `c6e0029a5e4b6e037a049804fdbe23ac74484311`。其 [环境源码](https://github.com/tencent-ailab/hok_env/blob/c6e0029a5e4b6e037a049804fdbe23ac74484311/hok_env/hok/hok1v1/env1v1.py) 提供 `reset`、`step`、`close_game`；1v1 观测长度 453，六个动作维度为 `[12,16,16,16,16,8]`，目标合法性依赖所选按钮。3v3 使用不同的 `step_feature` / `step_action` 接口，不能套用 1v1 动作格式。

GameAISDK 的 [开发接口](https://github.com/Tencent/GameAISDK/blob/a089330a30b7bfe1f6442258a12d8c0086240606/doc/2nddev/DevelopAI.md) 包含 `AgentAPI.GetInfo`、`ActionAPI.Click`、`Moving` 与释放触点的 `Reset`。其 [部署说明](https://github.com/Tencent/GameAISDK#1-环境部署) 仍列出 Ubuntu 14.04/16.04、CUDA 9、VS2017 等依赖；迁移成本需评估，当前不把整套旧运行时加入安卓 APK。

## 接入选择

先将 Gamecore 作为独立训练与评测后端，保留安卓的手机内执行后端。Gamecore 合法动作向量与手机触摸手势是两套观测/控制契约；训练出来的策略不能未经适配直接移植。手机还需要从画面可靠提取状态，并实测动作映射和延迟。

建议职责是：Decision 选择战术与阶段目标；低延迟策略负责连续移动、攻击和技能；RSI 提出受限策略候选；独立评测器冻结任务、对手及胜负规则，决定晋级和继承。这是后续接入设计，当前 APK 仍通过云端 Decision 逐步选择有限动作，没有实现成熟的本地战斗模型。

| Bamboo RSI 边界 | 后续 Gamecore 适配要求 |
|---|---|
| 版本与环境身份 | 绑定 SDK 提交、Gamecore 版本、地图、英雄、对手、策略和评测器哈希 |
| 运行预留 | 输入前持久登记运行 ID、配对和阵营；失败与中断保留 |
| 执行 | 严格按对应模式检查合法动作；超时与无效观测中止，保留失败 |
| 终局 | 从经核实的 Gamecore 终局字段和原始产物确认结果；`done`、奖励正数或模型自报都不能单独视为胜利 |
| 原始证据 | 保留对局 ID、结构化日志、ABS 回放及哈希；官方 ABSTOOL 用于回放，ABS 本身不是 MP4 |
| 独立评测 | 固定对手和条件、完整样本、冻结评测规则；候选不能修改评测器 |
| 晋级与继承 | 只更新同一环境契约内的已接受策略；下一轮父版本指向真实晋级版本 |

公开仓库此次核对到的是 1v1、3v3。正式手机 5v5、任意玩家匹配、手机原生合作接口的可用性须按后续合作资料确认，不从上述开源项目推断。

Gamecore 证据必须使用单独的来源与评测契约，不能包装成 Android `authorized_arena_live` 签名证明，也不能填入原神 14 天对照。模拟环境的晋级不代表手机实机晋级。

## 运行包与许可证

[腾讯官方开放环境入口](https://tencentarena.com/aiarena/zh/open-gamecore) 提供 Gamecore 获取流程；公开 SDK 不能代替 Gamecore 和部署所需的 `license.dat`。合作运行包、文档及授权文件由部署方提供，留在私有目录，不上传 GitHub。

hok_env 仓库代码采用 [Apache-2.0](https://github.com/tencent-ailab/hok_env/blob/master/LICENSE)，Gamecore 有 [独立协议](https://github.com/tencent-ailab/hok_env/blob/master/GAMECORE.LICENSE)。GameAISDK 的 [LICENSE.txt](https://github.com/Tencent/GameAISDK/blob/a089330a30b7bfe1f6442258a12d8c0086240606/LICENSE.txt) 声明 GPLv3，并列出第三方组件许可。Bamboo 原创部分的 MIT 不会替换这些许可；商业部署的具体范围沿用合作协议。

已新增独立 [HoK Lab 接入与 RSI 控制器](../hok-lab/README.md)，包含 1v1 SDK 工作进程、动作与终局校验、失败恢复、Decision 候选、固定配对评测和血缘存储。预测器、录像器和独立证明服务按实际合作环境连接。当前未复制上述项目代码进 Bamboo MIT 源码，未安装 Gamecore，未运行真实对战；需用取得的运行包验证 API、终局格式、回放和一次完整基线对局，再进入正式对照。
