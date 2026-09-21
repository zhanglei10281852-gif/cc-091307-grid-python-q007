"""社区房屋租赁安全登记服务入口。"""
from __future__ import annotations

from .app import create_app
from .domain import Domain
from .store import Store


class Service:
    """领域服务的基础入口：组合持久层与领域逻辑。"""

    def __init__(self, db_path: str = ":memory:"):
        self.store = Store(db_path)
        self.domain = Domain(self.store)
        self.ready = True

    def close(self) -> None:
        self.store.close()
        self.ready = False


__all__ = ["Service", "create_app"]
