# 数据访问层约定与本地验证

本服务的数据访问层基于本地 SQLite（文件库 + WAL；测试中用临时文件库）构建，目标是在**数据量增长**与**并发写入**下保持结果一致、可解释。本文说明四件事：

1. 分页约定（为什么翻页期间写入/删除/排序值重复都不会重项或漏项）；
2. 并发控制约定（如何避免后写者静默覆盖）；
3. 事务与错误分类规则（业务错误 vs 基础设施错误如何区分）；
4. 本地验证方法。

---

## 1. 分页约定：键集分页（keyset / seek pagination）

### 1.1 排序键与游标

- 固定排序：`ORDER BY created_at DESC, id DESC`，接口不允许自定义排序。
  `created_at` 可能重复，追加主键 `id` 作为**最终决胜列**，使排序键构成全序。
- 游标是不透明 token：`base64url(JSON({"ts": <created_at>, "id": <id>}))`。
  调用方只能从上一页响应的 `next_cursor` 原样回传，不应自行构造或解析。
- 查询条件取“严格位于锚点之后”的行：

  ```sql
  WHERE created_at < :ts
     OR (created_at = :ts AND id < :id)
  ORDER BY created_at DESC, id DESC
  LIMIT :limit + 1
  ```

  多取 1 行用于判断是否还有下一页；游标使用**严格不等**，锚点行不可能在相邻两页重复出现。

### 1.2 翻页期间的并发数据变化

| 翻页期间发生的事 | 行为 |
| --- | --- |
| 写入“更老”的新行 | 落在游标方向内，后续页可正常翻到，不重不漏 |
| 写入“更新”的新行 | 属于更早的页；从旧游标继续翻页看不到它，**从头重新发起查询**时才出现且只出现一次。它不会把任何旧行挤出结果 |
| `created_at` 相同的行 | 由 `id DESC` 打破并列，严格全序，不会重复 |
| 尚未翻到的行被删除 | 结果少一行，其余行位置不变，不会重复 |
| 游标锚点行被删除 | 判定游标失效，返回 `409 STALE_CURSOR`，调用方应放弃旧游标，从第一页重新开始 |

对比 `OFFSET/LIMIT`：OFFSET 分页在写入/删除时会发生整页位移（漏项或重复项），键集分页的页边界锚定在数据值上，不受位移影响。

### 1.3 分页参数的拒绝原因（可区分）

| HTTP | code | reason | 触发条件 |
| --- | --- | --- | --- |
| 422 | `INVALID_PAGINATION_PARAM` | `limit_not_positive` | `limit <= 0` |
| 422 | `INVALID_PAGINATION_PARAM` | `limit_too_large` | `limit > 100` |
| 422 | `INVALID_PAGINATION_PARAM` | `limit_wrong_type` | 非整数（含布尔值） |
| 422 | `INVALID_CURSOR` | `cursor_empty` / `cursor_malformed_base64` / `cursor_bad_json` / `cursor_bad_payload` | 游标**无法识别**（格式/编码/字段问题） |
| 409 | `STALE_CURSOR` | `cursor_anchor_deleted` | 游标格式合法，但锚点行已不在当前数据集中（**已失效**） |

默认 `limit=20`，最大 `100`。`INVALID_CURSOR`（你传的东西不合法）与 `STALE_CURSOR`（东西合法但过期）是两类不同拒绝。

### 1.4 响应示例

```json
{
  "items": [ { "id": "a4", "title": "...", "content": "...", "version": 2,
               "created_at": "...", "updated_at": "..." } ],
  "next_cursor": "eyJ0cyI6Li4uLCJpZCI6ImE0In0",
  "has_more": true
}
```

---

## 2. 并发控制：version 乐观并发（OCC）

- 每行带整数 `version`，创建时为 1，每次更新 `version = version + 1`。
- 更新必须携带 `expected_version`（调用方基于自己读到的版本）。更新语句为条件更新：

  ```sql
  UPDATE articles
     SET title = COALESCE(传入值, title),
         content = COALESCE(传入值, content),
         version = version + 1,
         updated_at = :now
   WHERE id = :id AND version = :expected_version
  ```

- 判定依据：`rowcount == 0` 时回查该行——
  - 行不存在 → `404 RECORD_NOT_FOUND`；
  - 行存在但版本不同 → `409 VERSION_CONFLICT`，错误体携带 `expected_version` 与服务端 `actual_version`，明确告知“你基于的版本已被他人推进”。
- 因此**后写入者不会静默覆盖先写入者**：陈旧版本的写入被明确拒绝，调用方应重新读取最新版本、合并后带新版本号重试。
- 写事务使用 `BEGIN IMMEDIATE`，在事务起点申请写锁（SQLite 单写者 + `busy_timeout=5s`），把锁等待集中在一处；条件更新在同一事务内完成，读-判-写窗口不会产生竞态。

冲突响应：

```json
{ "error": { "code": "VERSION_CONFLICT", "category": "BUSINESS",
  "message": "记录已被其他事务修改，版本不匹配",
  "reason": "expected_version != current_version",
  "details": { "id": "doc1", "expected_version": 1, "actual_version": 2 } } }
```

---

## 3. 事务与错误分类

### 3.1 单请求批量写

`POST /articles/batch` 的多个操作在**单个数据库事务**内顺序执行：

- 任一步业务校验失败 / 唯一冲突 / 版本冲突 / 记录不存在 → 立即 `ROLLBACK`，前面已执行的步骤全部不落库，返回 `409 BATCH_ABORTED`，`details.failed_index` 指明第几步失败、`cause_code` 指明根因；
- 锁、磁盘等基础设施错误 → 同样回滚，但保持基础设施分类（5xx），不伪装成业务错误；
- 全部成功才 `COMMIT`。

### 3.2 错误分类总表

所有错误都折叠为稳定 JSON：`{"error": {"code", "category", "message", "reason", "details"}}`。
`category` 只有两类：`BUSINESS`（调用方可通过修改请求解决，4xx）与 `INFRASTRUCTURE`（调用方无法通过修改请求解决，5xx）。
底层 `sqlite3.Error`（含 SQL、文件路径等内部信息）**不直接透传**，统一由 Repository/连接层翻译。

| HTTP | category | code | 含义 |
| --- | --- | --- | --- |
| 422 | BUSINESS | `INVALID_PAGINATION_PARAM` / `INVALID_CURSOR` / `VALIDATION_FAILED` | 参数不合法 |
| 409 | BUSINESS | `STALE_CURSOR` | 游标过期 |
| 409 | BUSINESS | `VERSION_CONFLICT` / `RESOURCE_ALREADY_EXISTS` / `BATCH_ABORTED` | 并发冲突 / 唯一冲突 / 批量回滚 |
| 404 | BUSINESS | `RECORD_NOT_FOUND` | 记录不存在 |
| 503 | INFRASTRUCTURE | `INFRA_UNAVAILABLE` | 写锁被占（`SQLITE_BUSY/LOCKED`），可稍后重试 |
| 507 | INFRASTRUCTURE | `INFRA_STORAGE_FULL` | 磁盘/存储资源已满（`SQLITE_FULL/IOERR`） |
| 500 | INFRASTRUCTURE | `INFRA_INTERNAL` | 其余未归类数据访问错误（兜底，不泄露内部细节） |

### 3.3 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/articles?limit=&cursor=` | 键集分页 |
| POST | `/articles` | 创建（body: `id,title,content`） |
| PUT | `/articles/{id}` | 乐观更新（body: `expected_version` + `title`/`content` 至少一个） |
| POST | `/articles/batch` | 单事务批量写（`operations`，用 `action: create/update` 判别） |

---

## 4. 本地验证

```bash
# 依赖（fastapi 与 pytest 已在本机虚拟环境中；如用 uv 可自行 sync）
python -m pytest tests/                      # 全量：29 个用例，本地 SQLite，无需外部服务
python -m pytest -o log_cli=true             # 实时打印“输入 + 判定依据”日志
python -m pytest tests/test_concurrency.py   # 仅并发：线程屏障复现同记录并发更新
python -m pytest tests/test_pagination.py    # 仅分页：翻页期间写入/删除/排序值重复/非法参数
python -m pytest tests/test_transaction.py   # 仅事务：回滚 + SQLITE_BUSY/FULL 翻译

# 手动起服务
python main.py                               # 默认库文件 data/app.db（自动建目录/建表，WAL）
# GET  /articles?limit=20&cursor=...
# POST /articles            {"id":"a1","title":"t","content":"c"}
# PUT  /articles/a1         {"expected_version":1,"title":"t2"}
# POST /articles/batch      {"operations":[{"action":"create","id":"b1","title":"t","content":""}]}
```

测试与日志要点：

- 用例全部使用 `tmp_path` 下的**本地文件型 SQLite**，并用线程屏障/受控时钟让并发与排序场景确定可复现；
- `app.data` logger 在每个写/分页入口打印**输入**（limit、cursor、id、expected_version），在判定分支打印**判定依据**（version matched / version_mismatch / anchor_deleted / rollback index 等）；
- 并发用例断言“恰好一方成功、另一方 `VERSION_CONFLICT` 且败者写入未落库”，并额外用持有写锁的线程验证锁竞争归为 `INFRA_UNAVAILABLE(503)` 而非业务冲突。
