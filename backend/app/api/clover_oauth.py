from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.integrations.clover.dependencies import get_oauth_service
from app.integrations.clover.oauth_service import (
    AUTHORIZATION_WINDOW_SECONDS,
    OAuthCallbackError,
    OAuthService,
)
from app.integrations.clover.token_store import TokenStoreError

router = APIRouter(prefix="/clover", tags=["clover-oauth"])

BINDING_COOKIE = "__Host-clover-oauth"


@router.get("/launch")
async def launch(request: Request, service: OAuthService = Depends(get_oauth_service)) -> Response:
    if "code" in request.query_params:
        raise HTTPException(400, "Start a new Clover authorization")
    try:
        authorization = await service.launch()
    except TokenStoreError:
        raise HTTPException(503, "Clover authorization storage is unavailable") from None
    response = RedirectResponse(authorization.url, status_code=302)
    response.set_cookie(
        BINDING_COOKIE,
        authorization.browser_nonce,
        max_age=AUTHORIZATION_WINDOW_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/callback")
async def callback(request: Request, service: OAuthService = Depends(get_oauth_service)) -> Response:
    params = request.query_params
    fields = ("state", "code", "merchant_id", "client_id", "error")
    try:
        if any(len(params.getlist(name)) > 1 for name in fields):
            raise OAuthCallbackError(400, "Invalid Clover authorization response")
        await service.complete_authorization(
            state=params.get("state"),
            browser_nonce=request.cookies.get(BINDING_COOKIE),
            code=params.get("code"),
            merchant_id=params.get("merchant_id"),
            client_id=params.get("client_id"),
            error=params.get("error"),
        )
        response = RedirectResponse(service.config.completion_url, status_code=303)
    except OAuthCallbackError as exc:
        response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except TokenStoreError:
        response = JSONResponse({"detail": "Clover authorization storage is unavailable"}, status_code=503)
    response.delete_cookie(BINDING_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.get("/complete", response_class=HTMLResponse)
async def complete(service: OAuthService = Depends(get_oauth_service)) -> HTMLResponse:
    # Static page is not an admin session or proof of authentication.
    return HTMLResponse(
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<title>Clover connection</title><h1>Clover authorization finished</h1>"
        "<p>You may close this window. This does not sign you into an admin account.</p></html>"
    )

