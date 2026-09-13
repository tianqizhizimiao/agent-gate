"""Authentication routes: register (with one-time admin token), login, me.

密码传输有两条路径：

* **推荐（挑战-应答）** —— 先 ``POST /api/auth/challenge`` 拿一次性 nonce 与盐，
  浏览器内用 ``PBKDF2(password, salt, 200000)`` 算出 verifier，再算
  ``proof = SHA256(nonce || verifier)`` 发回来。**明文密码不出浏览器**；
  且 nonce 用后即废，抓到的 proof 无法重放，也不等于密码或 verifier。

* **兼容（明文）** —— 直接传 ``password``。仅用于浏览器不支持 WebCrypto 的
  非安全上下文（如通过局域网 IP 走 HTTP 访问）时的回退。
"""
from __future__ import annotations

import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import auth as authmod
from .. import database, models
from ..auth import get_current_user

router = APIRouter()

_HEX = set("0123456789abcdef")


def _valid_verifier(v: str) -> bool:
    v = (v or "").strip().lower()
    return len(v) == 64 and all(c in _HEX for c in v)


@router.post("/api/auth/challenge", response_model=models.ChallengeResponse)
def challenge(req: models.ChallengeRequest):
    """发放一次性挑战（登录 / 注册前调用）。

    账号不存在时返回**确定性假盐**，使本接口无法被用来枚举用户名。
    """
    purpose = req.purpose if req.purpose in ("login", "register") else "login"
    row = database.query_one("SELECT password_hash FROM users WHERE id = ?", (req.username,))
    if purpose == "register":
        salt = secrets.token_hex(16)                      # 新账号：服务端定盐
    else:
        salt = (authmod.salt_of(row["password_hash"]) if row else None) \
            or authmod.fake_salt(req.username)
    nonce = authmod.issue_challenge(req.username, salt, purpose)
    return models.ChallengeResponse(
        nonce=nonce, salt=salt,
        iterations=authmod.PBKDF2_ITERATIONS, algo=authmod.PBKDF2_ALGO,
    )


@router.post("/api/auth/register", response_model=models.AuthResponse)
def register(req: models.RegisterRequest):
    tok = database.query_one(
        "SELECT * FROM registration_tokens WHERE token = ?", (req.registration_token,)
    )
    if tok is None or tok["used"]:
        raise HTTPException(400, "Invalid or already-used registration token")

    if database.query_one("SELECT id FROM users WHERE id = ?", (req.username,)):
        raise HTTPException(409, "Username already taken")

    if req.nonce and req.verifier:
        rec = authmod.consume_challenge(req.nonce, "register")
        if rec is None or rec["username"] != req.username:
            raise HTTPException(400, "挑战已失效，请重新点击注册")
        if not _valid_verifier(req.verifier):
            raise HTTPException(400, "verifier 格式不正确")
        stored = authmod.make_stored(rec["salt"], req.verifier.strip().lower())
    else:
        if len(req.password or "") < 6:
            raise HTTPException(400, "密码至少 6 位")
        stored = authmod.hash_password(req.password)

    try:
        database.execute(
            "INSERT INTO users (id, password_hash, is_admin, active, created_at) VALUES (?,?,?,?,?)",
            (req.username, stored, 0, 1, database.now()),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already taken")

    # Consume the token only after the account is created.
    database.execute(
        "UPDATE registration_tokens SET used = 1, used_by = ?, used_at = ? WHERE token = ?",
        (req.username, database.now(), req.registration_token),
    )
    token = authmod.create_token(req.username, False)
    return models.AuthResponse(token=token, username=req.username, is_admin=False)


@router.post("/api/auth/login", response_model=models.AuthResponse)
def login(req: models.LoginRequest):
    row = database.query_one("SELECT * FROM users WHERE id = ?", (req.username,))
    if row is None or not row["active"]:
        raise HTTPException(401, "Invalid username or password")

    if req.nonce and req.proof:
        rec = authmod.consume_challenge(req.nonce, "login")      # 一次性，先作废
        if rec is None or rec["username"] != req.username:
            raise HTTPException(401, "Invalid username or password")
        verifier = authmod.verifier_of(row["password_hash"])
        if not verifier or not authmod.check_proof(req.nonce, verifier, req.proof):
            raise HTTPException(401, "Invalid username or password")
    else:
        if not authmod.verify_password(req.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")

    token = authmod.create_token(row["id"], bool(row["is_admin"]))
    return models.AuthResponse(token=token, username=row["id"], is_admin=bool(row["is_admin"]))


@router.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"id": user["id"], "username": user["id"], "is_admin": bool(user["is_admin"])}
