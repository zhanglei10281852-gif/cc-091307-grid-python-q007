# 社区房屋租赁安全登记

面向网格员与社区管理端的房屋安全登记服务：维护房屋、居住单元、责任人、检查项的
**版本化** 信息，支持登记、变更、核查、限期整改、复核销号全流程，所有变更留痕、
归档记录不可覆盖、重启后数据不丢。

运行环境：Python 3.11。代码位于 `src` 目录，数据默认写入 `./data/service.db`
（可用环境变量 `SERVICE_DB_PATH` 覆盖）。

## 启动与测试

```bash
pip install fastapi uvicorn pytest httpx
python3 -m src                 # 启动服务，默认 127.0.0.1:8000（PORT 可改）
python3 -m pytest tests/ -q    # 运行测试（16 个用例）
```

所有请求需带 `X-Operator-Id` 头（预置：`op-admin` 管理端；`op-grid-1`/`op-grid-2`
网格员，分别负责片区 `Z-01`/`Z-02`）。写接口支持 `Idempotency-Key` 请求头。

## 核心流程

1. **登记**：`POST /persons` 登记责任人 → `POST /houses` 登记房屋（可内联居住单元，
   也可事后 `POST /houses/{id}/units`）→ 管理端 `POST /check-items` 维护检查标准。
2. **变更**：`PUT /houses/{id}`、`PUT /units/{id}`、`PUT /persons/{id}`、
   `PUT /check-items/{id}`，必须携带 `expected_version`；历史经
   `GET /.../history` 查询（含房间用途变化）。
3. **核查**：`POST /inspections` 录入核查结果（检查项版本当场定版）；归档前可
   `PUT /inspections/{id}` 更正；`POST /inspections/{id}/archive` 归档。
4. **限期整改**：对不合格项 `POST /rectifications`（隐患描述 + 整改期限）。
5. **复核销号**：`POST /rectifications/{id}/review`；通过则记录 `closed_at`/
   `closed_by` 销号，不通过可顺延期限。全程事件（下发/驳回/顺延/销号）留痕，
   隐患何时解除有据可查。

## 关键规则

- **拒绝写入并说明字段原因**：证件过期（`id_expires_on`）、中介缺少房东授权或授权
  过期（`authorization_doc`/`authorization_expires_on`）、同一地址重复登记
  （`address`，返回冲突房屋编号）均返回 422：
  `{"error": {"code": "validation_failed", "fields": [{"field": ..., "reason": ...}]}}`。
- **归档不可覆盖**：已归档检查记录拒绝任何修改（409 `inspection_archived`）；
  检查项升级只产生新版本，归档记录仍引用当时的版本原文。
- **并发冲突**：变更基于乐观锁，版本不一致返回 409 `version_conflict` 并携带
  `current_version`，多个网格员并行修改时后到者据此前置刷新。
- **幂等**：同一 `Idempotency-Key` + 相同材料重放返回首次结果、不产生重复写入；
  相同 Key 提交不同材料返回 409 `idempotency_conflict`。
- **片区脱敏**：查询时非本片区网格员的联系方式（电话、证件号）打码
  （如 `138****1111`），管理端与本片区网格员可见明文。
- **管理端**：`GET /admin/dashboard` 汇总当前风险（未销号整改单、期限、逾期标记、
  责任人）与全部实体的历史变更流；`GET /rectifications?status=open` 查未完成整改。
- **持久化**：全部状态存于 SQLite，服务重启后归档记录与未完成整改继续可查。

## 目录结构

```
src/
  schemas.py   请求结构（pydantic）
  store.py     SQLite 表结构与读写（当前表 + 版本表）
  domain.py    版本化写入、字段级校验、幂等、冲突、脱敏
  app.py       FastAPI 路由
  service.py   服务入口（Service / create_app）
tests/
  test_service.py  端到端用例（含并发、幂等、脱敏、重启持久化）
```
