# Bamboo RSI

基于 Bamboo / StartLux Decision 的原神每日委托执行与受限策略优化实验。Windows 桌面端采集游戏画面，由 Decision 选择任务级技能，再由 BetterGI 执行路线、战斗和交互；任务结果通过委托簿截图重新核验。

**开发预览，RSI 完整实机验收尚未完成。** 已验证部分真实委托；合成测试通过、开发试跑成功和同日部分进度都不能视为无人干预的完整每日或策略晋级。本项目没有米哈游官方授权声明，游戏使用与传播需遵守适用规则。

## 包含什么

- `desktop/`：Windows 桌面/语音界面，本机设置与 DPAPI 凭据存储。
- `platform/`：执行内核、原神适配器、运行证据、录像归档、RSI 协调器及回归测试；保留文件账目示例用于验证通用平台。
- `integrations/`：固定版本 AutoCommission 源码、BetterGI 改造补丁及战斗策略。
- `scripts/`：可复现构建、开发试跑和停止入口。

源码来自 [Game Harness Lab 的 hhr-yuanshen 分支](https://github.com/dctongsheng/game-harness-lab/tree/hhr-yuanshen)，底座提交 `1a7b0ca`，包含截至 2026-10-21 的本地原神 RSI 开发增量。本仓库以新的源码快照开始历史，聚焦原神 RSI；不携带旧小游戏实验、旧安装包、本机账号配置、运行记录或录像。

## 安装与离线验证

需要 Python 3.11–3.13。真实客户端目标为 Windows 11 22H2+ x64；桌面/原生源码构建需要 .NET 8 SDK，JS 测试需要 Node.js。所有下面的测试均使用测试数据，不登录游戏、不调用你的模型账号。

```powershell
git clone https://github.com/hhrhhrhhr/bamboo-rsi.git
cd bamboo-rsi
py -3.11 -m venv platform/.venv
platform/.venv/Scripts/python.exe -m pip install -r requirements-genshin.lock
platform/.venv/Scripts/python.exe -m pip install --no-deps -e ./platform
platform/.venv/Scripts/python.exe -m pytest platform/tests -q
node --test integrations/auto_commission/agent-main.test.cjs
python scripts/check_public_tree.py
```

Harness² 相关测试需要可选优化依赖，安装方式及 Linux/WSL 环境见 [平台指南](platform/README.md)。当前默认 RSI 后端为 `decision_search`，只通过 Decision 从开发者定义的策略/战斗变体中选择；另有 `harness2_chat` 后端。这不是任意源码自我改写或模型权重训练。

## 构建 Windows 客户端

首次源码发布不附带旧版 EXE，避免将旧安装包误认为当前版本。

```powershell
platform/.venv/Scripts/python.exe scripts/setup_genshin_bridge.py --checkout .runtime/bettergi
platform/.venv/Scripts/python.exe -m pip install -r requirements-desktop.lock
platform/.venv/Scripts/python.exe scripts/build_genshin_desktop.py --bettergi .runtime/bettergi
```

构建输出在 `dist/genshin-desktop`；该目录里的 `StartLux.Genshin.exe` 可用于设置和启动。构建会下载固定版本第三方依赖及中文语音模型。要生成安装程序，另安装 Inno Setup 6 后编译 `desktop/installer.iss`。原生程序与游戏权限需要匹配，程序不绕过 Windows 授权。

## 配置自己的 Bamboo 账号

**本仓库不附带作者的 Key、Bamboo 登录会话或游戏账号。公共 API 地址和模型名本身不会使调用计入作者账号。** 每位使用者必须在桌面客户端填写自己的 Bamboo Key、模型、游戏路径和 UID。

- 当前模型标识：`startlux-decision-27b-autothink`。凭据仅用作接口认证，费用/额度属于提供该凭据的账号，价格和可用性以服务商实际规则为准。
- 支持 `https://api.bamboonode.dev/v1/decisions` 与 `https://api.bamboonode.cn/v1/decisions`。桌面默认使用 `.dev`；开发运行的本机 `settings.json` 可以设置 `decision_url` 为 `.cn`。不要把 Key 填进 URL、源码或提交记录。
- 桌面端把设置写入 `%LOCALAPPDATA%/StartLux/Genshin/settings.json`，Key 经当前 Windows 用户的 DPAPI 加密保存在同目录 `credential.dpapi`。加密凭据文件也属于私密文件，不应提交或分享。
- `config/settings.example.json` 是空账号模板，不是可直接运行的账号配置。通过桌面端保存自己的 Key 后使用；源码开发入口支持独立的 `.runtime/genshin-home/`。
- 若想用开发入口运行本次构建，把构建输出的绝对路径写入本机 `.runtime/install-root.txt`。配置自己的战斗策略后，使用 `Test-Genshin-Dev.cmd`。停止入口为 `Stop-Genshin-Dev.cmd`。

决策会向第三方模型服务发送游戏截图、任务状态、UID 范围信息及可选动作，属于联网调用。运行日志、截图与视频也可能含账号信息；公开分享前需另行检查。仓库的忽略规则和发布检查只是防误提交措施，不能代替密钥轮换。已公开或分享过的 Key 应在服务商处撤销并换新。

## RSI 验收边界

协调器保留失败证据、固定运行版本、限制恢复次数；候选使用连续 14 个游戏日的基线/候选交替对照，各 7 天，真实通过后才能晋级，下一代必须继承已接受版本。开发者介入单独记录，不能用合成数据替代实机验收。沿途必需战斗必须真实完成，超时不能当作清怪成功。

完整初始化、WSL 依赖、试跑、录像与版本恢复见 [平台指南](platform/README.md)。不要在游戏输入运行中修改冻结代码、配置或执行器。

## 开源许可与来源

本项目原创的 RSI 增量、启动工具及相关文档采用 **[MIT](LICENSE)**，文件级范围见 [许可清单](LICENSE_SCOPE.json)。这是对本项目原创贡献的授权，不覆盖继承的 GPL 框架、BetterGI、AutoCommission 及其派生部分。

从 Game Harness Lab 继承的框架保留 **GPL-3.0-or-later**；BetterGI、AutoCommission 及派生集成继续遵守各自 GPL 许可。当前 Python 包同时包含 MIT 原创增量和 GPL 继承代码，因此包元数据标为 `MIT AND GPL-3.0-or-later`。当前完整发行物不能作为全仓纯 MIT 软件分发；分发 GPL 覆盖的组合程序仍需满足相应 GPL 要求。

第三方作者、许可证与来源均保留，详见 [第三方说明](THIRD_PARTY_NOTICES.md)、`platform/LICENSE`、`platform/LICENSE.MIT`、`platform/LICENSE.Harness2` 和上游目录中的许可证。游戏名称、画面及识别模板涉及的游戏知识产权不因本仓库开源而转移。
