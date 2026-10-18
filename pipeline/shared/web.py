from pathlib import Path

from fastapi.responses import FileResponse

ASSETS = Path(__file__).resolve().parents[1] / "web"


def mount_ui(app):
    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in ("app.js", "style.css"):
            from .core import DomainError

            raise DomainError("文件不存在", 404)
        return FileResponse(ASSETS / name)
