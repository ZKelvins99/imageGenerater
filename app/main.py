from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.api import config_routes, generate_routes, ws_routes
from app.services.config_service import DATA_DIR, ensure_dirs, load_settings
from app.services.task_service import task_manager

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def create_app() -> FastAPI:
    ensure_dirs()
    app = FastAPI(title="ImageGenerater", version="1.0.0")

    app.include_router(config_routes.router)
    app.include_router(generate_routes.router)
    app.include_router(ws_routes.router)

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        task_manager.set_broadcast(ws_routes.ws_manager.broadcast)

    @app.get("/")
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html")

    @app.get("/media/{path:path}")
    async def media(path: str):
        target = (DATA_DIR / path).resolve()
        if not str(target).startswith(str(DATA_DIR.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(target)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
