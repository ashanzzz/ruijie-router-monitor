from __future__ import annotations

import hashlib
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, WebSocket
from sqlalchemy import DateTime, Integer, String, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend.config import settings


class ControlBase(DeclarativeBase):
    pass


class AdminCredential(ControlBase):
    __tablename__ = "admin_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(String(512))
    password_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class AdminSession(ControlBase):
    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_version: Mapped[int] = mapped_column(Integer)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)


control_path = (settings.data_dir / "control.db").resolve()
control_engine = create_engine(
    f"sqlite:///{control_path}", connect_args={"check_same_thread": False}
)
ControlSession = sessionmaker(bind=control_engine, autoflush=False, expire_on_commit=False)
password_hasher = PasswordHasher()
COOKIE_NAME = "ruijie_admin_session"
login_failures: dict[str, deque[datetime]] = defaultdict(deque)
login_lock = threading.Lock()


def init_control_db() -> None:
    ControlBase.metadata.create_all(control_engine)


def validate_password(value: str) -> None:
    if len(value) < 10:
        raise HTTPException(422, "管理员密码至少需要10个字符")
    if len(value) > 256:
        raise HTTPException(422, "管理员密码过长")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=7 * 24 * 3600,
        path="/",
    )


def create_admin_session(db: Session, version: int) -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = datetime.utcnow()
    db.add(
        AdminSession(
            token_hash=hash_token(raw_token),
            password_version=version,
            csrf_token=csrf_token,
            created_at=now,
            expires_at=now + timedelta(days=7),
            last_seen_at=now,
        )
    )
    return raw_token, csrf_token


def get_admin() -> AdminCredential | None:
    with ControlSession() as db:
        return db.get(AdminCredential, 1)


def lookup_session(raw_token: str | None) -> AdminSession | None:
    if not raw_token:
        return None
    with ControlSession.begin() as db:
        item = db.get(AdminSession, hash_token(raw_token))
        admin = db.get(AdminCredential, 1)
        now = datetime.utcnow()
        if (
            item is None
            or admin is None
            or item.expires_at <= now
            or item.password_version != admin.password_version
        ):
            if item is not None:
                db.delete(item)
            return None
        item.last_seen_at = now
        db.flush()
        db.expunge(item)
        return item


def auth_status(request: Request) -> dict:
    admin = get_admin()
    current = lookup_session(request.cookies.get(COOKIE_NAME))
    return {
        "setup_required": admin is None,
        "authenticated": current is not None,
        "csrf_token": current.csrf_token if current else None,
    }


def setup_admin(password: str, confirm: str, response: Response) -> dict:
    validate_password(password)
    if password != confirm:
        raise HTTPException(422, "两次密码不一致")
    now = datetime.utcnow()
    with ControlSession.begin() as db:
        if db.get(AdminCredential, 1) is not None:
            raise HTTPException(409, "管理员密码已经设置")
        admin = AdminCredential(
            id=1,
            password_hash=password_hasher.hash(password),
            password_version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(admin)
        raw, csrf = create_admin_session(db, 1)
    set_session_cookie(response, raw)
    return {"status": "success", "csrf_token": csrf}


def _enforce_rate_limit(ip: str) -> None:
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=10)
    with login_lock:
        queue = login_failures[ip]
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= 8:
            raise HTTPException(429, "登录失败次数过多，请稍后再试")


def login_admin(password: str, response: Response, ip: str) -> dict:
    _enforce_rate_limit(ip)
    with ControlSession.begin() as db:
        admin = db.get(AdminCredential, 1)
        if admin is None:
            raise HTTPException(409, "请先设置管理员密码")
        try:
            valid = password_hasher.verify(admin.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            with login_lock:
                login_failures[ip].append(datetime.utcnow())
            raise HTTPException(401, "管理员密码错误")
        if password_hasher.check_needs_rehash(admin.password_hash):
            admin.password_hash = password_hasher.hash(password)
        raw, csrf = create_admin_session(db, admin.password_version)
    with login_lock:
        login_failures.pop(ip, None)
    set_session_cookie(response, raw)
    return {"status": "success", "csrf_token": csrf}


def require_admin(request: Request) -> AdminSession:
    item = lookup_session(request.cookies.get(COOKIE_NAME))
    if item is None:
        raise HTTPException(401, "未登录或登录已失效")
    return item


def require_csrf(request: Request, session: AdminSession = Depends(require_admin)) -> AdminSession:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
            raise HTTPException(403, "CSRF校验失败")
    return session


def logout_admin(request: Request, response: Response) -> dict:
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        with ControlSession.begin() as db:
            item = db.get(AdminSession, hash_token(raw))
            if item is not None:
                db.delete(item)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "success"}


def change_password(current: str, new: str, confirm: str, response: Response) -> dict:
    validate_password(new)
    if new != confirm:
        raise HTTPException(422, "两次新密码不一致")
    with ControlSession.begin() as db:
        admin = db.get(AdminCredential, 1)
        if admin is None:
            raise HTTPException(409, "管理员尚未设置")
        try:
            valid = password_hasher.verify(admin.password_hash, current)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            raise HTTPException(401, "当前管理员密码错误")
        admin.password_hash = password_hasher.hash(new)
        admin.password_version += 1
        admin.updated_at = datetime.utcnow()
        db.execute(delete(AdminSession))
        raw, csrf = create_admin_session(db, admin.password_version)
    set_session_cookie(response, raw)
    return {"status": "success", "csrf_token": csrf}


def reset_admin_password(new_password: str) -> None:
    validate_password(new_password)
    now = datetime.utcnow()
    with ControlSession.begin() as db:
        admin = db.get(AdminCredential, 1)
        if admin is None:
            admin = AdminCredential(
                id=1,
                password_hash=password_hasher.hash(new_password),
                password_version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(admin)
        else:
            admin.password_hash = password_hasher.hash(new_password)
            admin.password_version += 1
            admin.updated_at = now
        db.execute(delete(AdminSession))


def authenticate_websocket(websocket: WebSocket) -> bool:
    return lookup_session(websocket.cookies.get(COOKIE_NAME)) is not None
