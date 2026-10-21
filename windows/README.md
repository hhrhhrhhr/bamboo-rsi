# Windows 原神源码体验版

本仓库首次发布仅含源码。请按 [根目录构建步骤](../README.md) 构建当前版本；没有随源码附送旧预览安装包。下列操作说明中的安装目录指使用者自己的构建或安装目录。

## 首次试跑

1. 游戏使用中文界面，设置蒙德每日委托，进入可操作的主界面。
2. 确保 BetterGI 战斗策略可用。随包 BetterGI 默认位置为 `%LOCALAPPDATA%\Programs\StartLuxGenshin\bettergi\BetterGI.exe`。配置后从系统托盘完全退出 BetterGI，再通过助手启动接管。
3. 启动 StartLux 原神语音助手，填写游戏程序路径、UID、服务器、Bamboo Key 和模型部署 ID。Bamboo 官方接口当前使用 `startlux-decision-27b-autothink`。
4. 确认使用所填模型进行每日任务决策。每次请求和返回必须使用同一个模型 ID，不把 API 别名当作其他权重版本的证明。Key 在当前 Windows 用户下用 DPAPI 加密保存。新源码同时兼容旧版配置中的确认字段；仓库已有安装包仍使用旧版设置界面，需要重新构建才能应用此修改。
5. 开启语音，说“开始每日任务”；需要中断时说“停止”。也可使用界面上的启动、停止按钮。

已登录后停在派蒙菜单也可启动：Agent 同时核对 UID、游戏前台状态及“冒险等阶 / 世界等级 / 冒险之证”菜单标记。
首次由决策模型选择扫描动作，现有扫描脚本先返回游戏主界面，再打开冒险之证。
已识别的地图页同样可恢复，仍要求当前账号 UID 精确匹配并且游戏处于前台。
普通未知界面、加载画面、未核对账号或后台窗口不会通过这个恢复条件。
冒险之证委托页也可从三个明确标题识别后恢复，已有 0/4 等进度文字本身不构成恢复或完成依据。

当前实机队伍可选用[班尼特 / 绫华 / 夜兰 / 钟离策略](../integrations/bettergi/combat/bennett-ayaka-yelan-zhongli.txt)。
将其放在 BetterGI 的 `User/AutoFight` 子目录，配合“根据队伍自动选择”使用；专用策略包含四名角色的动作。
战斗设置开启“战斗中持续索敌”，使角色在敌人移到侧面后重新调整朝向。实机中“旋转寻找敌人”会额外移动并追击附近其他怪物；当前营地委托关闭该选项，减少离开委托范围的问题。
路径脚本或战斗模块返回成功后，仍须逐项重新扫描冒险之证核验真实完成标记。
手册会将已完成委托移到末尾。原生桥接按唯一任务名称保留身份，并将新位置的状态截图关联到原任务；四行均识别成功后才更新证据。

每项委托执行后重新读取冒险之证并保存截图。路线失败、无法识别或未确认完成会停止；四项真实委托全部确认完成后才开放凯瑟琳每日奖励领取。原石数量没有独立证据时保持未知。

2026-10-19 本地源码构建已实机完成蒙德四项真实委托并领取凯瑟琳奖励，手册截图确认 4/4 和“今日奖励已领取”。
该次运行使用 `startlux-decision-27b-autothink` 选择任务，由 BetterGI 执行；验证报告、追踪和原始截图保存在本地构建的 `verification/daily-2026-10-19/`。
这次验证覆盖一个账号当天的四项委托；仓库内现有预览安装包尚未更新为该本地构建。
每日动作有效期设为框架允许的 30 秒，以覆盖实测的图片上传和模型响应耗时；执行前仍重新校验账号、前台、任务条件和控制代次。

运行记录保存在：

```text
%LOCALAPPDATA%\StartLux\Genshin\runs
```

## 源码联调

在仓库的 `platform/.venv` 安装本项目后，可右键根目录 `Test-Genshin-Dev.cmd`，选择以管理员身份运行。
它使用已安装客户端的 BetterGI，以及当前工作区的 Python Agent、JS 适配器与固定版本委托资源；不需要重新打包即可应用这些修改。
设置和 DPAPI 加密凭据默认读取当前 Windows 用户的 `StartLux/Genshin` 目录。
本机可在忽略的 `.runtime/genshin-home/` 中放置独立设置和加密凭据，并通过 `.runtime/install-root.txt` 指定安装目录。

开发会话最多运行一小时，接受显式的 `run`、`stop`、`exit` 指令；不会自行重试已经失败的委托。
到期时同时通知启动器的独立停止线程，尚未连上 BetterGI 的静默启动也会被取消。
启动器未按时退出时，会话状态记录为 `attention_required`，不会误报已经关闭。
`Stop-Genshin-Dev.cmd` 可取消尚未完成启动或正在等待模型的试跑。
取消先走独立控制管道释放输入，再终止 Agent；运行中未确认停止时保留 BetterGI 的本地输入看门狗。
Windows 虚拟环境产生的 Python 子进程也会随 Agent 一起结束，独立启动的 BetterGI 不属于该进程树。
任务包生成和连接阶段的异常也会保存 `report.json` 与启动事件。
Agent 明确报告尚未接管输入时，可清理未完成启动的 BetterGI，避免待授权进程阻塞后续重试。
停止管道无法确认时会在追踪中记录 `input_stop` 的 `acknowledged: false`，保留原始故障类型与本地看门狗。
状态与启动事件保存在 `.runtime/live-state.json`、`.runtime/live-events.jsonl`，任务证据仍位于所用设置目录的 `runs/`。

```powershell
platform/.venv/Scripts/python.exe scripts/genshin_dev_controller.py status
platform/.venv/Scripts/python.exe scripts/genshin_dev_controller.py run
platform/.venv/Scripts/python.exe scripts/genshin_dev_controller.py exit
```

采集明确声明无人干预的试验时使用 `run --run-mode autonomous`；该参数不会增加权限、自动重启执行器或放宽完成判定。
只在本轮确实没有人工操作/调参时使用；自动报告仍会核对初始进度、回执、输入覆盖和异常记录。

试跑后保留整个 `runs/` 目录，包括失败和中断记录。新源码会自动生成 `attempt.json`、`feedback.json`
和设置目录下的 `episodes/index.json`，用于核对整天的进度、恢复次数和人工重启。
运行模式默认 development；最后一次报告成功不能代表整天无人干预。
模型弃权或响应过期最多重新观察两次，每个账号游戏日累计恢复最多四次；不会自动重放执行结果不明的委托。
原始运行仍包含 `report.json`、`trace.jsonl` 和截图。异常发生过早时可能只有部分记录。
完整的采集命令、日期隔离规则和停止确认流程见 [平台说明](../platform/README.md#日级证据和有界恢复)。

持续 RSI 开发入口为根目录 `Start-Genshin-RSI.cmd`，对应停止入口为 `Stop-Genshin-RSI.cmd`。
先按[协调器说明](../platform/README.md#原神-rsi-协调器)初始化；实机输入阶段仍须通过正常的 Windows 管理员授权。
右键 `Start-Genshin-RSI.cmd` 选择“以管理员身份运行”。入口会请求旧协调器安全停止，
最多等待 90 秒，取得所有执行锁并核对历史输入停止回执后才恢复服务；不会强杀进程、删除锁或更改每日预算。
普通权限启动会提示上述操作并直接退出，不会中断已有监控，也不会自行提权。
也可以双击 `Start-Genshin-RSI-Admin.lnk`，由 Windows 请求管理员授权。
此快捷方式由 `scripts/create_genshin_admin_shortcut.ps1` 生成并核验目标、参数和
标准 Shell Link 管理员标志；生成过程不会执行快捷方式或改变 UAC 设置。
窗口在退出后会保留退出码并等待按键。每次启动的阶段、权限结果和经过脱敏的错误位置
分别保存在 `.runtime/rsi-startup/*.jsonl`，Python 之前的入口和退出码另记于
`.runtime/rsi-startup-entry.log`；Python 及其父进程的权限令牌也会记入日志，便于定位权限继承问题。
历史记录不会覆盖，不记录密钥或异常正文。
状态、不可变候选、每日预留、模型调用预算和晋级证据分别保存在设置目录的 `rsi/` 下。
新初始化默认用免费的 Decision 接口进行受限策略搜索；可选聊天改进通道保持独立。
候选开发试跑使用 `--rsi-rehearsal`，强制标记 development，不计入跨日自主验收。
额度状态按接口区分：聊天改进模型额度不足只暂停候选生成，可用的 Decision 仍能执行每日任务。
2026-10-20 已实测文档标注为测试期免费的 Decision 文本和截图请求均成功；
只有 Decision 自身返回额度错误才因此停止日常输入。修复对应接口后使用 `rsi-genshin resume`。
该开发入口尚未获得多日自主运行验收，不能沿用 2026-10-19 人工调试运行的成功标记。

本安装包来自已通过验证的源码版本 `be629fb624739f7198273e41c44411f79abb5106`。[Windows 构建与安装验证](https://github.com/dctongsheng/game-harness-lab/actions/runs/39214636384)通过；真实麦克风、榜首模型 API 联调及原神实机每日委托/领奖闭环尚未验收。完整对应源码和许可证同时保留在安装包的 `sources/` 与 `licenses/` 中。
