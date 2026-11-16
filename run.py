"""开发入口；容器内由 gunicorn 以 `run:app` 加载。"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
