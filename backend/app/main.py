import logging
import hmac
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import clover_oauth, dev_clover, health, menu, orders, table_sessions
from app.admin import api as admin_api
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.public import api as public_api
from app.table_sessions.service import OrderConfirmationPending

configure_logging()
settings = get_settings()
app = FastAPI(title=settings.app_name, redirect_slashes=False)


@app.exception_handler(OrderConfirmationPending)
async def confirmation_pending_response(
    request: Request, exc: OrderConfirmationPending,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Order sent; confirmation pending", "code": "ORDER_CONFIRMATION_PENDING"},
        headers={"Cache-Control": "no-store"},
    )
def configure_frontend_cors(application: FastAPI, configuration: Settings) -> None:
    if configuration.allowed_frontend_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(configuration.allowed_frontend_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Idempotency-Key", "ngrok-skip-browser-warning"],
            allow_credentials=True,
        )


configure_frontend_cors(app, settings)
logger = logging.getLogger("app.requests")


@app.middleware("http")
async def add_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.scope["path"]
    internal_route = (path == "/api/orders" or path == "/api/table-sessions"
                      or path.startswith("/api/table-sessions/"))
    expected = settings.internal_api_key.get_secret_value() if settings.internal_api_key else ""
    if settings.allowed_frontend_origins and internal_route and (
        not expected or not hmac.compare_digest(
            request.headers.get("X-Internal-API-Key", ""), expected,
        )
    ):
        response = JSONResponse({"detail": "Internal API is unavailable"}, status_code=403)
    else:
        try:
            response = await call_next(request)
        except Exception:
            # Do not let unexpected provider/storage exceptions reach access/error
            # loggers with request context. Operational log below records the 500.
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.scope["path"].startswith(("/clover/", "/api/dev/", "/api/admin/")):
        response.headers["Cache-Control"] = "no-store"
        if request.scope["path"] == "/clover/callback" and response.status_code >= 400:
            response.delete_cookie(
                clover_oauth.BINDING_COOKIE, secure=True, httponly=True, samesite="lax", path="/"
            )
    route = request.scope.get("route")
    logger.info(
        "HTTP %s %s %d",
        request.method,
        getattr(route, "path", "<unmatched>"),
        response.status_code,
    )
    return response


app.include_router(health.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(table_sessions.router)
app.include_router(public_api.router)
app.include_router(dev_clover.router)
app.include_router(clover_oauth.router)
app.include_router(admin_api.router)

