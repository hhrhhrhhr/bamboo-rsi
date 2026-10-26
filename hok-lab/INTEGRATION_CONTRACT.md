# Gamecore 部署适配契约

本文件描述 Bamboo 自定义的预测器、录像器与证明接口；它们不是腾讯公开 API 的承诺。SDK 调用依据固定提交 `c6e0029a5e4b6e037a049804fdbe23ac74484311` 的 [1v1 例程](https://github.com/tencent-ailab/hok_env/blob/c6e0029a5e4b6e037a049804fdbe23ac74484311/hok_env/hok/hok1v1/unit_test/test_env.py) 与 [终局统计](https://github.com/tencent-ailab/hok_env/blob/c6e0029a5e4b6e037a049804fdbe23ac74484311/aiarena/1v1/actor/actor.py)。Gamecore 服务、权重和设备到位后仍需真实兼容性验证。

## 预测器

冻结的 Python 文件导出 `create_predictor(model_root)`，返回对象：

- `reset(camp, player_id)`：每局清空循环网络状态，绑定本局阵营与玩家。
- `scores(state)`：接受 SDK 原始状态，返回六个有限数值 logit 头。前五个长度分别是 12、16、16、16、16；最后一个是 12×8，给每种按钮提供目标分数。若模型只输出上游示例的 8 个目标 logits，可将同一行重复 12 次；最终合法目标仍由每个按钮对应的掩码限制。

这里仅接受真实冻结的预测器，不自动补随机动作或全零模型。读取 `model_root` 之外的模型、动态服务或模型池将使冻结不完整；接入前必须将依赖纳入冻结目录。模型初始化必须在 SDK reset 前完成。程序尚未附带能赢下王者荣耀的训练权重。

## 录像与回放

冻结的 Python 文件导出 `create_recorder(output_directory)`，返回对象：

- `start()`：真实开始录制游戏窗口，返回且仅返回 `{"active":true,"kind":"game_window","microphone":false}`；未成功不得返回该值。
- `finish(match_id)`：停止并保存录像，收集与 match_id 对应的 ABS；返回 `{"playable":true,"stopped":true,"video_path":"match.mp4","replay_path":"match.abs"}`。须确认录制进程已结束；路径必须在该运行目录下，禁止绝对路径与链接。
- `recover()`：在新进程中读取该目录保存的本次录制句柄，停止遗留录制并保存可恢复数据，返回 `{"stopped":true,"saved":true}`。只能控制自己启动的录制，禁止按名称终止其他录像或 Gamecore 会话。

录像器须先持久化所持有的资源与恢复信息，再报告启动成功；只能在确认停止后返回 stopped。主控硬超时后会调用恢复，失败则保留占用。具体实现随获准 Gamecore 的渲染/回放环境适配，目前不虚构一个已可用的官方录像 API。

## 独立证明

签名使用 Ed25519；公钥为 32 字节标准 Base64，签名为 64 字节标准 Base64。正文使用 UTF-8 JSON、对象键排序、无额外空格、禁止 NaN；数值字段使用整数。顶层为 body 和 signature。

body 包含：

| 字段 | 要求 |
|---|---|
| assignment | 与预登记运行完整一致，包括 run_id、trial、slot、pair、arm、seat、policy、contract、scenario_sha256、mode |
| origin | `authorized_gamecore_live` |
| assisted | false；开发干预必须如实报告，不能晋级 |
| server_build | 与冻结 Gamecore 服务版本哈希一致 |
| native_result_verified | true；评测方独立核验原生终局、双方阵营、英雄、技能、铭文与对手版本 |
| video_verified | true；评测方确认录像可播放、覆盖输入前后并与本次比赛对应 |
| manifest_sha256 | 该运行 manifest.json 原始字节 SHA-256 |
| outcome | WIN 或 LOSS，须与原始水晶状态及本地记录一致 |
| match_id | Gamecore 的真实对局身份，跨所有试验唯一 |
| policy_calls, duration_ms | 正整数，与运行原始记录一致；动作数还由事件轨迹重算 |

评测服务必须独立获取或校验原生帧、Gamecore 版本和录像，不能照抄 worker-report.json 后直接签名。ABS 与 MP4 的对应关系也须核验。SDK 工作进程不获得签名私钥，项目没有用于把本地胜负“签成正式成绩”的命令。

服务将文件以 `<run_id>.json` 投递到私有目录后，cycle 可以自动导入。文件必须原子落盘；不完整文件会被拒绝。错误签名、旧运行、未知结果、改动原始文件、重用比赛、人工介入、输入或录像未停止都会阻止晋级。

测试中使用临时密钥签署假的视频与 SDK 夹具，只验证上述程序控制流程。没有真实独立评测方时，不可用测试密钥或自签报告代替实机验收。
