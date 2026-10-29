"""管理員登入／登出。Session token 只放在 HttpOnly + SameSite=Strict cookie，前端 JS 讀不到。"""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.auth import AdminAuth, LoginLockedError

COOKIE_NAME = "farmgm_admin"
ADMIN_HEADER = "x-farmgm-admin"


def require_csrf_header(request: Request) -> None:
    """寫入類請求必須帶自訂標頭（跨站表單無法送出），且 Origin（若有）須與 Host 相同。"""
    if request.headers.get(ADMIN_HEADER) != "1":
        raise HTTPException(status_code=403, detail="缺少管理請求標頭。")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
        raise HTTPException(status_code=403, detail="不允許跨來源的管理請求。")


def _auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth


def require_admin_session(request: Request) -> str:
    auth = _auth(request)
    if not auth.enabled:
        raise HTTPException(status_code=503, detail="尚未設定管理員帳號，群組管理功能停用。")
    username = auth.validate(request.cookies.get(COOKIE_NAME))
    if username is None:
        raise HTTPException(status_code=401, detail="請先登入管理員帳號。")
    return username


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login", dependencies=[Depends(require_csrf_header)])
async def login(request: Request, response: Response, body: LoginBody) -> dict:
    auth = _auth(request)
    if not auth.enabled:
        raise HTTPException(status_code=503, detail="尚未設定管理員帳號，群組管理功能停用。")
    client = request.client.host if request.client else "unknown"
    try:
        # scrypt 需要數十毫秒，放到執行緒池避免阻塞其他請求
        token = await run_in_threadpool(auth.login, body.username, body.password, client)
    except LoginLockedError as exc:
        raise HTTPException(
            status_code=429,
            detail=f"登入失敗次數過多，請 {exc.retry_after} 秒後再試。",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    if token is None:
        # 不區分帳號或密碼錯誤
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤。")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(auth.session_seconds),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/api",
    )
    return {"username": auth.username}


@router.post("/logout", dependencies=[Depends(require_csrf_header)])
async def logout(request: Request, response: Response) -> dict:
    _auth(request).logout(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, samesite="strict")
    return {"logged_in": False}


@router.get("/me")
async def me(request: Request) -> dict:
    auth = _auth(request)
    username = auth.validate(request.cookies.get(COOKIE_NAME)) if auth.enabled else None
    return {"enabled": auth.enabled, "logged_in": username is not None, "username": username}
