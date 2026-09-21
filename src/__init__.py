"""社区房屋租赁安全登记领域包。"""
from .app import create_app
from .service import Service

__all__ = ["Service", "create_app"]
