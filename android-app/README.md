# Bamboo RSI Android

手机独立运行的 Android 11+ 开发版：游戏画面 → 云端 Decision 选择有限动作 → 无障碍触摸 → 本地录像与证据。运行不依赖电脑、ADB、Root 或 BetterGI。构建需要 JDK 和 Android SDK。

**当前为 0.2.0-dev，尚未完成安卓真机对战、真实晋级与商用验收。** 代码层面已接通策略候选、预登记、独立证明校验、晋级、父版本继承和回滚。没有内置腾讯授权对战服务，单元测试不能计作真实战绩。

## 使用

1. 安装开发 APK，在手机填写自己的 HTTPS Decisions 接口、精确模型名和 Key。Key 在 Android Keystore 中加密并绑定接口；换接口须重新输入。
2. 用完整横屏游戏截图，依次点选摇杆、普攻、三个技能中心；也可手动填坐标。显示比例变化时重新校准。
3. 按应用说明开启无障碍；先自行进入训练场或获准测试局，再从应用启动并完成系统录屏授权。不会自动登录、排队、选英雄或点系统授权。
4. 录制全显示屏、无麦克风。仅在国服 com.tencent.tmgp.sgame 前台运行。窗口切换瞬间仍可能被录入，运行时不要打开私人界面。
5. 在“RSI 候选、对照与证据导出”查看状态，选择运行并导出录像、日志与版本状态 ZIP。导出可能含账号画面，不包含模型密钥。

动作有八方向短移动、边移动边普攻的双触点手势、三个技能的点击与四方向瞄准、观察和停止；没有英雄专属连招、持续多点操控、完整经济/装备操作或成熟的低延迟战斗控制器。每局最多 300 次模型调用、30 分钟；单次移动最多 500 毫秒。超过 2.5 秒的画面决策会丢弃，累计 5 次后停止并记录 provider_too_slow。这些能力不足以证明可以完成一局正常竞技对战。

## 手机端策略 RSI

- RsiLedger：文件锁、原子替换、Android Keystore HMAC；版本以内容哈希标识并记录父版本。先预留运行/调用预算再执行；崩溃不会退还样本或优化调用次数。
- StrategyVersion：仅优化有界数据（移动时长、观察间隔、均衡/谨慎/远程策略指令）。候选不能修改程序、评测器、样本数或晋级阈值。不会更新 Decision 权重，也不是手机自行编译任意代码。
- 同一已接受父版本至少有 3 局录像完整、输入已释放的开发证据后，Decision 才能提出一个相邻候选。默认每局结束尝试自动提出候选；每个 UTC 日最多 2 次优化调用。失败、弃权、中断均保留。优化器只看开发集，不读取对照结果。
- 通过手机页面登记固定 100 个配对（200 局），每对两臂，阵营交替，臂顺序预先随机。冻结安装 APK、游戏版本、设备、按键、接口、模型和独立评测公钥。配置变化会阻止继续试验，必须保留失败并重新登记。
- 本地中文 OCR 读取明确的中央结算横幅，要求先观察到对局、两次间隔观测。模型自报 win 不会成为胜利证据。OCR 规则仍需真实游戏截图验证。
- 正式晋级还需要独立授权评测方签名，绑定运行、版本、配对、阵营、对手、胜负、调用数、录像和日志哈希。当前通过文件导入证明；**独立评测服务、自动安排对局、对手/英雄/地图控制尚未接入**。自己生成密钥、自签结果不算独立实机验收。
- 收齐所有 200 个有效结果才评估：胜率至少提高 5 个百分点；配对单侧精确二项检验通过；模型调用成本不高于父版本 110%。跨候选的显著性预算为 0.05 / (n × (n + 1))，n 为累积登记次数，取消或拒绝不重置。
- 通过后原子更新已接受版本，下一轮只能以它为父版本。拒绝和回滚保留记录；再次优化仍需新父版本的开发证据。不会用同批对照数据兼任下一轮训练数据。

完整签名格式见 [ARENA_PROTOCOL.md](ARENA_PROTOCOL.md)。Protocol.compare 是旧诊断接口，永远不能晋级；实际状态转换由新账本和固定评测器负责。手机 Java 实现与仓库 Python rsi-core 目前是两个实现，尚未统一存储协议。

## 构建与检查

JDK 17–21、Gradle 8.9、Android SDK 35，AGP 8.7.3。中文 OCR 模型随 APK 打包，不依赖首次下载模型。设置 JAVA_HOME、ANDROID_HOME 后执行：

    gradle -p android-app testDebugUnitTest lintDebug assembleDebug

可设置 GRADLE_USER_HOME 及 BAMBOO_ANDROID_BUILD_ROOT 将缓存、构建输出移到空间充足的磁盘。默认 APK 位于 app/build/outputs/apk/debug/app-debug.apk；指定构建根目录时位于其 app/outputs/apk/debug/app-debug.apk。这是开发签名包。

测试覆盖模型非法响应、结果误识别、崩溃预算、账本篡改、配置变化、错误签名、未签名战绩拒绝、完整合成对照、晋级继承与回滚。夹具只存在于 JUnit 临时目录，不能计作实机验收。CI 不连接游戏或真实账号。

## USB 部署与设备检查

USB 仅用于安装和诊断，游戏执行与 RSI 留在手机。工具要求 Python 3.11+、Android platform-tools；可用 `--adb` 指定 adb 路径，多设备时用 `--serial` 选择。默认报告写入仓库忽略目录 `.runtime/android-device/`，不回显设备序列号、密钥或其他应用的无障碍配置。

```powershell
platform/.venv/Scripts/python.exe android-app/tools/device.py doctor
platform/.venv/Scripts/python.exe android-app/tools/device.py install --apk <开发APK路径>
gradle -p android-app assembleDebugAndroidTest
platform/.venv/Scripts/python.exe android-app/tools/device.py smoke --test-apk <测试APK路径>
```

doctor 只读取 Android 版本、应用安装信息、无障碍状态与可用空间。安装与测试前须正常关闭游戏和 Bamboo RSI；工具检查进程，发现运行则拒绝，不强杀、不卸载、不清除证据、不自动授予系统权限。

设备测试覆盖真实 Android Keystore HMAC 与账本重新打开/中断恢复/篡改拒绝，以及主页重建不启动执行、不改配置。测试使用独立临时账本和密钥，不进入游戏、不调用 Decision、不录屏。**测试 APK 构建成功不等于设备测试已通过；没有连接设备时如实保留阻碍报告。**

腾讯官方 SDK 的范围、接口与后续连接设计见 [TENCENT_INTEGRATION.md](TENCENT_INTEGRATION.md)。目前没有运行中的 Gamecore 适配器。

## 发布与许可

商用阻塞项见 [RELEASE_READINESS.md](RELEASE_READINESS.md)。当前无障碍自主执行方式不符合 Google Play 相关自动化政策；腾讯 Gamecore 研究许可不是商业自动化授权。需要确定经许可的产品用途、执行接口和渠道。

新写源代码遵循 MIT，依赖不因此改为 MIT。中文 OCR 使用 Google ML Kit，在设备本地处理图片，但 SDK 会向 Google 发送性能/使用指标，见 [官方隐私说明](https://developers.google.com/ml-kit/terms) 及 [THIRD_PARTY.md](THIRD_PARTY.md)。本模块不包含腾讯游戏、Gamecore 或模型权重。参考过 hok-agent、Operit、hok_env 的公开架构说明，未复制其控制代码。
