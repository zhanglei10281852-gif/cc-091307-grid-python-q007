"""服务层异常：统一错误码、HTTP 状态码与字段级原因。"""


class ServiceError(Exception):
    """所有业务异常的基类，to_dict() 即为 API 错误响应体。"""

    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


class ValidationFailed(ServiceError):
    """字段校验未通过（证件过期 / 授权缺失 / 地址冲突等），拒绝写入。

    fields 形如 {"cert_expiry": "证件已于 2026-01-01 过期，禁止登记"}，
    客户端可直接按字段定位原因。
    """

    status = 422
    code = "VALIDATION_FAILED"

    def __init__(self, fields, message="字段校验未通过，写入已拒绝"):
        super().__init__(message, fields=fields)
        self.fields = fields


class VersionConflict(ServiceError):
    """乐观锁冲突：并行修改时返回当前版本号与当前快照，供调用方合并重试。"""

    status = 409
    code = "VERSION_CONFLICT"

    def __init__(self, entity_label, current):
        self.current = current
        super().__init__(
            f"{entity_label}已被其他网格员修改，请刷新后基于最新版本重试",
            current_version=current.get("version"),
            current=current,
        )


class ArchivedImmutable(ServiceError):
    """已归档的检查记录不可被新版本覆盖。"""

    status = 409
    code = "ARCHIVED_IMMUTABLE"

    def __init__(self, inspection_id):
        super().__init__(
            f"检查记录 {inspection_id} 已归档，归档记录不可被新版本覆盖",
            inspection_id=inspection_id,
        )


class NotFound(ServiceError):
    status = 404
    code = "NOT_FOUND"

    def __init__(self, entity_label, entity_id):
        super().__init__(f"{entity_label}不存在: {entity_id}", entity_id=entity_id)


class PermissionDenied(ServiceError):
    status = 403
    code = "PERMISSION_DENIED"


class IdempotencyKeyReused(ServiceError):
    """同一幂等键被用于不同的请求内容。"""

    status = 409
    code = "IDEMPOTENCY_KEY_REUSED"

    def __init__(self, key):
        super().__init__(
            f"幂等键 {key} 已用于其他请求内容，请更换幂等键",
            idempotency_key=key,
        )


class PartyMaterialMismatch(ServiceError):
    """相同证件的责任人已登记但本次材料不一致，应走变更接口。"""

    status = 409
    code = "PARTY_MATERIAL_MISMATCH"

    def __init__(self, party_id):
        super().__init__(
            "相同证件号码的责任人已登记但材料不一致，请使用变更接口并携带 expected_version",
            party_id=party_id,
        )
