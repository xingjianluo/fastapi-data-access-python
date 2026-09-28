# fastapi-data-access-python

一个把数据访问层做扎实的 FastAPI 示例：键集分页（翻页期间写入/删除、排序值
重复均不重不漏）、乐观锁并发更新（无静默覆盖）、多写操作事务（失败整体回滚）、
业务错误与基础设施错误明确分类。存储为本地线程安全内存表，无需外部依赖。

## 快速开始

```bash
uv sync --group dev
uv run python main.py          # 启动服务，访问 /docs
uv run pytest                  # 运行全部单测（日志含输入与判定依据）
```

## 接口概览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/records?sort_by=&order=&limit=&cursor=` | 键集分页列表 |
| POST | `/records` | 创建（body: name, score） |
| GET | `/records/{id}` | 详情 |
| PUT | `/records/{id}` | 更新（body: name?, score?, expected_version） |
| DELETE | `/records/{id}` | 删除（body: expected_version） |
| POST | `/records/transaction` | 多写操作原子提交（body: {"ops": [...]}） |

## 关键约定

- 分页：游标携带 `(排序字段, 方向, 排序值, 主键 id)`，复合键严格比较；
  非法/失效参数返回带可区分 `reason` 的 400。
- 并发：更新/删除必须带 `expected_version`，版本不符返回 409
  `version_conflict`，撞唯一约束返回 409 `unique_conflict`。
- 事务：任一步失败整体回滚，错误为 `transaction_aborted` 并带
  `cause_type/cause_reason`；容量满为 507 `storage_full`（基础设施），
  字段非法为 422 `business_validation`（业务），底层异常不透传给调用方。

详细设计、错误码表与手工复现步骤见 [docs/design.md](docs/design.md)。
