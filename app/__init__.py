"""Flask 应用工厂。"""
from __future__ import annotations

import os

from flask import Flask


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DB_PATH=os.environ.get(
            "DB_PATH", os.path.join(app.instance_path, "app.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-secret-change-me"),
        MAX_CONTENT_LENGTH=1024 * 1024,  # 上传上限 1 MiB
    )
    if config:
        app.config.update(config)

    os.makedirs(app.instance_path, exist_ok=True)

    from . import db
    db.init_db(app)
    app.teardown_appcontext(db.close_db)

    from . import routes
    app.register_blueprint(routes.bp)

    return app
