"""Flask 应用工厂。"""
from __future__ import annotations

import os
import tempfile

from flask import Flask, g

from . import db


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DB_PATH=os.environ.get(
            "DB_PATH", os.path.join(app.instance_path, "app.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-secret-change-me"),
        MAX_CONTENT_LENGTH=1024 * 1024,  # 上传上限 1 MiB（备份包同样受限）
        # 待确认恢复的备份暂存目录（每个备份一个带令牌的文件，确认后即删）
        RESTORE_STAGING_DIR=os.environ.get(
            "RESTORE_STAGING_DIR",
            os.path.join(tempfile.gettempdir(), "biodiversity_restore")),
    )
    if config:
        app.config.update(config)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["RESTORE_STAGING_DIR"], exist_ok=True)

    db.init_db(app)
    app.teardown_appcontext(db.close_db)

    from . import routes
    app.register_blueprint(routes.bp)

    @app.before_request
    def _open_snapshot():
        # 每个请求打开一个快照读事务并捕获 generation：
        # 读请求全程看到同一个已提交状态；写请求在升级写事务时做围栏复查。
        conn = db.get_db()
        g.snapshot_generation = db.begin_snapshot(conn)

    return app
