# 客服反馈 API v1

JSON / UTF-8，所有请求需要 `Authorization: Bearer <token>`。写入体最多 100 KB。服务按业务/租户隔离数据库；凭据不出现在请求体、日志或导出中。POST 200 表示已登记，**不表示生产生效**。

| 方法和路径 | 身份 | 作用 |
| --- | --- | --- |
| POST `/v1/events` | ingest | 上报一条脱敏反馈，相同 event_id 和内容幂等 |
| GET `/v1/issues` | read / reviewer | 聚合问题、证据原话、状态及 revision |
| GET `/v1/summary` | read / reviewer | 队列数量、显式评分覆盖，不推定满意度 |
| POST `/v1/reviews` | reviewer | 确认、拒绝、待补充，必须提供 Idempotency-Key |
| POST `/v1/revocations` | reviewer | 撤回指定审核；重复撤回幂等 |
| GET `/v1/approved` | read / reviewer | 仍有效且允许学习的审核候选快照 |

401 缺少凭据；403 角色不允许；409 证据/审核版本或幂等键冲突，应重新读取问题后决定；400 格式不符。错误响应不回显输入。读取凭据可以看到原话，必须作为敏感权限管理。

## 事件

```json
{
  "event_id": "redacted-call-1-r3-unknown",
  "session_id": "redacted-call-1",
  "customer_group": "opaque-customer-1",
  "round_id": "r3",
  "policy_version": "script-v5",
  "source_system": "aichatcore",
  "origin": "redacted_business",
  "split": "training",
  "kind": "unknown_question",
  "topic": "statement-explanation",
  "user_quote": "这个费用我还是没听懂，可以找人解释吗？",
  "assistant_quote": "我还不能准确解释这项费用，需要请人工核实。",
  "signals": ["explicit_confusion"],
  "hypotheses": [],
  "rating": null,
  "learning_example": null
}
```

可选问题类型：unknown_question、unresolved_reply、repeat_question、confusion、human_request、stop_request、system_failure、learning_sample。字段集合固定，未知字段拒绝。ID 必须为脱敏的不透明标识；`topic` 只是聚合线索，仍待运营确认。

验证集与仅用于调查的记录必须使用 `split=audit_only`，即使审核通过也不进入学习导出。真实业务不允许填写当前模拟训练适配器的 `learning_example`。

若有心理状态推测，只能使用这样的结构；不能设置 confirmed=true：

```json
{"claim":"可能感到困惑，尚待核实","basis_quote":"没听懂","source":"model_inference","confirmed":false}
```

`basis_quote` 必须出现在客户原话里。它不会进入候选学习快照。`rating` 只能是主动评分及证据引用，例如 `{"value":4,"scale":5,"source":"explicit_customer_rating","evidence_ref":"survey-opaque-id"}`；没有评分传 null，不要让模型代打分。相同会话及评分证据不会因多个事件重复计数，模拟与真实来源分开统计。

## 审核

先 GET issues，运营查看原话和来源，随后提交：

```json
{
  "issue_id": "<GET 返回的 issue_id>",
  "expected_revision": "<GET 返回的 revision>",
  "event_ids": ["redacted-call-1-r3-unknown"],
  "decision": "approved",
  "category": "knowledge",
  "summary": "已核实该类费用解释入口；复杂争议由人工核查。",
  "references": ["internal-knowledge:statement-explanation:version-7"],
  "confirmed_intent": null,
  "valid_until": "2027-01-01T00:00:00+08:00"
}
```

请求头 `Idempotency-Key` 是每个逻辑审核操作的唯一 ID。原请求重试沿用，新的审核用新 ID。request body 不能带 actor / actor_kind：这些取认证 Principal。新增事件不会自动继承旧审批。decision 支持 approved/rejected/needs_info；非 approved 时 valid_until=null。

category：annotation 是确认训练标签，knowledge 是事实/FAQ，wording 是话术，strategy 是策略，engineering 是工程事项。approved 必须给来源及未来有效期；审核者负责核实来源。本模块不自动抓取来源文件，也不把审批等同发布。

撤回体：

```json
{"review_id":"<审核返回的 id>","reason":"来源版本已失效，需要重新核实"}
```

## Java / Go

Java 11+ `HttpClient` 和 Go 标准库 `net/http` 都可以直接 POST 上述 JSON。业务端提交 feedback，不传模型凭据或数据库配置；运营端使用独立审核认证。读取事件前应先在业务端做脱敏和会话分组。

Java 示例核心调用（`feedbackJson` 为序列化后的上述对象）：

```java
HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + "/v1/events"))
    .header("Authorization", "Bearer " + System.getenv("BAMBOO_FEEDBACK_INGEST_TOKEN"))
    .header("Content-Type", "application/json")
    .timeout(Duration.ofSeconds(10))
    .POST(HttpRequest.BodyPublishers.ofString(feedbackJson, StandardCharsets.UTF_8))
    .build();
HttpResponse<String> response = HttpClient.newHttpClient()
    .send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
// 检查状态码；只有同 event_id、同内容才可安全重试。
```

Go 核心调用：

```go
req, err := http.NewRequestWithContext(ctx, http.MethodPost,
    baseURL+"/v1/events", bytes.NewReader(feedbackJSON))
if err != nil { return err }
req.Header.Set("Content-Type", "application/json")
req.Header.Set("Authorization", "Bearer "+os.Getenv("BAMBOO_FEEDBACK_INGEST_TOKEN"))
resp, err := (&http.Client{Timeout: 10*time.Second}).Do(req)
if err != nil { return err }
defer resp.Body.Close()
// 检查状态码，读取受限大小响应；不要记录带 token 的请求头。
```
