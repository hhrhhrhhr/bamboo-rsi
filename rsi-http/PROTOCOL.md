# Enterprise HTTP protocol v1

规范的字段和响应类型见 [openapi.json](openapi.json)。以下说明补充跨语言编码、角色分工和恢复语义。控制 API、执行 API 和核验 API 的 origin 分别配置，不要求部署在同一系统或使用同一语言。

## 企业适配接口

### 1. 固定任务清单

`GET /rsi/v1/manifest` 返回：

```json
{
  "protocol": "bamboo-rsi-http/1",
  "mode": "read_only",
  "task": {"id": "acme.inventory_read", "...": "完整 Task schema 见 OpenAPI"},
  "revisions": {"executor": "<sha256>", "verifier": "<sha256>", "dataset": "<sha256>"},
  "initial_files": {"api-policy.json": "{\"read_mode\":\"single\"}"},
  "training": {"policy": {"read_mode": ["single", "batch"]}},
  "cases": [{"id": "case-1", "inputs": {"snapshot": "immutable-snapshot-1"}}]
}
```

上例为结构示意。实际清单包含完整 `task` 字段，至少每轮所需的独立验证案例；运行下一轮需要新的案例。训练信息是企业服务主动脱敏后的投影，不应包含验证答案、用户隐私或密钥。原始病例/订单/审计记录可以留在企业域，`inputs` 只携带快照标识，核验服务通过自己的只读接口读取真实结果。

`revisions` 应覆盖实际服务实现/镜像、配置与数据快照，而不是永不变化的“v1”标签。客户端通过 `inspect` 输出清单承诺值，将该 SHA256 写入私有 catalog。SDK 还会固定本地协调代码、任务指标与核验信任。企业执行服务必须拒绝清单/实现版本不匹配的请求，不能在相同 revision 下静默换代码或数据。

### 2. 执行一个冻结版本

`POST /rsi/v1/execute`，请求体是 `ExecutionPacket`，header `Idempotency-Key` 等于 `request.execution_id`。请求中有：

- `request_sha256`：Bamboo 对 `request` 对象的承诺值。
- `request.version`：版本 ID、父版本 ID、任务/契约与候选文件，不能只使用“最新配置”。
- `request.case`：预先登记的验证案例；不可临时挑选更容易的样本。
- `request.manifest_sha256`、`revisions`、`mode`：执行的固定边界。

企业服务按允许的策略执行只读/影子工作，保存请求 ID、实际使用的版本、原始结果和业务审计记录，返回 `Execution`。`result` 可以包含输出值、内部审计记录 ID 或查询回执，但不能仅返回一个“成功=true”代替真实结果。执行器自报的成本不直接决定晋级。

同一执行 ID 的相同请求只能对应一次实际执行；完成后重试应返回原始响应。相同 ID、不同内容应返回 409。已记录开始但不能确定是否完成时也应拒绝重发，并保留现场进行对账。示例实现将请求和响应写入持久日志，缺失响应时不会重新调用业务 API。

### 3. 独立核验

`POST /rsi/v1/verify`，请求体 `VerificationPacket` 包含原始执行请求、原始响应和 `evidence_sha256`。独立核验服务应：

1. 核对请求/案例/候选版本与自己的固定任务清单。
2. 从权威数据源、隔离审计日志或独立结果接口读取真实结果，不能仅签署执行器传来的成功标志。
3. 重算正确性与全部指标。若未达到要求，返回签名的 `success:false`；无法核验则失败，不签“成功”。
4. 在判定中绑定 task、manifest、verifier revision、execution、version、case、request hash 和 evidence hash。
5. 签署 `VerificationPayload` 并返回 `Proof`。

RSI 在晋级与读取已接受版本时，重新校验本地原始证据、版本绑定和核验凭证。这一步不再次访问远端，也不重复业务执行。要防止执行器自行给自己打分，核验签名密钥必须由独立核验服务管理，不交给候选和业务执行器。

## 跨语言 JSON 与签名

请求中已有的 `request_sha256`、`manifest_sha256`、`evidence_sha256` 是 Bamboo 生成的绑定值，远端按原值核对、回传，并进行语义和审计核验。Bamboo 的内容哈希采用 UTF-8 JSON：对象键排序、无额外空格、非 ASCII 直接 UTF-8、禁止 NaN/Infinity/重复键。如果在远端复算这些哈希，应使用完全一致的规范化方式；不要假定任意 JSON serializer 的字节都相同。货币推荐整数最小单位，避免跨语言浮点格式差异。

**核验签名不要求远端复制 Python 的序列化细节**：

```text
payload = 你自己的 JSON 库将 VerificationPayload 编码成 UTF-8 字节
payload_base64 = 标准 Base64(payload)
signature_hex = lowercase_hex(HMAC-SHA256(UTF8(verification_key), payload))
```

验证方先对 Base64 解码得到的原始字节检查 HMAC，再解析 JSON，并逐字段核对绑定。不要签完以后重新序列化 payload。Java 可用 `Mac.getInstance("HmacSHA256")`，Go 可用 `crypto/hmac` 和 `crypto/sha256`；双方的信任密钥均为相同 ASCII 文本的 UTF-8 字节，不进行额外 Base64 解码。

`measurement.metrics` 必须恰好包含 TaskSpec 声明的所有指标，值为非负有限数；`success` 必须是真正的 JSON 布尔值。核验失败不能通过修改签名或指标字段绕过。

## 控制接口与操作状态

控制请求使用独立 Bearer token。部署时配置可调用的集成，客户端不能提供任意 URL 或插件导入路径。创建实验与所有动作请求必须带 16–128 字符幂等键。控制幂等键可使用 UUID；企业执行幂等键固定为协调器产生的 32 位小写十六进制 execution ID。

```text
POST /v1/experiments
  {"experiment":"inventory-2026-01","integration":"acme.inventory_read"}

POST /v1/experiments/inventory-2026-01/actions
  {"operation":"prepare"}

GET /v1/operations/<operation-id>
GET /v1/experiments/inventory-2026-01
```

一次控制操作的 `id` 与 RSI 的 `job` 不同：`prepare` 操作完成后，`result.job` 才是随后 `advance/finish/recover` 使用的作业 ID。`succeeded` 表示这个控制步骤完成，还要检查 `result.status`：`accepted` 是晋级，`rejected` 是候选被拒绝，`no_change` 表示本轮没有新候选。拒绝一个候选可以是正常完成的评测。

操作 SQLite 日志在排队前提交；有状态未知的执行时不会重新调用。响应丢失后，用原幂等键和相同内容重发，返回原操作；修改内容会报冲突。宿主重启把未完成操作标成 `interrupted`。先查询实验的 `pending` 作业，再显式 `recover`，不能靠生成新请求 ID 假装旧执行没有发生。

默认串行 worker 不排无限队列。繁忙时新执行返回 `409 worker_busy`；客户端稍后可重试同一尚未受理的请求，已受理的请求始终查询原操作。`stop` 立即写入检查点标记，HTTP 调用有有限期限；`resume` 不自动续跑未知结果。

## 适用范围

库存查询、订单对账、检索/路由、只读报表与业务流程的影子执行可用本协议做初次接入。写入生产数据库、扣款、发送消息、灰度发布等有副作用的动作需要业务系统自己的权限、幂等、补偿和发布控制；当前远程模式没有打开这些动作。SDK 的接受版本是内部候选晋级，不是生产发布审批。

本包中的演示与 API-supplied 候选有明确来源标签。不能把一个本地协议测试说成真实企业 RSI 已成立；真实验收还需真实任务数据、业务独立裁判、合适的对照设计和从晋级父版本继续产生改进的证据。
