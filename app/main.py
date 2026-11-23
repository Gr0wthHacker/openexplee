import asyncio
import base64
import binascii
import secrets
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from app.database import init_db
from app.config import settings
from app.api.campaigns import router as campaigns_router
from app.api.leads import router as leads_router
from app.api.accounts import router as inboxes_router
from app.api.settings import router as settings_router, load_persisted_settings
from app.api.stats import router as stats_router
from app.api.cloudflare import router as cloudflare_router
from app.api.mcp import router as mcp_router
from app.api.mailbox import router as mailbox_router
from app.engine.scheduler import run_scheduler_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite schema and tables
    await init_db()
    await load_persisted_settings()
    scheduler_task = asyncio.create_task(run_scheduler_loop(interval_seconds=300))
    print("[OpenExplee] Database, persisted settings, and follow-up scheduler initialized.")
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="OpenExplee",
    description="The Open-Source, Self-Hosted Alternative to Explee",
    version="1.0.0",
    lifespan=lifespan
)

@app.middleware("http")
async def require_operator(request: Request, call_next):
    path = request.url.path
    if (path == "/health" and request.method == "GET") or (
        path == "/api/cloudflare/inbound-webhook" and request.method == "POST"
    ):
        return await call_next(request)

    if len(settings.admin_password.get_secret_value()) < 16:
        return JSONResponse({"detail": "Configure OPENEXPLEE_ADMIN_PASSWORD (at least 16 characters)."}, 503)
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    try:
        credentials = base64.b64decode(encoded, validate=True) if scheme.lower() == "basic" else b""
    except (ValueError, binascii.Error):
        credentials = b""
    expected = b"admin:" + settings.admin_password.get_secret_value().encode()
    if not secrets.compare_digest(credentials, expected):
        return JSONResponse(
            {"detail": "Operator login required."}, 401,
            headers={"WWW-Authenticate": 'Basic realm="OpenExplee", charset="UTF-8"'},
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} or (
            origin and origin not in {f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"}
        ):
            return JSONResponse({"detail": "Cross-origin operator request denied."}, 403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path not in {"/docs", "/redoc"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
    return response


# Register API Routers
app.include_router(campaigns_router)
app.include_router(leads_router)
app.include_router(inboxes_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(cloudflare_router)
app.include_router(mcp_router)
app.include_router(mailbox_router)

# Mount Static Files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"name": "OpenExplee API", "status": "running", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"ok": True, "app": "openexplee", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)

