"""Same-origin exchange of a recent Identity Platform login for a browser session."""

from fastapi import APIRouter, Request, Response
from metis_contracts import ErrorCode, MetisError

from .. import browser_session
from ..auth import principal_from_header

router = APIRouter(prefix="/v1/session", tags=["authentication"])


def _response() -> Response:
    if not browser_session.enabled():
        raise MetisError(ErrorCode.NOT_FOUND, "browser sessions are unavailable")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("")
def current(request: Request) -> Response:
    response = _response()
    if request.headers.get("origin"):
        browser_session.require_browser_origin(request)
    token = request.cookies.get(browser_session.COOKIE, "")
    browser_session.lookup(token)
    response.headers[browser_session.BINDING_HEADER] = browser_session.binding(token)
    return response


@router.post("")
def sign_in(request: Request) -> Response:
    response = _response()
    browser_session.require_browser_origin(request)
    principal = principal_from_header(request.headers.get("authorization"))
    token = browser_session.start(principal, request.cookies.get(browser_session.COOKIE))
    response.set_cookie(
        browser_session.COOKIE,
        token,
        max_age=browser_session.SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.headers[browser_session.BINDING_HEADER] = browser_session.binding(token)
    return response


@router.delete("")
def sign_out(request: Request) -> Response:
    response = _response()
    browser_session.require_browser_origin(request)
    token = request.cookies.get(browser_session.COOKIE)
    if token:
        browser_session.principal_for_request(request, None)
        browser_session.revoke(token)
    response.delete_cookie(browser_session.COOKIE, secure=True, httponly=True, samesite="lax")
    return response
