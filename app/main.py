from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.api import (
    asset_routes,
    config_routes,
    generate_routes,
    job_routes,
    provider_routes,
    ws_routes,
)
from app.db import connection as db_conn
from app.db import migrate as db_migrate
from app.services import config_service, jsonl_migration, task_service
from app.services.config_service import ensure_dirs, load_settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="ImageGenerater", version="1.0.0")

    app.include_router(config_routes.router)
    app.include_router(generate_routes.router)
    app.include_router(provider_routes.router)
    app.include_router(asset_routes.router)
    app.include_router(job_routes.router)
    app.include_router(ws_routes.router)

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        ensure_dirs()
        await db_migrate.migrate()
        await jsonl_migration.migrate_history_jsonl()
        task_service.task_manager.set_broadcast(ws_routes.ws_manager.broadcast)
        await task_service.task_manager.start_workers()
        await task_service.task_manager.recover_on_startup()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await task_service.task_manager.stop_workers()
        await db_conn.close()

    @app.get("/")
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html")

    @app.get("/media/{path:path}")
    async def media(path: str):
        data_dir = config_service.DATA_DIR
        target = (data_dir / path).resolve()
        if not str(target).startswith(str(data_dir.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            target,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    ensure_dirs()
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
