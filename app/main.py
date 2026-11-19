"""pcc 服务端入口。启动：cd server && python3 -m app.main"""
from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import admin, api
from .config import load_config
from .db import make_pool


def create_app() -> FastAPI:
    cfg = load_config()
    docs_on = bool(cfg["server"].get("docs_enabled", False))
    app = FastAPI(
        title="pcc server",
        version="1.0",
        docs_url="/docs" if docs_on else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg["auth"]["secret_key"],
        https_only=bool(cfg["server"].get("session_secure", False)),
        same_site="lax",
    )
    app.state.config = cfg
    app.state.pool = make_pool(cfg)
    app.include_router(api.router)
    app.include_router(admin.router)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg["server"]["host"], port=int(cfg["server"]["port"]))
