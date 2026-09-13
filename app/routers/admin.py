"""Admin routes: one-time registration tokens + account management."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException

from .. import config, database, models
from ..auth import get_admin_user
from .toolgroups import dispose_owned_groups

router = APIRouter()


@router.get("/api/admin/registration-tokens", response_model=list[models.RegistrationTokenOut])
def list_tokens(admin: dict = Depends(get_admin_user)):
    rows = database.query("SELECT * FROM registration_tokens ORDER BY created_at DESC")
    return [
        models.RegistrationTokenOut(
            token=r["token"], created_at=r["created_at"], used=bool(r["used"]), used_by=r["used_by"]
        )
        for r in rows
    ]


@router.post("/api/admin/registration-tokens")
def create_token(admin: dict = Depends(get_admin_user)):
    token = "reg-" + secrets.token_urlsafe(16)
    now = database.now()
    database.execute(
        "INSERT INTO registration_tokens (token, created_at, used) VALUES (?,?,0)",
        (token, now),
    )
    return {"token": token, "created_at": now}


@router.delete("/api/admin/registration-tokens/{token}")
def revoke_token(token: str, admin: dict = Depends(get_admin_user)):
    database.execute("DELETE FROM registration_tokens WHERE token = ? AND used = 0", (token,))
    return {"ok": True}


@router.get("/api/admin/users", response_model=list[models.AdminUserOut])
def list_users(admin: dict = Depends(get_admin_user)):
    rows = database.query("SELECT * FROM users ORDER BY created_at")
    return [
        models.AdminUserOut(
            id=r["id"], is_admin=bool(r["is_admin"]), active=bool(r["active"]),
            created_at=r["created_at"], is_bootstrap=(r["id"] == config.ADMIN_USERNAME),
        )
        for r in rows
    ]


@router.post("/api/admin/users/{name}/cancel")
def cancel_user(name: str, admin: dict = Depends(get_admin_user)):
    """停用账号（可恢复）。

    同时处置他名下的工具组：**有别的成员就转给最早加入的那位**，没有就删除该组
    —— 否则会留下「谁都改不了、删不掉」的僵尸组。
    """
    if name == admin["id"]:
        raise HTTPException(400, "You cannot cancel your own account")
    if database.query_one("SELECT id FROM users WHERE id = ?", (name,)) is None:
        raise HTTPException(404, "Account not found")
    groups = dispose_owned_groups(name)
    database.execute("UPDATE users SET active = 0 WHERE id = ?", (name,))
    return {"ok": True, "groups": groups}


@router.post("/api/admin/users/{name}/grant-admin")
def grant_admin(name: str, admin: dict = Depends(get_admin_user)):
    """把某个账号提升为管理员。"""
    if database.query_one("SELECT id FROM users WHERE id = ?", (name,)) is None:
        raise HTTPException(404, "Account not found")
    database.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (name,))
    return {"ok": True}


@router.post("/api/admin/users/{name}/revoke-admin")
def revoke_admin(name: str, admin: dict = Depends(get_admin_user)):
    """取消某个账号的管理员身份。"""
    row = database.query_one("SELECT * FROM users WHERE id = ?", (name,))
    if row is None:
        raise HTTPException(404, "Account not found")
    if name == admin["id"]:
        raise HTTPException(400, "不能取消自己的管理员身份")
    if name == config.ADMIN_USERNAME:
        raise HTTPException(400, "该账号由 admin.json 指定，无法取消管理员身份")
    database.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (name,))
    return {"ok": True}


@router.post("/api/admin/users/{name}/reactivate")
def reactivate_user(name: str, admin: dict = Depends(get_admin_user)):
    database.execute("UPDATE users SET active = 1 WHERE id = ?", (name,))
    return {"ok": True}


@router.delete("/api/admin/users/{name}")
def delete_user(name: str, admin: dict = Depends(get_admin_user)):
    """**彻底删除**账号（不可恢复）。

    连同一起清理：
      * 他名下的**所有工具组** —— 数据库行、成员关系、包目录（含代码 / KV / 日志）
      * 他参加过的其他工具组的成员关系
      * 他的全部 API 密钥、上游 API、用量记录
      * 注册令牌上的 ``used_by`` 引用

    需要「可恢复的停用」请用 ``/cancel``。
    """
    if name == admin["id"]:
        raise HTTPException(400, "不能删除自己的账号")
    if database.query_one("SELECT id FROM users WHERE id = ?", (name,)) is None:
        raise HTTPException(404, "Account not found")

    # 1) 他名下的工具组：彻底删除时一律清掉（注销走的是「转给最早成员」）
    groups = dispose_owned_groups(name, transfer=False)

    # 2) 他作为成员参加的其他工具组
    database.execute("DELETE FROM tool_group_members WHERE user_id = ?", (name,))

    # 3) 账号自身的数据
    keys = database.query("SELECT id FROM api_keys WHERE user_id = ?", (name,))
    ups = database.query("SELECT id FROM upstreams WHERE user_id = ?", (name,))
    usage = database.query("SELECT COUNT(*) AS n FROM usage WHERE user_id = ?", (name,))
    database.execute("DELETE FROM api_keys WHERE user_id = ?", (name,))
    database.execute("DELETE FROM upstreams WHERE user_id = ?", (name,))
    database.execute("DELETE FROM usage WHERE user_id = ?", (name,))
    database.execute("UPDATE registration_tokens SET used_by = NULL WHERE used_by = ?", (name,))
    database.execute("DELETE FROM users WHERE id = ?", (name,))

    return {
        "ok": True,
        "deleted": {
            "tool_groups": len(groups["deleted"]),
            "api_keys": len(keys),
            "upstreams": len(ups),
            "usage_rows": int(usage[0]["n"] if usage else 0),
        },
    }
