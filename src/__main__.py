"""python -m src 启动房屋安全登记 HTTP 服务。"""
import argparse

from .server import run


def main():
    parser = argparse.ArgumentParser(description="社区房屋租赁安全登记服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None,
                        help="SQLite 数据文件路径（默认取环境变量 HOUSING_DB_PATH，否则 data/housing.db）")
    args = parser.parse_args()
    run(host=args.host, port=args.port, db_path=args.db)


if __name__ == "__main__":
    main()
