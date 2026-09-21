"""python -m src：以 uvicorn 启动服务。"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .app import create_app

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
