"""OAuth callback routes: redirect to Twitch authorization or complete it."""

import html
import logging
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from src.application.oauth import OAuthService
from src.domain.exceptions import OAuthFlowError
from src.interfaces.dependencies import get_oauth_service

logger = logging.getLogger(__name__)

OAuthServiceDep = Annotated[OAuthService, Depends(get_oauth_service)]

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Twitch auth</title></head>
<body style="font-family: sans-serif; margin: 3rem">
<h1>{title}</h1><p>{body}</p>
</body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """Render a page, escaping any user-controlled values."""
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), body=html.escape(body)),
        status_code=status,
    )


def make_router(redirect_uri: str) -> APIRouter:
    """Build the OAuth router, serving the redirect URI's path.

    A GET without a code/error redirects to Twitch authorization; the browser
    callback arrives back with ?code=...&state=... (or ?error=...).
    """
    router = APIRouter()
    path = urlparse(redirect_uri).path or "/"

    @router.get(path, response_class=HTMLResponse)
    async def oauth_endpoint(request: Request, service: OAuthServiceDep) -> Response:
        if "code" not in request.query_params and "error" not in request.query_params:
            return RedirectResponse(service.authorize_url(), status_code=302)
        if "error" in request.query_params:
            return _page(
                "Authorization failed",
                f"Twitch returned: {request.query_params['error']}",
                status=400,
            )
        code = request.query_params.get("code") or ""
        state = request.query_params.get("state") or ""
        try:
            user = await service.complete_authorization(code, state)
        except OAuthFlowError as exc:
            return _page("Authorization failed", str(exc), status=400)
        except Exception:  # BLE001: OAuth/DB failure surfaced as a generic page
            logger.exception("oauth callback failed")
            return _page(
                "Authorization failed",
                "Something went wrong — check the server log.",
                status=400,
            )
        return _page(
            "Authorized",
            f"{user.display_name} connected — token stored. "
            "The consumer will pick up the channel within the resync interval.",
        )

    return router
