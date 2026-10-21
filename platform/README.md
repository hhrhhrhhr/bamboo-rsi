# StartLux Platform 0.3.0

实机配置 `record_video: true` 会在输入授权前启动 BetterGI 游戏窗口录像。
录像为 1280×720、10 fps、无音频的 MP4，每分钟独立落盘到
`<home>/runs/<run-id>/video/`；`timeline.jsonl` 保留实际采样时间，
`video-report.json` 关联片段散列和首尾帧解码检查。录像启动失败不授权输入；
运行中录像失败会结束运行并释放输入。异常退出前已完成的片段保留，
未完成的片段不会被报告为已验证录像。录像仅用于复核，不替代任务完成或 RSI 晋级证据。

独立的通用执行平台：**StartLux Decision 决策 + 场景插件 + Google Harness² 优化后端**。
Python 包与 Windows 客户端不依赖旧 `game-harness-lab` 模块、GEPA 或 RRSI。
仓库根目录保留旧小游戏工程，便于已有使用者继续使用；新开发入口是本目录。

## 当前可运行能力

- `core/`：通用观察、动作、决策回执、状态校验、停止、执行记录、策略版本及回滚。
- `scenarios/genshin/`：现有 BetterGI Windows 接管、每日委托、领奖证据校验；截图回放与记录导入。
- `scenarios/workspace/`：在独立目录读取账目、调用工具写出合计文件、独立检查结果，验证通用接口。
- `optimization/`：调用官方 Harness² 的 Propose–Probe–Compose、候选选择和跨任务经验库。
  固定上游提交 `1cfe254e8d25b15f37048c3730134686abe60335`，并行与串行模式均可运行。
- 云端改进模型使用可配置的 Chat Completions 接口；游戏动作仍由指定的 StartLux Decision 模型选择。

首版可优化 instructions、memory、tool guidance；固定执行器、动作参数、模型身份和完成校验。
上游只注册 Codex/OpenCode 提示模板，因此适配层在一个位置注册 StartLux 的文本编辑范围。
核心递归、试跑、合成、选择和经验库调用上游实现。

## 安装与离线体验

优化环境使用 Linux/WSL 的 Python 3.12 或 3.13；也已在 macOS 3.13 验证。
Windows 客户端仅运行执行层，无需安装优化器。

从仓库根目录运行：

```bash
python3.12 -m venv platform/.venv
source platform/.venv/bin/activate
pip install -r platform/requirements-optimizer.lock
pip install --no-deps -e ./platform
startlux scenarios
startlux optimize --scenario workspace.ledger --config platform/examples/workspace/config.json --output /tmp/startlux-workspace --fixture
startlux optimize --scenario genshin.daily --config platform/examples/genshin/config.json --output /tmp/startlux-genshin --fixture --mode parallel
```

`--fixture` 明确使用测试模型，演示流程不会调用云端，不代表真实模型效果。
文件场景故意通过测试策略演示修正漏算负数；即使测试改进通过，也不允许发布给客户端。
原神合成样例没有实机执行，只验证适配。未知结果不视为成功。

重复相同命令可继续已绑定的任务；修改数据、模型、模式或递归预算需要新的输出目录。
`--k 1..4` 控制递归预算。默认 `--max-improver-calls 32` 限制单次进程的改进模型请求次数。
输出包括 `provenance.json`、`candidate.json`、`library/`、`gate.json`、`report.json`。

## 接真实模型

配置文件只填写模型 ID、任务包和数据路径。示例里的 `fixture-*` 必须换成真实部署 ID。
原神模型在任务包 `manifest.json` 的 `decision_model` 中固定；配置增加
`"decision_model_confirmed": true`，表示部署方已确认使用所填模型 ID；兼容旧确认字段。
平台不会推断公开别名等同于其他权重版本。

```bash
export STARTLUX_DECISION_KEY_FILE=/private/decision-key.txt
export STARTLUX_IMPROVER_URL=https://your-provider.example/v1/chat/completions
export STARTLUX_IMPROVER_MODEL=your-improver-model
export STARTLUX_IMPROVER_KEY_FILE=/private/improver-key.txt
startlux optimize --scenario workspace.ledger --config /data/workspace-config.json --output /data/optimization-001
```

Decision 默认 `https://api.bamboonode.dev/v1/decisions`；可用 `STARTLUX_DECISION_URL` 配置开发端点。
改进接口需要支持文本/图片输入、`response_format=json_object` 和 `max_tokens`。
Key 文件由操作者保管，不写入任务包、提示上下文或运行记录。真实运行去掉 `--fixture`。
改进模型的美元成本目前记录为未知；发布检查使用 Decision 返回的实际 token 数，不推算价格。

## 原神记录 → 优化 → 候选试跑

Windows 仍使用仓库的安装包。新构建使用本项目运行时；已发布二进制版本以文件名及
`windows/installer-source.json` 为准。实际麦克风与原神每日任务闭环需要 Windows 登录账号验收。

运行记录位于 `%LOCALAPPDATA%\StartLux\Genshin\runs`。将某次完整目录复制到优化服务器：

```bash
startlux import-genshin --run /data/genshin-run --output /data/captured-suite.json
```

导入器校验截图 SHA-256、观察绑定、动作目录及模型身份。单日记录保持 `unpartitioned`，
全部置于 evolve，不能据此运行优化验收。同一天的多次启动也不能拆分到两侧。
它不会把历史模型选择当作正确答案。要做独立回归，需要为 case 填写 `acceptable_actions`
或 `expect_abstention`，并设置 `label_available=true`；留出的 heldout 标签不会交给改进模型。
独立标签必须由截图和任务定义判定，不能抄历史模型选择。优化器会拒绝未标注数据及旧的逐帧划分。
先冻结日期划分，再生成候选；同一导入批次必须使用相同的模型、动作与执行资源：

```bash
startlux import-genshin --run /data/day-a-run-1 --run /data/day-a-run-2 --run /data/day-b-run-1 --heldout-day 2026-10-21 --output /data/captured-suite.json
```

`--heldout-day` 必须实际出现在记录中，并至少留下另一个完整游戏日供 evolve。
日期隔离只解决同日画面泄漏；跨任务类别、跨账号的外推能力仍需独立评估。

配置文件示例：

```json
{"pack":"genshin-run/pack","suite":"captured-suite.json","decision_model_confirmed":true}
```

```bash
startlux optimize --scenario genshin.daily --config /data/genshin-config.json --output /data/genshin-opt-001
startlux export-genshin --pack /data/genshin-run/pack --policy /data/genshin-opt-001/candidate.json --output /data/candidate-pack
```

导出包始终为 development，需要 Windows 实机验证。快照即使全对也不能自动发布。
开发者可以把 `candidate-pack` 的完整内容复制到
`%LOCALAPPDATA%\StartLux\Genshin\trial-pack`，随后在 0.3.0 客户端照常语音启动。
客户端只接受与内置模型、动作和执行资源完全一致的文本策略试验，记录为 `development_trial`。
删除该目录即可恢复内置/已发布策略；不要在执行中替换文件。
优化器不连接游戏输入、不重复领取奖励。已验证的策略库放在客户端
`%LOCALAPPDATA%\StartLux\Genshin\policies`，下一次启动才载入；正在运行的任务固定策略。

## 日级证据和有界恢复

每次运行在接管前建立 `runs/<id>/attempt.json`，记录 UTC 时间、服务器游戏日、策略、
Agent 源码/二进制哈希及可用的原生执行器/战斗配置哈希。服务器游戏日按服务器时区凌晨 04:00 切换。
`execution.lock` 使用操作系统进程锁，同一个设置目录不能同时启动两个 Agent。
它不替代原生执行器的输入控制代次和看门狗。

模型弃权、响应过期会重新观察并请求新决策；前台或 UID 暂时不可用会先等待只读复核。
每种原因最多恢复两次，每个账号游戏日最多四次，预算保存在 attempt 中，重新启动不会清零。
没有模型回执时不执行默认动作。执行失败、结果未知、超时、账号/控制权切换不自动重放。
上次已接管却未确认停止的记录会阻止再次启动；`reconcile` 必须从该记录的原控制管道取得实际停止确认，
不能靠删除锁或把运行状态改成成功来继续。外部停止器在终止 worker 前保存实际停止回执。

```powershell
genshin-agent reconcile --config C:/path/to/settings.json --run-id PREVIOUS_RUN_ID
startlux assess-genshin --home C:/path/to/Genshin --output C:/path/to/assessment
startlux collect-genshin --home C:/path/to/Genshin --output C:/path/to/new-collection
```

正常结束后自动生成本次 `feedback.json` 和 `episodes/index.json`。评估重新核验原始截图，
保留全部失败/中断尝试、初始进度、回执耗时、已知 token 数、恢复次数及失败前后证据引用。
不明费用保持未知。无法归属账号/日期的旧记录留在 `unscoped_attempts`，不会直接丢弃。
采集器把可用快照按任务包底座分组；坏记录仍保留失败评估，不能进入快照集。

默认 `--run-mode development`，不能计为无人干预证据。显式 `--run-mode autonomous` 只是声明采集模式，
仍须从 0/4 开始、完整记录、实际完成和停止确认；日内多次人工启动及未知覆盖均不合格。
`autonomy_eligible` 是本地记录的初步筛选，不是第三方无人干预认证，也不赋予发布资格。
旧记录不能因最后一次成功而补记成自主完成。

进程内有界恢复、崩溃后的保守阻断和失败归档是 RSI 的证据入口。未知动作不跨进程自动重放。
后续的跨日调度、候选生成、独立评估与晋级通过下述独立协调器执行。

## 原神 RSI 协调器

`startlux rsi-genshin` 将日常执行与 WSL 中的优化接成持久流程。新初始化默认使用
`decision_search`，仅调用 `/v1/decisions`；`harness2_chat` 保留真实 Harness² 与聊天改进模型流程：

1. 收集所有运行，包括失败和中断；相同执行资源才能进入同一训练集。按完整游戏日保留新验证集。
   历史模型选择不作标签。独立规则只核验已有截图约束下的扫描、待完成任务及领奖选择，未知标签保留。
   这些规则不能证明 OCR 或战斗正确；没有独立日期时只能提案，之后等待实机前瞻试验。
2. `decision_search` 从当前父版本出发，让 Decision 选择版本化编辑库中的策略和战斗变更，
   比较真实训练回放后决定保留父版本或提交候选。策略编辑包括精简说明和强化证据使用；
   战斗编辑包括减少位移和缩短攻击片段。开发者编写编辑算子，模型负责选择与组合，
   不将这种受限搜索称为任意源码生成，也不将其冒充 Harness²。
   `harness2_chat` 则从当前已接受父版本生成策略文本，保留真实 Propose–Probe–Compose 调度。
   默认每代最多 4 个训练快照、32 次改进模型调用、80 次 Decision 探测，失败/中断也计入持久预算。
   每个 job 保存完整采集库和实际选用的训练 ID。验证集不进入策略生成或战斗修复上下文；
   已被前代使用的日期不能重新称为新的独立验证集。
3. 执行失败可触发战斗 DSL 提案。Decision 通道从受限变体选择，聊天通道生成一次提案。
   原生执行日志按本次宿主 PID 归档，失败诊断可进入下一轮训练。仅允许班尼特、神里绫华、夜兰、钟离和有限动作、时长，
   先做语法/预算检查，再调用当前 BetterGI 的原生解析器。模型不能改输入权限、完成判定或运行任意 Python/JS。
   脚本在启动前安装，停止确认后按哈希恢复；崩溃恢复同样需要停止证据，外部修改不会被覆盖。
4. 候选先登记连续 14 个游戏日，基线与候选交替各 7 天。事先冻结账号、模型、资源、运行程序及日期表，
   每天只预留一次执行，缺失或失败不会用日内重试替换。全部成功的候选还需与基线具有相同战斗/对话任务数量分布，
   并改善成功率，或在全成功时令中位 token 至少下降 10%，或令中位耗时下降 10% 且 token 增幅不超过 10%；
   所有路径中位耗时均不能增加超过 10%，token 均不能超过两倍。
   这是不同日期的单账号前瞻比较，不能冒充同任务随机对照或统计显著性证明。
5. 晋级前从原始 trace、PNG、版本及输入回执重新核验全部日期；仅真实 Windows 证据可晋级。
   原子切换版本，下一代继承已接受版本。晋级后观察到失败即回滚；默认最多接受 3 代，之后继续执行已接受策略，停止生成新候选。

```powershell
platform/.venv/Scripts/python.exe -m startlux.cli rsi-genshin init --home .runtime/genshin-home --root . --host dist/genshin-desktop-local/bettergi
platform/.venv/Scripts/python.exe -m startlux.cli rsi-genshin status --home .runtime/genshin-home
platform/.venv/Scripts/python.exe -m startlux.cli rsi-genshin cycle --home .runtime/genshin-home
```

初始化默认使用 Ubuntu-24.04 的 `~/.cache/startlux-rsi-venv/bin/python`；可用 `--wsl-distribution` 和
`--wsl-python` 指定已安装 Harness² 的环境。协调器配置在 `home/rsi/config.json`，不含密钥。
Windows DPAPI 凭据仅经匿名管道传给 WSL；改进模型默认 `gpt-6.1-sol`，Decision 部署保持用户配置。
`optimizer_backend` 可选 `decision_search` 或 `harness2_chat`，旧配置缺省值仍按聊天通道解释。
已用于开发诊断的日期应写入 `development_training_days`，不得再作为独立 heldout 日期。
原神每日输入仍需要管理员权限、前台游戏及精确账号校验。`Start-Genshin-RSI.cmd` 持续运行；
`Stop-Genshin-RSI.cmd` 停止协调器、优化 worker 和本项目输入执行器。不会创建系统计划任务或替用户开机登录。

额度故障按接口分别记录在 `home/rsi/providers.json`。聊天改进模型的 `insufficient_user_quota`
只暂停候选生成；已接受策略仍可通过可用的 Decision 接口完成每日任务。Decision 自身确认额度故障时才阻止日常输入。
修复对应接口后显式执行 `rsi-genshin resume --home ...`；旧失败及费用记录保留，重试共享原有调用上限。
代码或原生资源发生变化时会暂停，以免试验混入不同运行环境。停止后需先 resume 再 serve。
源代码模式和打包 Agent 具有不同运行哈希，不能在同一已登记试验中混用。

候选可通过 `scripts/run_genshin_dev.py --run-mode development --rsi-rehearsal <bundle_hash>`
做同日开发试跑，只允许当前父版本的直接子候选；不会消费或伪造跨日登记。
该入口继承账号校验、停止确认、脚本安装恢复和完整失败记录。开发试跑和已经开始过的每日不能计为自主日验收。
协调器新配置默认 `max_rehearsals_per_day=2`：候选先经过原生解析和开发试跑，
核验真实任务完成与停止回执后才登记下一游戏日起的前瞻试验。失败候选保留并反馈给下一轮选择；
进程退出码不作为通过依据。试跑预留在启动前落盘，崩溃后的含糊预留不会重放输入。
候选失败按运行代码与原生执行环境绑定；修复执行环境后允许重新提出同一候选，
旧失败与当日试跑次数仍保留。运行中的文件不会热替换。

支持 `command_checkpoint_v1` 的原生桥接允许每日执行超时后取消当前技能并释放输入，
最多两次，且不会重新授权人工已停止的会话。下一步必须由 Decision 选择日志扫描，
只有新的截图证据确认刚才那项委托完成，才继续后续任务；未知状态和未完成任务均停止。
原始超时、取消与恢复证据会一起进入评估，不把恢复改写成无故障执行。
已返回终态并确认释放输入的技能失败也遵循同一核验流程，不重放失败的动作。
受限搜索 v2 在固定预算中同时保留早期回归快照和最新失败快照，并新增班尼特火元素／
治疗优先变体；编辑库仍由开发者编写，具体变更由真实 Decision 选择，效果必须实测。
v3 的证据策略还可携带按任务名称和游戏日绑定的失败记忆：有其他待办时优先完成其他项，
不沿用可能因排序改变而失效的槽位编号。初次扫描会识别已验证的严寒复苏界面并返回免费神像，
避免优化等待期间的严寒伤害妨碍下一次读取日志。其他未知弹窗仍停止。
开发试跑默认每天 2 次，可显式配置为 1–16 次；增加次数只用于调试，不能充当更多独立验收日。

本次开发的离线测试覆盖状态转换、两代继承、回滚、额度/重启、证据损坏及战斗文件恢复。
2026-10-20 已核对[官方说明](https://bamboonode.dev/docs/ai-model/decisions/startlux)：
测试期 `startlux-decision-27b-autothink` 的 `/v1/decisions` 支持有 default 组权限的零余额账户免费调用。
同日实测文本请求返回 HTTP 200，带原神历史截图的项目适配器请求也成功，返回合法的 scan 决策。
此前额度错误来自另一个 `/v1/chat/completions` 上的 `gpt-6.1-sol`，不能推断 Decision 不可用。
仅 Decision 的受限搜索通道不依赖聊天额度。真实候选、实机完成及跨日晋级分别核验；
没有 14 天实机证据时，自动晋级仍不能称为实机验收通过。
受限策略/战斗脚本优化不等于模型权重训练，也不能自动新增尚未支持的委托路线。
原神实机与 RSI 回放使用 `think_mode=always, think_budget=128`，验证响应中的思考启用状态
与 token 上限，实际输出 token 一并计入评估。2026-10-20 同状态离线对照中，关闭思考选择
再次竞速，开启后选择送餐；该对照只验证选择变化，不代表任务成功。通用内核默认和原八款
小游戏实验仍保持关闭思考，未修改模型服务的全局设置。
原神选择题的选项说明同时带上当前已核验的任务名称，原动作目录、可用选项和原生校验保持一致；
不会由客户端代选或过滤失败任务。“极速前进”路线跳过额外战后拾取扫描，这个运行时开关
只属于该路线执行器，不写入用户全局配置。
开发原生桥接为脚本交互键保留 100ms 按住时间，并在取消时确保松键；对话委托记录实际进入
对话界面的证据，避免始终留在主界面被上游误报为“对话结束”。离线测试通过不代表送餐任务已完成，
仍需真实委托日志核验。原始任务包不变，执行器更新单独绑定新的原生构建哈希。
离线优化遇到明确的网络传输错误或 HTTP 429/500/502/503/504 时，最多重试一次；每次请求先占用同一个
持久调用预算并保存独立记录。有效但不理想的选择、响应校验失败和额度耗尽均不重试。该恢复只用于
不执行游戏输入的优化 worker；实机决策仍按原有期限和停止规则处理错误。
开发试跑若因明确的临时接口错误结束，且原始轨迹证明只执行过日志扫描、从未提交委托/领奖动作，
并已确认释放输入，则记作“结果不确定”，不淘汰候选，也不计作成功。每天最多额外启动一次同候选
试跑，每次仍占用原有每日试跑预算。未确认停止、未知响应、已提交任务动作和损坏证据均禁止这条恢复。
原神模型请求只上传当前帧；已核验的日志状态与任务名称仍随结构化事实发送。四张历史日志原图继续
完整保存、绑定并用于独立验收，减少每次重复上传历史画面导致的超时；实机选择仍要求新鲜观察和原有有效期。
离线 worker 的接口超时独立设置为 90 秒，实机保留 30 秒及原有动作有效期。失败记录只保存白名单中的
错误类别（例如 read_timeout、http_503），不写入服务器原始错误正文、请求头或凭据。

2026-10-20 实机反馈及用户确认：“极速前进”沿途魔物必须击败才能通行。此前按倒计时跳过战斗的
假设已经撤回；历史候选和失败录像继续保留，但非零跳怪阈值会在输入启用前被拒绝，原生执行器也
禁止这种参数。必打战斗超时或寻敌次数耗尽不能当作清怪成功后继续路线。倒计时仍记录在日志中。
Decision 搜索 v5 改为选择清怪方案：修正后的减少位移方案保留绫华的短冲刺与重击，也可选择
更紧凑的爆发循环或偏向班尼特的方案。脚本规则由开发者定义，具体候选由模型选择；快照回归
只检验动作选择，实际击杀耗时与限时到达必须由录像和任务证据验证，不能据此宣称已提速。
原生握手仍在输入启用前绑定同一参数，前瞻验收重新核验绑定；旧候选的历史身份哈希保持不变。

## 独立回归与发布

Harness² 完成候选选择后，平台才在 heldout 上运行基线与候选；不拿 heldout 挑选候选。
缺失记录、未知结果、任务退步、没有改进、未知 token 成本、token 超过基线两倍均拒绝发布。
快照和测试模型的结果始终没有客户端发布资格。

对已经通过真实执行验证的场景：

```bash
startlux promote --scenario workspace.ledger --config /data/workspace-config.json --optimization-run /data/optimization-001 --output /data/policies
startlux rollback --scenario workspace.ledger --config /data/workspace-config.json --output /data/policies --expected-active CANDIDATE_SHA256
```

更新已有策略时，promote 同样需要 `--expected-active`，避免覆盖另一个发布。
原神的 replay 插件只产生 snapshot 结果，因此其通用 promote 会被拒绝；原神实机晋级必须经过
`rsi-genshin` 的前瞻日期试验。远程自动更新和模型权重训练服务不在当前实现中。

## 新增场景插件

实现 `startlux.core.scenario.Scenario`：任务列表、用户目标、可见素材、基线策略、执行及独立验证。
`run()` 使用 `ExecutionEngine` 和场景会话提供的 `observe/candidates/start/reconcile/cancel/stop`。
`Trial` 区分 snapshot、sandbox_execution、live_execution，并记录证据来源。
可重置性由场景负责；真实不可逆操作不能直接用于多候选并行试跑。

外部 Python 包注册：

```toml
[project.entry-points."startlux.scenarios"]
"your.scenario" = "your_package:create_scenario"
```

工厂接收 JSON 配置，返回 Scenario。插件在安装时执行 Python 代码，仅安装可信插件。
`workspace.ledger` 是完整的最小参考。场景插件不依赖 Harness²，未来可增加其他优化后端。

## 验证与部署

```bash
python -m pytest platform/tests -q
docker build -t startlux-optimizer platform
```

Windows 构建入口仍为 `scripts/build_genshin_desktop.py`，原生 IPC、WPF 语音和安装卸载由
`.github/workflows/genshin.yml` 验证；Linux 的 Harness² 集成由 `platform.yml` 验证。
Docker 镜像提供 CLI 作业入口，生产队列、多租户控制台及远程更新服务尚未实现。

本平台沿用原神 Agent 的 GPL-3.0-or-later；Google Harness² 保留 Apache-2.0。
BetterGI、AutoCommission 及语音依赖的来源、许可证见仓库 `THIRD_PARTY_NOTICES.md`。
