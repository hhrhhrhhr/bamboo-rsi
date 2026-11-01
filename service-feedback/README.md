# 客服体验反馈与运营审核

让客户少重复、得到可靠解释、可以暂停或转人工。系统不知道的问题先记录，再由运营核实；审核结果进入知识、话术、策略或工程改进候选。候选仍须独立评测，才可晋级并由下一轮继承。

本模块独立于 `collections-lab`，保留上一轮模拟实验的冻结代码和原始证据。不会外呼、发消息给运营、修改 aichatcore 数据库或替换生产话术。当前完整 RSI 接入仍是**模拟会话离线验收**，不能据此认定真实客户更舒服。

## 事实、推测和审核

| 信息 | 处理方式 |
| --- | --- |
| 客户原话、助手回复、会话/轮次、版本 | 保留脱敏证据及内容哈希，可追溯 |
| 明确说没听懂、重复提问、希望结束或转人工 | 保存可观察信号；业务执行器应立即尊重这些要求，不能等审核后才结束 |
| AI 不知道、知识冲突、业务接口异常 | 汇总为待核实问题，不能编造确定答复 |
| “可能焦虑/不信任”等心理推测 | 必须标记模型推测、未确认并引用依据；结构化推测字段不进入学习材料 |
| 客户主动评分 | 单独记录证据；没有评分是未知，沉默、短通话、答应还款都不算满意 |
| 运营核实的事实/标签 | 绑定证据版本、审核人、来源、有效期，可拒绝、待补充或撤回 |
| 系统故障、流程不合理 | 导出工程改进事项，后续作为代码候选输入；不能用话术掩盖缺陷 |

“舒服”应在真实业务中结合解决率、重复解释负担、投诉、退出/转人工请求是否被尊重，以及自愿评分验证。不能只追求更短通话、更高承诺还款率，也不能把推测的心理弱点用来增加施压。

## 已实现

- 同一事件幂等导入，内容冲突拒绝；按来源、数据类型、用途、问题类型及分类线索聚合。分类线索本身不是运营已确认的标签。
- 审核只能由配置的 reviewer 凭据提交，业务数据接入凭据无审核权。审核人来自服务端身份配置，不信任请求体的 actor。
- 审核指定具体事件，并校验当前问题版本。新发生的问题仍需审核；并发旧版本审核失败，避免覆盖新证据。
- 审核、撤回及导入保留审计链。审批过期、撤回、拒绝、待补充、验证集材料均不出现在可学习快照中。
- 五类产物：`annotation`、`knowledge`、`wording`、`strategy`、`engineering`。导出是候选材料，审批并不直接发布。
- 实际接入现有 SDK 的 `business.reviewed_collection_support`：仅使用运营确认且与本轮训练快照一致的模拟记录；心理推测、未来批次、验证集不进入优化器。
- 候选绑定审核证据；构建、执行、晋级前重新检查有效性。晋级检查与撤回写入串行化。后续撤回会阻止新执行/继承，旧历史不会被改写。
- 冻结的独立核验器在原有处理损失和约束事件之外，检查**模拟对话中的重复/未解决信号、联系偏好违反数**不得倒退。保留原 12 组配对和逐轮阈值；不修改游戏的正式验收条件。

已审核的自由文本知识、话术、策略和工程事项目前能导出，但还没有相应业务执行器；不会自动把它们写入生产 Prompt 或启动代码修改。当前 RSI 优化器只接受 `annotation` 类审核，继续在已限定的模拟话术库和路由范围内学习。

## 使用

在仓库根目录安装：

```powershell
platform\.venv\Scripts\python.exe -m pip install -e ./rsi-core -e ./rsi-sdk -e ./rsi-http -e ./collections-lab -e ./service-feedback
```

把之前模拟数据中的**训练部分**加入待审核队列（不会自动审批）：

```powershell
bamboo-feedback import-collection --inbox .runtime/service-feedback/reviews.sqlite3 --snapshot .runtime/collections-simulation-20261029/incoming/wave-1.json
bamboo-feedback report --inbox .runtime/service-feedback/reviews.sqlite3 --destination .runtime/service-feedback/report-1
```

`report.html` 是本地阅读材料，`issues.json` 保留原话及审核版本，`approved-candidates.json` 导出仍有效的确认内容；所有文件应留在忽略目录。工具不向运营发送消息。

### Java / Go 接入

使用 [HTTP 协议](PROTOCOL.md)。不要求业务项目嵌入 Python。按业务/租户分别部署独立 inbox；当前服务不是共享的多租户系统。

`serve` 从进程环境读取三枚不同的长随机凭据，不落盘：

- `BAMBOO_FEEDBACK_INGEST_TOKEN`：业务上报。
- `BAMBOO_FEEDBACK_REVIEW_TOKEN`：运营审核，绑定 `--reviewer-id`。
- `BAMBOO_FEEDBACK_READ_TOKEN`：读取审核材料，具有敏感业务数据读取权限。

```powershell
bamboo-feedback serve --inbox .runtime/service-feedback/reviews.sqlite3 --reviewer-id ops-user-opaque-id --port 8791
```

只监听回环地址。企业远程接入需要自己的 TLS、用户登录/SSO、权限映射和审计接入；不要把该开发服务直接暴露公网。认证令牌不能传在 URL 中。当前 CLI 的单个 reviewer 身份适合本地联调，多人运营须由企业认证层将真实操作者绑定到各自的 Principal。

`from_session_turn` 提供 aichatcore 的 `SessionTurn` 白名单映射，不读取 LLM Prompt、账户对象、工具调用或音频。业务必须先脱敏原话和 ID；这个函数不会自动发现并清除自由文本里的个人信息。

### 经过审核的模拟 RSI

新的实验必须使用 `ReviewedWorkflow`（包含晋级前撤回检查），不是普通 `Workflow`。SDK 插件注册提供集成发现；现有通用 HTTP 控制器的默认 Workflow 尚未包含本模块的审批发布检查，因此该插件**不应直接装进默认 Gateway 自动运行**。先使用下列入口：

```powershell
bamboo-feedback step --inbox .runtime/service-feedback/reviews.sqlite3 --intake .runtime/reviewed-experiment/intake --home .runtime/reviewed-experiment/workflow
```

intake 的模拟批次仍用 `bamboo-collections ingest` 创建。未审核时，提案为空、版本不晋级；该次提案和已登记验证批次仍算已消耗，不能审批后反复复用同一验证集。

为了验证全链路，可在完全独立的目录明确模拟运营：

```powershell
bamboo-feedback rehearse --simulate-operator --destination .runtime/reviewed-rehearsal-new
```

它创建三批模拟会话，以 `test_fixture / simulated-operator` 身份确认情景标签，执行原始 SDK 的构建、对照、晋级、继承、预算及重启核验。结果明确记录 **actual_human_reviews=0、real_model_calls=0**。测试夹具不能确认真实业务记录；该示例绝不代表已有运营人员审批。

撤回已被接受的审核不会抹除原始实验，下一次使用会阻断。需检查撤回影响，使用 SDK 的 `recover` 解决已中止任务，再用 `rollback` 退回仍有效的版本，或建立新的经审核候选。不能通过修改旧审批记录解除阻断。

## 边界与下一步

目前已经有待审核数据入口、角色隔离、审计、模拟业务评测和递归继承。真实外呼还需要业务提供脱敏会话、实际问题解决标签、主动评分、有效知识来源及生产发布接口，才能单独登记真实影子评测；不能把模拟记录的 origin 改成真实就算验收。

这不是心理诊断系统，未接入音频、声学情绪识别或真实通话满意度采集。未知问题的登记不等于已解决，运营审批来源是审核者声明，并未自动验证外部文档正确性。本地审计链用于发现意外修改，不是防数据库管理员重写的外部签名存证。

```powershell
platform\.venv\Scripts\python.exe -m pytest service-feedback/tests -q -c service-feedback/pyproject.toml
platform\.venv\Scripts\python.exe scripts/check_public_tree.py
```

原创实现采用 [MIT](LICENSE)。
