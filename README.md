# 社区房屋租赁安全登记

面向街道网格员与社区管理端的房屋安全登记服务。维护**房屋、居住单元、责任人、检查项**的版本化信息，支持**登记、变更、核查、限期整改、复核销号**全流程留痕，解决群租排查中"同一套房屋被房东/中介/租客重复登记、房间用途变化无历史、隐患整改后无法证明何时解除"的问题。

纯 Python 3.11 标准库实现（`http.server` + `sqlite3`），无第三方依赖。

## 运行

```bash
python3 -m src --host 0.0.0.0 --port 8000 --db data/housing.db   # 启动 HTTP 服务
python3 -m unittest discover -s tests -t .                       # 运行测试（36 个用例）
python3 examples/demo.py                                         # 端到端流程演示
```

数据落盘于 SQLite 文件，**服务重启后已归档记录与未完成整改继续可查**。

## 需求 → 机制

| 需求 | 实现 |
| --- | --- |
| 版本化信息 | 四类实体均带 `version`，每次写入同时落 `revisions` 快照（含字段级 from/to 差异） |
| 证件过期 / 授权缺失 / 地址冲突 | 写入前校验，422 返回 `fields` 字段级原因；地址经全角/空白归一化后查重 |
| 已归档记录不可被新版本覆盖 | 服务层拒绝（409 `ARCHIVED_IMMUTABLE`）+ SQLite 触发器兜底（绕过服务层也写不进） |
| 并行修改返回冲突版本 | `expected_version` 乐观锁，冲突返回 409 + `current_version` + 当前快照 |
| 重复提交幂等 | `Idempotency-Key` 重放返回首次响应；相同责任人材料自动去重；材料不一致提示走变更接口 |
| 片区权限脱敏 | 非本片区查询时手机号/证件号脱敏（`138****5678`），历史快照同步脱敏；管理端全量可见 |
| 管理端视图 | `GET /api/admin/risk-overview` 当前风险、整改期限、逾期天数、责任人；`GET /api/properties/{id}/history` 全量变更时间线 |
| 重启持久化 | SQLite 落盘；销号时间 `closed_at` 即隐患解除证明 |

## API 一览

请求头：`X-Operator-Id` / `X-Operator-Role`（`grid`|`admin`）/ `X-Operator-Zones`（片区，URL 编码，逗号分隔）/ `Idempotency-Key`（POST 幂等键）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/properties` | 登记房屋（地址冲突 422） |
| GET | `/api/properties` `?zone=` | 房屋列表 |
| GET / PUT | `/api/properties/{id}` | 详情（按片区脱敏）/ 变更（需 `expected_version`） |
| POST | `/api/properties/{id}/units` | 登记居住单元 |
| PUT | `/api/units/{id}` | 变更单元（用途变化留痕） |
| POST | `/api/properties/{id}/parties` | 登记责任人（房东/中介/租客） |
| PUT | `/api/parties/{id}` | 变更责任人（证件过期/授权缺失 422） |
| POST | `/api/inspections` | 核查（全部通过直接归档；有隐患进入 OPEN） |
| POST | `/api/inspections/{id}/rectify` | 限期整改（OPEN → RECTIFYING，设 deadline） |
| POST | `/api/inspections/{id}/close` | 复核销号（→ CLOSED 并归档，记录 `closed_at`） |
| GET | `/api/inspections` `?property_id=&status=` | 检查记录查询（含归档） |
| GET | `/api/properties/{id}/history` | 该房屋全部实体变更时间线 |
| GET | `/api/admin/risk-overview` | 管理端风险总览（仅 admin） |

## 错误格式

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "字段校验未通过，写入已拒绝",
    "fields": {"cert_expiry": "证件已于 2020-01-01 过期，禁止登记"}
  }
}
```

错误码：`VALIDATION_FAILED`(422) / `VERSION_CONFLICT`(409，附 `current_version` 与 `current` 快照) / `ARCHIVED_IMMUTABLE`(409) / `IDEMPOTENCY_KEY_REUSED`(409) / `PARTY_MATERIAL_MISMATCH`(409) / `NOT_FOUND`(404) / `PERMISSION_DENIED`(403)。

## 代码结构

```
src/
  service.py   领域服务：状态机、校验、版本化、幂等、脱敏、总览
  db.py        SQLite 模式（含归档不可变触发器）与事务助手
  errors.py    业务异常 → HTTP 状态码/错误码/字段级原因
  masking.py   联系方式脱敏
  server.py    标准库 HTTP 层（路由、请求头解析、错误映射）
tests/         36 个用例：字段拒绝、归档不可变、并发冲突、幂等、脱敏、整改闭环、重启持久化
examples/demo.py  全流程演示
```
