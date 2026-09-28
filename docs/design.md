# 数据访问层设计与约定

本服务演示在数据量增长与并发写入下，如何让分页与写入结果**一致且可解释**。
存储为线程安全的本地内存表（无需外部依赖即可复现全部行为），
分层为：

```
api        HTTP 接口：参数解析、错误分类映射、统一错误信封
service    业务校验、事务编排（预校验 + 整批提交）
repository 领域读写：键集分页、乐观锁写、多操作原子提交
storage    线程安全内存表：锁内快照、工作副本暂存、容量约束
pagination 游标编解码、分页参数校验
errors     统一错误分类
```

## 1. 分页约定（键集分页 / Keyset Pagination）

列表接口：`GET /records`

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `sort_by` | `created_at` / `score` | `created_at` | 排序字段 |
| `order` | `asc` / `desc` | `asc` | 排序方向 |
| `limit` | 1..100 的整数 | 20 | 每页条数 |
| `cursor` | 上一页返回的不透明游标 | 无 | 不传表示第一页 |

响应：

```json
{
  "ok": true,
  "items": [ {"id": 1, "name": "a", "score": 10, "created_at": 1, "version": 1} ],
  "next_cursor": "eyJpIjox...",
  "has_more": true
}
```

### 为什么不用 offset

offset 分页在“翻页期间有新数据写入 / 数据被删除”时会错位，
出现重复或漏项。本服务改用**键集分页**：

- 游标内容为 `base64url(JSON)`，携带四要素：
  生成游标时的排序字段 `s`、排序方向 `o`、末条记录的排序值 `v`、末条主键 `i`。
- 下一页以复合键严格比较定位：
  - `asc`：`(sort_value, id) > (v, i)`
  - `desc`：`(sort_value, id) < (v, i)`
- `id` 作为并列决胜键（tie-breaker），即使排序值完全相同，顺序也是确定的，
  因此**排序值重复不会造成重复/漏项**。
- 每次查询都基于当前数据快照即时过滤：
  - 翻页期间**新增**的行，若落在游标窗口之后，会在后续页出现且仅出现一次；
    若落在窗口之前，则不会倒灌进已翻过的页（符合稳定快照语义）。
  - 翻页期间**删除**的行直接从结果集中消失，不会导致相邻行重复出现。
  - 任何一行在整遍翻页中最多出现一次（`id` 严格比较，无“边界相等回退”问题）。
- 游标是不透明字符串，调用方不应自行构造；**游标与当前 `sort_by`/`order`
  不一致即判定失效**，返回可区分的拒绝原因。

### 非法/失效分页参数的拒绝原因

| HTTP | reason | 触发条件 |
|---|---|---|
| 400 | `invalid_sort_by` | 排序字段不在允许集合 |
| 400 | `invalid_order` | 排序方向不是 asc/desc |
| 400 | `limit_not_integer` | limit 不是整数 |
| 400 | `limit_too_small` | limit < 1 |
| 400 | `limit_too_large` | limit > 100 |
| 400 | `cursor_malformed` | 游标无法解码 / 内容缺字段 / 字段类型错 |
| 400 | `cursor_sort_mismatch` | 游标生成时的排序字段与本次请求不一致（游标失效） |
| 400 | `cursor_order_mismatch` | 游标生成时的方向与本次请求不一致（游标失效） |

## 2. 并发控制约定（乐观锁）

每条记录带单调递增的 `version`：

- 创建时 `version = 1`；每次成功更新（含未改值的更新）`version += 1`。
- `PUT /records/{id}` 与 `DELETE /records/{id}` 必须携带
  `expected_version`（>=1 的整数）。
- 提交时在表锁内做原子检查（compare-and-set 语义）：
  - 记录不存在 → `record_not_found`（404）；
  - `expected_version != 当前 version` → `version_conflict`（409），
    **明确失败，不做后写入者静默覆盖**，并在消息中给出期望版本与当前版本；
  - 检查通过才写入并推进版本。
- `name` 有唯一约束，并发创建/改名撞名 → `unique_conflict`（409）。

冲突分类一览：

| 冲突类型 | reason | HTTP | 含义 |
|---|---|---|---|
| 乐观锁版本冲突 | `version_conflict` | 409 | 记录被别人先改过，调用方持有的版本已过期 |
| 唯一约束冲突 | `unique_conflict` | 409 | name 已被占用 |
| 记录不存在 | `record_not_found` | 404 | 更新/删除目标不存在 |

调用方收到 409 后，应重新 GET 最新版本，合并修改后携带新版本重试。

## 3. 事务约定

`POST /records/transaction`，请求体 `{"ops": [...]}`，
操作类型：

```json
[
  {"op": "create", "name": "a", "score": 1},
  {"op": "update", "id": 1, "score": 5, "expected_version": 2},
  {"op": "delete", "id": 2, "expected_version": 1}
]
```

- 一个请求内的多个写操作**作为整体提交**：存储层先在工作副本上顺序套用
  全部变更并逐项做业务前提校验，**任何一步失败都丢弃工作副本整体回滚**，
  已暂存的前序操作也不会落库。
- 操作按顺序在工作副本上可见，因此同一批可以“先 create 再 update 该 id”
  （新建记录版本从 1 开始）。
- 事务失败时返回 `transaction_aborted`，并用 `cause_type`/`cause_reason`
  保留导致回滚的原始分类，同时沿用原始 HTTP 状态码，
  以区分**业务校验失败**与**基础设施失败**。
- 单事务操作数 1..100。

### 错误分类规则

| 大类 | error_type / cause_type | HTTP | 性质 |
|---|---|---|---|
| 分页参数 | `invalid_pagination` | 400 | 调用方参数错误 |
| 业务字段校验 | `business_validation` | 422 | 字段非法 / 空更新 / 空事务等 |
| 记录不存在 | `record_not_found` | 404 | 资源状态不符 |
| 并发冲突 | `version_conflict` / `unique_conflict` | 409 | 可重试/可解释的冲突 |
| 事务回滚（业务原因） | `transaction_aborted`，cause 为上述 4xx | 原 cause 状态 | 整体回滚 |
| 容量打满 | `storage_full` | 507 | **基础设施错误**：资源被占满 |
| 其他基础设施 | `infrastructure_error` | 503 | **基础设施错误** |
| 未预期错误 | `internal_error` | 500 | 兜底，内部细节只进日志 |

关键区分：

- **业务校验错误**（4xx）：调用方可通过修正请求解决，响应消息可安全展示；
- **基础设施错误**（5xx/507）：容量、存储等环境问题，调用方应退避重试。
  底层异常文本（堆栈、内部信息）**只写服务端日志，不向调用方透传**；
  所有响应统一使用错误信封：

```json
{
  "ok": false,
  "error": {
    "error_type": "version_conflict",
    "reason": "version_conflict",
    "message": "记录已被其他写入修改，版本不匹配: 期望版本 1，当前版本 2"
  }
}
```

事务失败额外包含 `cause_type` / `cause_reason`。

## 4. 容量（资源占满）语义

存储层有 `max_records` 上限（应用默认 1000；测试中刻意调小复现）。
容量检查在**所有业务校验通过之后**执行：

- 超限提交 → `storage_full`（507），整批回滚；
- 它与字段非法、版本冲突等业务错误是不同类别，调用方可据此选择“改参数”
  还是“稍后重试”。

## 5. 本地验证方法

```bash
# 安装依赖（含 dev 组的 pytest / httpx）
uv sync --group dev

# 运行全部单测（-s 已在 pyproject 中配置，日志打印输入与判定依据）
uv run pytest

# 只看某一类场景
uv run pytest tests/test_concurrency.py   # 并发更新冲突
uv run pytest tests/test_pagination.py    # 翻页期间写入/删除、排序值重复、非法参数
uv run pytest tests/test_transaction.py   # 事务回滚 / 业务与基础设施错误分类
uv run pytest tests/test_api.py           # HTTP 错误信封与状态码

# 启动服务手工验证
uv run python main.py        # http://127.0.0.1:8000/docs
```

### 手工复现并发冲突

```bash
# 1) 建一条记录，拿到 version=1
curl -s -X POST localhost:8000/records -H 'Content-Type: application/json' \
  -d '{"name":"doc","score":1}'
# 2) 两人各持 version=1 先后更新：第一个 200（version→2），第二个 409
curl -s -X PUT localhost:8000/records/1 -H 'Content-Type: application/json' \
  -d '{"score":2,"expected_version":1}'
curl -s -X PUT localhost:8000/records/1 -H 'Content-Type: application/json' \
  -d '{"score":3,"expected_version":1}'
```

### 手工复现事务回滚与容量打满

应用默认上限较大；代码中 `create_app(max_records=N)` 可调小，
单测 `test_transaction_storage_full_is_infrastructure` 即用 `max_records=3`
复现“资源被占满 → 507 storage_full”，
`test_transaction_all_commit_or_all_rollback` 复现“中间步版本冲突 → 整体回滚”。

## 6. 测试场景对照

| 需求场景 | 测试 |
|---|---|
| 并发更新冲突（16 线程同版本竞争，恰 1 成功） | `test_concurrent_updates_only_one_succeeds` |
| 后写入者静默覆盖防护（串行丢失更新） | `test_stale_update_after_someone_else_committed` |
| 并发同名创建唯一冲突 | `test_concurrent_create_same_name` |
| 旧版本删除被拒 / 不同行并发互不阻塞 | `test_stale_delete_rejected`、`test_concurrent_disjoint_records` |
| 翻页期间写入新数据 | `test_paging_with_inserts_during_walk`、`test_pagination_end_to_end_with_ties` |
| 翻页期间删除数据 | `test_paging_with_deletes_during_walk` |
| 排序值重复（asc/desc） | `test_paging_stable_with_duplicate_sort_values` 等 |
| 非法分页参数（8 种 reason） | `test_invalid_pagination_params`、`test_cursor_sort_mismatch_rejected` |
| 事务回滚（版本/唯一/容量/字段） | `tests/test_transaction.py` 全部用例 |
