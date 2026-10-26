# Bamboo HoK Lab

腾讯 Gamecore **1v1** 的独立接入与受限策略 RSI 控制器。主控复用 `rsi-core` 的版本、文件锁和 CAS 晋级存储；对战使用独立的配对胜率评测器。不会改写原神冻结程序、14 天试验或 Android 账本。

**当前是可测试的接入代码，真实 Gamecore 对局与晋级均为零。** 本仓库不包含腾讯运行包、许可、预训练权重、已部署的独立签名服务或针对合作环境完成验证的录像实现。需要将这些部署材料接上，才能实际执行。单元测试中的 SDK、录像与签名数据均为明确的合成夹具。

## 已实现的闭环

1. 固定 SDK/模型/预测器/录像器文件，Gamecore 服务版本、英雄/技能/铭文、评测公钥和源码哈希。
2. 从已接受父版本执行开发对局，保留包括失败在内的完整记录。
3. 满 3 次可用开发记录后，程序调用 Decision，在相邻策略中提出候选，允许不修改。
4. 调用模型前登记 100 对、共 200 局的计划；每对两臂使用相同英雄与阵营，对内执行顺序随机，阵营交替。公开 SDK 没有在此接入中控制随机种子，不能声称两个对局重放相同随机轨迹。
5. 固定门槛：胜率至少提高 5 个百分点；单侧配对精确二项检验通过；预测调用总成本不超过父版本 110%。第 n 次登记的显著性预算为 `0.05 / (n * (n + 1))`，取消和失败不重置。未收齐证据不显示中途效果。
6. 独立评测方验证 Gamecore、终局、场景、无开发干预及录像，签发绑定原始文件的 Ed25519 证明。程序重新检查签名、原始哈希、逐帧动作、终局与成本，再决定晋级或拒绝。
7. 晋级后下一轮从新版本出发，必须收集新父版本的开发记录。拒绝、回滚与中断保留版本、样本及模型调用预算。

优化对象目前是预测器**攻击按钮与三个技能按钮的 logit 偏置**，每项整数 -2 到 2，每次只改变一项一个单位。高频战斗由冻结的本地预测器执行，Decision 负责局间候选选择。这不是训练 Decision 权重，也不是允许候选修改任意程序。SDK/预测器/评测规则变更需要另建实验，不得迁移旧成功样本。

## 安装与检查

主控 Python 3.11+；原生 SDK 工作进程按固定上游要求使用 **Linux Python 3.8/3.9**。两者通过独立进程与文件工件协作，SDK 工作进程不会导入主控的 Python 3.11 依赖。实际部署建议主控也在 WSL/Linux，两个解释器使用同一套路径。Windows 上可以运行检查与离线测试，不直接加载 Linux `.so`。

```powershell
platform/.venv/Scripts/python.exe -m pip install -e ./rsi-core -e ./hok-lab
platform/.venv/Scripts/python.exe -m bamboo_hok.cli doctor --output .runtime/hok-preflight.json
platform/.venv/Scripts/python.exe -m pytest hok-lab/tests -q -c hok-lab/pyproject.toml
```

doctor 不启动 Gamecore、不进入游戏、不调用模型或导入插件。带 `--runtime` 时会核验冻结文件、在 8 秒内查询 SDK 解释器版本，并检查 SDK 目录、原生扩展的 Python ABI 和插件工厂定义；缺组件时逐项给出修复说明，返回退出码 2。它不把历史比赛数写成零，也不能替代服务连通性、录像播放或胜率验收。真正执行前会重复检查，已知部署错误不消耗正式对照槽位。也可以将 `rsi-core/src` 与 `hok-lab/src` 加入 PYTHONPATH，从源码运行。工作进程的 `worker.py`、`protocol.py` 保持 Python 3.8 语法兼容。

## 部署材料到位后的操作

将示例配置复制到 `.runtime`，填写真实路径与独立服务的 Gamecore 构建哈希。私有许可、账号和密钥不放入 SDK/模型快照，也不上传 GitHub。`sdk_root` 是可导入 `hok` 包的目录；预测器与录像器的依赖文件也要包含在冻结目录内，不能运行时下载或更新。

```text
bamboo-hok seal-runtime --runtime .runtime/hok-runtime.local.json --output .runtime/hok-runtime.frozen.json
bamboo-hok init --home .runtime/hok-rsi --runtime .runtime/hok-runtime.frozen.json --scenario hok-lab/examples/scenario.json --authority .runtime/hok-authority-public.txt
bamboo-hok run-next --home .runtime/hok-rsi --development
bamboo-hok propose --home .runtime/hok-rsi --endpoint https://api.bamboonode.cn/v1/decisions --model <精确模型名>
bamboo-hok run-next --home .runtime/hok-rsi
bamboo-hok import-proof --home .runtime/hok-rsi --proof <独立评测方的证明文件>
bamboo-hok evaluate --home .runtime/hok-rsi
bamboo-hok status --home .runtime/hok-rsi
```

模型 Key 从进程环境 `BAMBOO_DECISION_KEY` 读取，也可用 `--key-env` 指定变量名；不要写入命令行、配置或报告。原神 DPAPI 凭据不会自动复制到这里。给原生工作进程的环境移除了模型凭据与其他无关环境变量。

`cycle --max-matches 1..10 --proof-directory <独立服务投递目录> --endpoint ... --model ...` 可在单次有界调用中自动安排开发、提出候选、执行已登记对照并导入独立证明；达到预算后退出。它不创建计划任务。已完整保存的对局缺少独立证明时会等待；已确认中断或原始记录不完整的正式槽位会使该轮立即拒绝，保留失败与预算，不浪费其余场次，也不以重试替换失败。只有重新登记的完整新试验才能再次对照。

```text
bamboo-hok recover --home .runtime/hok-rsi
bamboo-hok reject --home .runtime/hok-rsi --reason <原因>
bamboo-hok rollback --home .runtime/hok-rsi --reason <原因>
```

中断会留下运行占用。recover 先确认对应 Gamecore 与录像器停止，再将未完整结束的记录保留为未知；无法确认就继续阻止输入。重复恢复保留第一次中断时间。若主控只是在完整工件落盘后、清除占用前退出，则保留原工件并等待独立证明，不把它重跑或直接计为晋级。

源码变化后仍可使用 status、recover 和 reject 维护入口，继续对局、生成候选及晋级仍受冻结检查约束。status 在只能读历史指针时明确报告 `accepted_state_verified: false`。恢复先停止远程对局；如录像器尚未停止且其冻结依赖已变更，则保留占用并要求恢复原文件，不执行改过的插件。已接受版本证据损坏时支持有记录地回滚；不删除坏证据。

## 执行与信任边界

- 首版只支持单个本地预测器对冻结 Gamecore 的 Common AI；原始观测、动作、原生 protobuf 帧均存档。
- 开始执行前，录像器必须报告已开始录制游戏窗口且不使用麦克风；结束时需保存 MP4，正式对照还需对应 ABS。播放验证最终由独立评测方确认，程序不能从扩展名推断可播放。
- Native SDK 对非法动作可能仅告警，本接入在发送前严格拒绝；目标合法掩码随所选按钮变化。
- 终局按上游 1v1 actor 使用的 type-24 水晶核验；只有明确的一方水晶为零、另一方大于零才认定胜负。超时、双毁、缺失状态和模型自报均为 UNKNOWN。
- 工作进程有硬超时，主控随后调用 Gamecore stop/exists 核验。录像器需要实现可跨进程恢复的停止接口。未知释放状态阻止再次输入。
- 正常终局先走 SDK 的 close_game 流程，再进行停止核验和录像收尾；关闭失败仍保留终局原始记录，但不能作为成功的正式样本。
- 同一操作系统用户的所有实验共用 `~/.bamboo-rsi/hok-port-leases` 下的端口占用记录；重叠端口禁止并行执行。主控崩溃后记录继续保留，须恢复原实验并确认游戏和录像均停止后才能复用。不同用户或主机的执行隔离仍须由部署方配置。
- 工作进程在主控保存 PID、创建时间和完整命令后才允许产生副作用。恢复时按这些身份核验并停止遗留子进程；不按进程名批量结束，不终止复用同一 PID 的其他任务。
- 预测器、SDK、录像器及独立证明服务是可信部署组件，候选只能修改两个有界参数。文件冻结与签名不是任意恶意代码的 OS 沙箱；由操作者自行配置公钥，也不等于第三方商业认证。
- Gamecore 成绩不能替代 Android 实机、正式 5v5 或原神每日验收。手机执行仍由 `android-app` 负责。

接口细节见 [INTEGRATION_CONTRACT.md](INTEGRATION_CONTRACT.md)。官方 SDK、许可与模式来源见 [腾讯接入依据](../android-app/TENCENT_INTEGRATION.md)。Bamboo 原创代码 MIT；腾讯 SDK 与 Gamecore 分别保留其许可，不随本模块分发。
