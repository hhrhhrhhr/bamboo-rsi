# Bamboo Delta Lab · Windows

三角洲行动 Windows 接入的第一阶段：绑定指定游戏进程的窗口，记录无麦克风视频，向 Decision 发送游戏画面并保存结构化建议、延迟和失败证据。

**0.1.0 是只观察的开发探针。没有游戏输入、自动开局、战斗、撤离或 RSI 晋级能力。** 当前没有三角洲客户端实机验证记录。模型输出的“结算”只触发停止观察，不能作为完成或胜利证明。

## 已有能力

- 通过完整 EXE 路径和进程创建时间绑定唯一游戏窗口；要求前台、非最小化、窗口尺寸不变。没有桌面捕获、自动切前台或提权兜底。
- FFmpeg `gdigrab hwnd=` 只录该窗口，以 5 FPS 保存 H.264/MKV，提供 2 FPS 的观察截图；不启用音频输入。切出游戏或进程改变会停止录制。检查存在短暂采样间隔，因此运行期间应避免打开私人界面。
- 先确认录像和新截图可用，再调用模型；拒绝黑屏/单色截图，退出时实际解码录像核验。DirectX 客户端是否支持此捕获路径仍需实机验证；黑屏会停止，不能声称已经录到游戏。
- Decision 只能选择固定的阶段、进展、动作建议。核验精确模型名、完整动作目录、概率数值与置信度；禁止重定向及任意动作。超过 2.5 秒的建议标记为过期，但任何建议都不会执行。
- 复用 `rsi-core` 的原子存储、锁、哈希和持久模型调用预算。调用前占用预算，失败及重启不退还；每次试跑使用新目录，保留全部失败。
- 独立目录与模块，不导入或修改原神、BetterGI、王者荣耀的运行时或实验记录。

## 配置与运行

需要 Windows、Python 3.11+、带 `gdigrab` 和 `libx264` 的 FFmpeg，以及已安装并处于前台的三角洲客户端。FFmpeg 是单独配置的外部程序；本仓库不分发其二进制。当前本机测试使用 imageio-ffmpeg 0.6.0 附带的 FFmpeg 7.1，发布时需保留其许可与来源。

```powershell
platform/.venv/Scripts/python.exe -m pip install --no-deps -e ./rsi-core -e ./delta-lab
New-Item -ItemType Directory -Force .runtime/delta-lab
Copy-Item delta-lab/examples/windows.template.json .runtime/delta-lab/config.json
```

在私有配置中填写真实游戏 EXE 和 FFmpeg EXE 的**绝对路径**。不要填写 WeGame 启动器路径，也不要把 Key 写入配置。`model` 填账户可用的精确模型名；Key 由当前进程环境 `BAMBOO_DECISION_KEY` 提供。探针不会自动读取或复制原神账号凭据。截图会发送给配置的模型服务。

```powershell
platform/.venv/Scripts/python.exe -m bamboo_delta.cli doctor --config .runtime/delta-lab/config.json
platform/.venv/Scripts/python.exe -m bamboo_delta.cli observe --config .runtime/delta-lab/config.json --samples 3
```

没有安装包时也可将 `delta-lab/src` 和 `rsi-core/src` 加入 `PYTHONPATH` 后运行上述命令。

先手动进入训练或获准测试场景，再启动观察。`doctor` 缺任何前提就返回退出码 2；不启动游戏、不调用模型、不录屏。`observe` 默认最多 3 次请求，连接/读取超时设为 15 秒；整个实验持久预算默认 20 次。按 Ctrl+C 或在当前运行目录创建名为 `STOP` 的文件即可停止。网络调用正在进行时，停止标记会先终止录制，返回的建议仍保留且不会执行。

每次输出至 `.runtime/delta-lab/runs/<id>/`：

- `manifest.json`：本机配置、源码/游戏/录像程序哈希。
- `frame-*.png`、`request-*.json`、`decision-*.json`：实际观察及请求记录。
- `game.mkv`：录像；`report.json` 给出解码结果与原始工件哈希。

这些文件可能包含游戏账号画面及本机路径，应留在忽略目录。`observation_completed` 仅表示观察流程完成；所有报告始终为 `game_input: false`、`verified_outcome: unknown`、`promotion_eligible: false`。

## 后续实机接入与 RSI 门槛

1. 在真实三角洲窗口验收捕获、录像及 Decision 阶段识别，量出端到端延迟与过期率。模型在静态画面的判断不能证明其能控制 FPS 战斗。
2. 新增有界 Windows 动作执行器、输入租约和独立停止看门狗，先验收固定路线、交互、卡住停止；所有游戏操作由项目执行器消费 Decision 选择，开发者介入单独记录。
3. 增加游戏专用的结果核验：目标达到、失败、死亡、撤离与收益分别取证。未接通这些核验之前不开放正式对照。
4. 接入 `rsi-core.Adapter`，固定地图/装备/任务/执行器和评测规则，开发集与验证集隔离。候选先限制为有界策略和技能参数；评测器、预算和阈值不允许候选修改。
5. 预先登记适合实际环境的完整对照，不沿用原神不可重复每日的设计或王者胜率阈值。对照证明改善后晋级，再从新父版本继续收集开发证据。离线回放、编解码测试及观察报告都不能作为实机晋级样本。

真正的搜打撤还需要实时感知、连续镜头/移动控制、战斗、物品交互与撤离技能，目前均未实现。王者荣耀 Gamecore 运行包不能代替三角洲环境；已有合作所覆盖的测试环境及接口需与三角洲项目对齐。

## 验证

```powershell
platform/.venv/Scripts/python.exe -m pytest delta-lab/tests -q -c delta-lab/pyproject.toml
```

默认测试使用明确的合成帧、假模型和假录像器。设置 `BAMBOO_TEST_FFMPEG` 后增加真实视频编解码测试，素材仍为测试图案；它不操作游戏，也不构成三角洲实机验证。

## 参考与许可

本模块新写源码采用 MIT，没有复制或调用下面项目的代码：

- [DeltaCrafting](https://github.com/ixekico/DeltaCrafting)：MIT；Windows 截图/OCR/特勤处制造流程参考，不提供完整战斗撤离。
- [DeltaForceAgent](https://github.com/xxxsy11/DeltaForceAgent)：MIT；知识与工具规划参考，自优化作用于工具规划。
- [Tencent GameAISDK](https://github.com/Tencent/GameAISDK)：GPL-3.0；图像识别、AI 与设备交互分层参考，文档架构依赖电脑连接手机，没有现成三角洲运行环境。
- [FFmpeg](https://ffmpeg.org/legal.html)：外部工具，其配置及编译组件决定适用许可，不因本模块 MIT 而重新许可。
