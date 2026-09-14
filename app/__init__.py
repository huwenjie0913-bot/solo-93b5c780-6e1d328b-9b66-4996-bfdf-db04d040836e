"""退役电芯配组均衡评估 API。

应用工厂 + 蓝图注册，数据库路径可通过环境变量
``CELL_GROUP_DB`` 覆盖（测试与生产隔离）。
"""
from __future__ import annotations

import os

from flask import Flask, jsonify

from .db import init_db
from .routes import bp as api_bp


def create_app(database: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database or os.environ.get(
        "CELL_GROUP_DB", os.path.join("data", "cell_group.db")
    )
    app.config["JSON_AS_ASCII"] = False

    app.register_blueprint(api_bp)

    @app.route("/health")
    def health() -> tuple:
        return jsonify({"status": "ok"}), 200

    @app.errorhandler(404)
    def not_found(err):  # noqa: ANN001
        return jsonify({"error": {"code": "NOT_FOUND", "message": "资源不存在"}}), 404

    init_db(app.config["DATABASE"])
    return app
