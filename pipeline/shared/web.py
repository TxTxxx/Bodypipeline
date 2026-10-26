from pathlib import Path
from urllib.parse import urlencode

from fastapi.responses import FileResponse, RedirectResponse

ASSETS = Path(__file__).resolve().parents[1] / "web"
BASE_ASSETS = {"app.js", "style.css", "client-replay.js", "client-inference.js", "client-video.js"}
REPORT_ASSETS = {"report.js", "report.css"}


def mount_ui(app, *, report=False, replay=False):
    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    if report:

        @app.get("/report")
        @app.get("/report/{job_id}")
        def report_page(job_id: str | None = None):
            return FileResponse(ASSETS / "report.html")

    if replay:

        @app.get("/replay")
        def replay_page(capture: str | None = None):
            query = "?" + urlencode({"capture": capture}) if capture else ""
            return RedirectResponse("/#replay" + query, status_code=307)

    @app.get("/assets/{name}")
    def asset(name: str):
        allowed = BASE_ASSETS | (REPORT_ASSETS if report else set())
        if name not in allowed:
            from .core import DomainError

            raise DomainError("文件不存在", 404)
        return FileResponse(ASSETS / name)
