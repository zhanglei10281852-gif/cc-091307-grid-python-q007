"""FastAPI 路由层。

- 所有请求通过 X-Operator-Id 识别操作员（网格员/管理端）；
- 写接口支持 Idempotency-Key 请求头，重复提交同一材料返回首次结果；
- 业务错误统一返回 {"error": {code, message, fields?, ...}}。
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse

from .domain import Domain, DomainError
from .schemas import (
    CheckItemChangeIn,
    CheckItemIn,
    HouseChangeIn,
    HouseRegisterIn,
    InspectionCorrectIn,
    InspectionIn,
    PersonChangeIn,
    PersonIn,
    RectificationIn,
    ReviewIn,
    UnitChangeIn,
    UnitIn,
)
from .store import Store

DEFAULT_DB_PATH = "./data/service.db"


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("SERVICE_DB_PATH", DEFAULT_DB_PATH)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    store = Store(path)
    domain = Domain(store)

    app = FastAPI(title="社区房屋租赁安全登记服务", version="1.0.0")
    app.state.store = store
    app.state.domain = domain

    @app.exception_handler(DomainError)
    async def handle_domain_error(request, exc: DomainError):
        error = {"code": exc.code, "message": exc.message}
        if exc.fields:
            error["fields"] = exc.fields
        error.update(exc.extra)
        return JSONResponse(status_code=exc.status, content={"error": error})

    def current_operator(x_operator_id: str = Header(default="")) -> dict:
        op = domain.get_operator(x_operator_id)
        if op is None:
            raise DomainError("unknown_operator", "缺少或未知的 X-Operator-Id", status=401)
        return op

    def require_admin(op: dict = Depends(current_operator)) -> dict:
        if op["role"] != "admin":
            raise DomainError("forbidden", "仅管理端可执行该操作", status=403)
        return op

    IdemKey = Header(default=None, alias="Idempotency-Key")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---------------- 责任人 ----------------

    @app.post("/persons")
    def register_person(payload: PersonIn, op: dict = Depends(current_operator),
                        idempotency_key: str | None = IdemKey):
        return domain.register_person(op, payload.model_dump(mode="json"), idempotency_key)

    @app.put("/persons/{person_id}")
    def change_person(person_id: str, payload: PersonChangeIn,
                      op: dict = Depends(current_operator),
                      idempotency_key: str | None = IdemKey):
        return domain.change_person(op, person_id, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/persons/{person_id}")
    def get_person(person_id: str, op: dict = Depends(current_operator)):
        return domain.person_view(person_id, op)

    @app.get("/persons/{person_id}/history")
    def get_person_history(person_id: str, op: dict = Depends(current_operator)):
        return domain.person_history(person_id, op)

    # ---------------- 房屋与居住单元 ----------------

    @app.post("/houses")
    def register_house(payload: HouseRegisterIn, op: dict = Depends(current_operator),
                       idempotency_key: str | None = IdemKey):
        return domain.register_house(op, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/houses")
    def list_houses(op: dict = Depends(current_operator), zone: str | None = None):
        return domain.list_houses(op, zone=zone)

    @app.get("/houses/{house_id}")
    def get_house(house_id: str, op: dict = Depends(current_operator)):
        return domain.house_view(house_id, op)

    @app.put("/houses/{house_id}")
    def change_house(house_id: str, payload: HouseChangeIn,
                     op: dict = Depends(current_operator),
                     idempotency_key: str | None = IdemKey):
        return domain.change_house(op, house_id, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/houses/{house_id}/history")
    def get_house_history(house_id: str, op: dict = Depends(current_operator)):
        return domain.house_history(house_id, op)

    @app.post("/houses/{house_id}/units")
    def add_unit(house_id: str, payload: UnitIn, op: dict = Depends(current_operator),
                 idempotency_key: str | None = IdemKey):
        return domain.add_unit(op, house_id, payload.model_dump(mode="json"), idempotency_key)

    @app.put("/units/{unit_id}")
    def change_unit(unit_id: str, payload: UnitChangeIn,
                    op: dict = Depends(current_operator),
                    idempotency_key: str | None = IdemKey):
        return domain.change_unit(op, unit_id, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/units/{unit_id}/history")
    def get_unit_history(unit_id: str, op: dict = Depends(current_operator)):
        return domain.unit_history(unit_id, op)

    # ---------------- 检查项（管理端维护） ----------------

    @app.post("/check-items")
    def register_check_item(payload: CheckItemIn, op: dict = Depends(require_admin),
                            idempotency_key: str | None = IdemKey):
        return domain.register_check_item(op, payload.model_dump(mode="json"), idempotency_key)

    @app.put("/check-items/{item_id}")
    def change_check_item(item_id: str, payload: CheckItemChangeIn,
                          op: dict = Depends(require_admin),
                          idempotency_key: str | None = IdemKey):
        return domain.change_check_item(op, item_id, payload.model_dump(mode="json"),
                                        idempotency_key)

    @app.get("/check-items")
    def list_check_items(op: dict = Depends(current_operator)):
        return domain.list_check_items()

    @app.get("/check-items/{item_id}/history")
    def get_check_item_history(item_id: str, op: dict = Depends(current_operator)):
        return domain.check_item_history(item_id)

    # ---------------- 核查 ----------------

    @app.post("/inspections")
    def create_inspection(payload: InspectionIn, op: dict = Depends(current_operator),
                          idempotency_key: str | None = IdemKey):
        return domain.create_inspection(op, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/inspections/{inspection_id}")
    def get_inspection(inspection_id: str, op: dict = Depends(current_operator)):
        return domain.inspection_view(inspection_id, op)

    @app.put("/inspections/{inspection_id}")
    def correct_inspection(inspection_id: str, payload: InspectionCorrectIn,
                           op: dict = Depends(current_operator),
                           idempotency_key: str | None = IdemKey):
        return domain.correct_inspection(op, inspection_id, payload.model_dump(mode="json"),
                                         idempotency_key)

    @app.post("/inspections/{inspection_id}/archive")
    def archive_inspection(inspection_id: str, op: dict = Depends(current_operator),
                           idempotency_key: str | None = IdemKey):
        return domain.archive_inspection(op, inspection_id, idempotency_key)

    # ---------------- 限期整改与复核销号 ----------------

    @app.post("/rectifications")
    def issue_rectification(payload: RectificationIn, op: dict = Depends(current_operator),
                            idempotency_key: str | None = IdemKey):
        return domain.issue_rectification(op, payload.model_dump(mode="json"), idempotency_key)

    @app.get("/rectifications")
    def list_rectifications(op: dict = Depends(current_operator), status: str | None = None,
                            zone: str | None = None, house_id: str | None = None):
        return domain.list_rectifications(op, status=status, zone=zone, house_id=house_id)

    @app.get("/rectifications/{rect_id}")
    def get_rectification(rect_id: str, op: dict = Depends(current_operator)):
        return domain.rectification_view(rect_id, op)

    @app.post("/rectifications/{rect_id}/review")
    def review_rectification(rect_id: str, payload: ReviewIn,
                             op: dict = Depends(current_operator),
                             idempotency_key: str | None = IdemKey):
        return domain.review_rectification(op, rect_id, payload.model_dump(mode="json"),
                                           idempotency_key)

    # ---------------- 管理端 ----------------

    @app.get("/admin/dashboard")
    def admin_dashboard(op: dict = Depends(require_admin)):
        return domain.admin_dashboard()

    return app
