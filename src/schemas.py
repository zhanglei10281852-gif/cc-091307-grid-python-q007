"""请求数据结构定义（pydantic）。

业务规则校验（证件过期、授权缺失、地址冲突、版本冲突等）在 domain 层完成，
这里只负责请求形状与基本格式。
"""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["owner", "agent", "tenant"]
Severity = Literal["low", "medium", "high"]
InspectResult = Literal["pass", "fail"]


class ResponsibleIn(BaseModel):
    """房屋责任人挂靠：中介(agent)必须携带房东授权材料。"""

    person_id: str
    role: Role
    authorization_doc: Optional[str] = None
    authorization_expires_on: Optional[dt.date] = None


class UnitIn(BaseModel):
    room_label: str = Field(min_length=1, max_length=50)
    purpose: str = Field(min_length=1, max_length=50)
    capacity: int = Field(default=1, ge=0, le=100)


class HouseRegisterIn(BaseModel):
    address: str = Field(min_length=4, max_length=200)
    zone: str = Field(min_length=1, max_length=50)
    responsibles: list[ResponsibleIn] = Field(min_length=1)
    units: list[UnitIn] = []
    change_note: Optional[str] = None


class HouseChangeIn(BaseModel):
    expected_version: int = Field(ge=1)
    address: Optional[str] = Field(default=None, min_length=4, max_length=200)
    zone: Optional[str] = Field(default=None, min_length=1, max_length=50)
    responsibles: Optional[list[ResponsibleIn]] = None
    change_note: Optional[str] = None


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=5, max_length=20)
    id_number: str = Field(min_length=6, max_length=30)
    id_expires_on: dt.date


class PersonChangeIn(BaseModel):
    expected_version: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    phone: Optional[str] = Field(default=None, min_length=5, max_length=20)
    id_number: Optional[str] = Field(default=None, min_length=6, max_length=30)
    id_expires_on: Optional[dt.date] = None
    change_note: Optional[str] = None


class UnitChangeIn(BaseModel):
    expected_version: int = Field(ge=1)
    room_label: Optional[str] = Field(default=None, min_length=1, max_length=50)
    purpose: Optional[str] = Field(default=None, min_length=1, max_length=50)
    capacity: Optional[int] = Field(default=None, ge=0, le=100)
    change_note: Optional[str] = None


class CheckItemIn(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=50)
    severity: Severity


class CheckItemChangeIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    category: Optional[str] = Field(default=None, min_length=1, max_length=50)
    severity: Optional[Severity] = None
    change_note: Optional[str] = None


class InspectionResultIn(BaseModel):
    item_id: str
    result: InspectResult
    note: Optional[str] = None


class InspectionIn(BaseModel):
    house_id: str
    unit_id: Optional[str] = None
    results: list[InspectionResultIn] = Field(min_length=1)


class InspectionCorrectIn(BaseModel):
    expected_version: int = Field(ge=1)
    results: list[InspectionResultIn] = Field(min_length=1)


class RectificationIn(BaseModel):
    inspection_id: str
    item_id: str
    hazard: str = Field(min_length=2, max_length=500)
    deadline: dt.date


class ReviewIn(BaseModel):
    passed: bool
    note: Optional[str] = None
    new_deadline: Optional[dt.date] = None
