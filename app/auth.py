"""Authentication: password hashing, JWT issuance, and FastAPI dependencies."""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
import secrets
import threading
import time
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

from . import config, database

# PBKDF2-HMAC-SHA256 轮数。前端会用同样的参数算出 verifier，两端必须一致。
PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGO = "pbkdf2_sha256"


# --------------------------------------------------------------------------- #
# Password hashing (stdlib pbkdf2-hmac-sha256, no external deps)
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGO}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != PBKDF2_ALGO:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return secrets.compare_digest(dk.hex(), hash_hex)


def salt_of(stored: str) -> str | None:
    parts = (stored or "").split("$", 2)
    return parts[1] if len(parts) == 3 and parts[0] == PBKDF2_ALGO else None


def verifier_of(stored: str) -> str | None:
    """取出存储值里的派生密钥（= 前端要算出的 verifier）。"""
    parts = (stored or "").split("$", 2)
    return parts[2] if len(parts) == 3 and parts[0] == PBKDF2_ALGO else None


def make_stored(salt_hex: str, verifier_hex: str) -> str:
    return f"{PBKDF2_ALGO}${salt_hex}${verifier_hex}"


# --------------------------------------------------------------------------- #
# 挑战-应答（密码明文不出浏览器，且传输值一次性、不可重放）
# --------------------------------------------------------------------------- #
_NONCE_TTL = 120          # 秒
_NONCE_MAX = 4096         # 待用挑战上限，防内存膨胀
_nonces: dict[str, dict] = {}
_nonce_lock = threading.Lock()


def _prune_nonces(now: float) -> None:
    for k in [k for k, v in _nonces.items() if v["exp"] < now]:
        _nonces.pop(k, None)


def fake_salt(username: str) -> str:
    """用户不存在时返回一个确定性假盐，避免通过挑战接口枚举账号。"""
    return hmac_mod.new(
        config.JWT_SECRET.encode("utf-8"),
        ("ag-fake-salt:" + (username or "")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def issue_challenge(username: str, salt_hex: str, purpose: str) -> str:
    nonce = secrets.token_hex(32)
    now = time.time()
    with _nonce_lock:
        _prune_nonces(now)
        if len(_nonces) >= _NONCE_MAX:      # 超限就丢掉最先过期的一半
            for k in sorted(_nonces, key=lambda k: _nonces[k]["exp"])[: _NONCE_MAX // 2]:
                _nonces.pop(k, None)
        _nonces[nonce] = {"username": username, "salt": salt_hex,
                          "purpose": purpose, "exp": now + _NONCE_TTL}
    return nonce


def consume_challenge(nonce: str, purpose: str) -> dict | None:
    """取出并立即作废（一次性）。无论校验成功与否都不再可用，天然防重放。"""
    now = time.time()
    with _nonce_lock:
        _prune_nonces(now)
        rec = _nonces.pop(nonce or "", None)
    if rec is None or rec["exp"] < now or rec["purpose"] != purpose:
        return None
    return rec


def make_proof(nonce_hex: str, verifier_hex: str) -> str:
    """proof = SHA256(nonce || verifier)，绑死一次性 nonce。"""
    return hashlib.sha256(
        bytes.fromhex(nonce_hex) + bytes.fromhex(verifier_hex)
    ).hexdigest()


def check_proof(nonce_hex: str, verifier_hex: str, proof_hex: str) -> bool:
    try:
        expect = make_proof(nonce_hex, verifier_hex)
    except Exception:
        return False
    return secrets.compare_digest(expect, (proof_hex or "").strip().lower())


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_token(username: str, is_admin: bool = False) -> str:
    payload = {
        "sub": username,
        "admin": is_admin,
        "iat": int(time.time()),
        "exp": int(time.time()) + config.JWT_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
def _user_from_token(token: str):
    try:
        payload = decode_token(token)
    except Exception:
        return None
    username = payload.get("sub")
    if not username:
        return None
    row = database.query_one("SELECT * FROM users WHERE id = ?", (username,))
    if not row or not row["active"]:
        return None
    return row


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = _user_from_token(token)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive account")
    return dict(row)


def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user
