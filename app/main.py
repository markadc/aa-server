"""pcc 服务端入口。启动：cd aa-server && uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8800"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from . import admin, api
from .config import load_config
from .db import ensure_schema, make_pool


def create_app() -> FastAPI:
    cfg = load_config()
    docs_on = bool(cfg["server"].get("docs_enabled", False))
    app = FastAPI(
        title="pcc server",
        version="2.0",
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
    ensure_schema(app.state.pool)
    app.include_router(api.router)
    app.include_router(api.secure)
    app.include_router(admin.router)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        """API 的错误体直接展开，方便 APK 解析：{ok:false, error, error_code}。"""
        if isinstance(exc.detail, dict):
            body = dict(exc.detail)
            body.setdefault("ok", False)
        else:
            body = {"ok": False, "error": str(exc.detail)}
        headers = getattr(exc, "headers", None)
        response = JSONResponse(status_code=exc.status_code, content=body)
        if headers:
            for k, v in headers.items():
                response.headers[k] = v
        return response

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg["server"]["host"], port=int(cfg["server"]["port"]))
