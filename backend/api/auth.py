from datetime import datetime, timedelta
import hashlib
import secrets
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from auth.database import ControlSessionLocal, AdminCredential, AdminSession
from auth.passwords import hash_password, verify_password, validate_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

def utc_now():
    return datetime.utcnow()

def create_session(db, password_version: int) -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    db.add(AdminSession(
        token_hash=token_hash,
        password_version=password_version,
        csrf_token=csrf_token,
        created_at=utc_now(),
        last_seen_at=utc_now(),
        expires_at=utc_now() + timedelta(days=7),
    ))
    return raw_token, csrf_token

def set_session_cookie(response: Response, raw_token: str):
    response.set_cookie(
        key="ruijie_admin_session",
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=False, # HTTP allowed for local network
        max_age=7 * 24 * 3600,
        path="/",
    )

class SetupAdminRequest(BaseModel):
    password: str
    confirm_password: str

class LoginRequest(BaseModel):
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_new_password: str

@router.get("/status")
def get_auth_status(request: Request):
    with ControlSessionLocal() as db:
        admin = db.get(AdminCredential, 1)
        if admin is None:
            return {"setup_required": True, "authenticated": False}
            
        raw_token = request.cookies.get("ruijie_admin_session")
        if not raw_token:
            return {"setup_required": False, "authenticated": False}
            
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        session = db.get(AdminSession, token_hash)
        
        if (session is None or session.expires_at <= utc_now() 
            or session.password_version != admin.password_version):
            return {"setup_required": False, "authenticated": False}
            
        return {"setup_required": False, "authenticated": True, "csrf_token": session.csrf_token}

@router.post("/setup")
def setup_admin(req: SetupAdminRequest, response: Response):
    with ControlSessionLocal.begin() as db:
        if db.get(AdminCredential, 1) is not None:
            raise HTTPException(409, "管理员已经设置")

        if req.password != req.confirm_password:
            raise HTTPException(422, "两次密码不一致")

        try:
            validate_password(req.password)
        except ValueError as e:
            raise HTTPException(422, str(e))

        admin = AdminCredential(
            id=1,
            password_hash=hash_password(req.password),
            password_version=1,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(admin)
        raw_token, csrf_token = create_session(db, 1)

    set_session_cookie(response, raw_token)
    return {
        "status": "success",
        "csrf_token": csrf_token,
    }

@router.post("/login")
def login(req: LoginRequest, response: Response):
    with ControlSessionLocal.begin() as db:
        admin = db.get(AdminCredential, 1)
        if admin is None:
            raise HTTPException(409, "请先设置管理员密码")

        if not verify_password(admin.password_hash, req.password):
            raise HTTPException(401, "管理员密码错误")

        raw_token, csrf_token = create_session(
            db,
            admin.password_version,
        )

    set_session_cookie(response, raw_token)
    return {
        "status": "success",
        "csrf_token": csrf_token,
    }

@router.post("/logout")
def logout(request: Request, response: Response):
    raw_token = request.cookies.get("ruijie_admin_session")
    if raw_token:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with ControlSessionLocal.begin() as db:
            session = db.get(AdminSession, token_hash)
            if session:
                db.delete(session)
    response.delete_cookie("ruijie_admin_session", path="/")
    return {"status": "success"}

@router.post("/change-password")
def change_password(req: ChangePasswordRequest, request: Request, response: Response):
    raw_token = request.cookies.get("ruijie_admin_session")
    if not raw_token:
        raise HTTPException(401, "未登录")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    
    with ControlSessionLocal.begin() as db:
        session = db.get(AdminSession, token_hash)
        admin = db.get(AdminCredential, 1)
        if not session or not admin or session.password_version != admin.password_version:
            raise HTTPException(401, "会话无效")
            
        if not verify_password(admin.password_hash, req.current_password):
            raise HTTPException(401, "当前密码错误")
            
        if req.new_password != req.confirm_new_password:
            raise HTTPException(422, "两次新密码不一致")
            
        admin.password_hash = hash_password(req.new_password)
        admin.password_version += 1
        admin.updated_at = utc_now()
        
        # Invalidate all old sessions
        db.query(AdminSession).delete()
        
        # Create new session
        new_raw_token, csrf_token = create_session(db, admin.password_version)
        
    set_session_cookie(response, new_raw_token)
    return {"status": "success", "csrf_token": csrf_token}
