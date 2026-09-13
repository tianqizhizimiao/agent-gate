"""Tool group management: create/join/delete, package editing, files, reload,
tools listing, tool-call logs and members.

A tool group is a Python package folder on disk (with ``__init__.py``). The
package defines the injected prompt (``prompt(...)``) and registers tools
(``@tool``). Each group is DB-isolated (its own SQLite KV + logs under
``.agentgate/`` inside the package folder).
"""
from __future__ import annotations

import secrets
import shutil
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from .. import config, database, models
from ..auth import get_current_user
from ..gateway.group_manager import manager

router = APIRouter()

DEFAULT_INIT = '''"""Tool group package. Edit me, then click Save & Reload.

Injected globals (no import needed):
  prompt(text)   append an injected system-prompt fragment
  @tool          register a function as a callable tool; its JSON schema is
                 auto-derived from type hints + docstring
  database       per-group persistent key-value store:
                 database["k"] = v, database.get("k")
                 list / dict 支持任意深度的修改并自动写回，例如
                 database["k"].append(x)、database["a"]["b"]["c"] = 1
  name           current caller username (use str(name))

Helper .py files uploaded to this folder are importable via relative imports,
e.g. `from . import helpers`.
"""


prompt("You are a helpful assistant.")


@tool
def hello(who: str = "world") -> str:
    """Say hello to someone.
    :param who: who to greet
    """
    return f"Hello, {who}! (called by {name})"
'''


def _get_group_or_404(gid: str, user: dict):
    """返回 ``(row, is_owner, is_member, is_admin)``。

    **系统管理员视同成员**：可以查看并管理任意工具组（原先管理员对别人的组是 403）。
    """
    row = database.query_one("SELECT * FROM tool_groups WHERE id = ?", (gid,))
    if row is None:
        raise HTTPException(404, "Tool group not found")
    is_admin = bool(user.get("is_admin"))
    is_owner = row["owner_id"] == user["id"]
    is_member = is_admin or bool(
        database.query_one(
            "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
            (gid, user["id"]),
        )
    )
    if not (is_owner or is_member):
        raise HTTPException(403, "You are not a member of this tool group")
    return row, is_owner, is_member, is_admin


def _need_write(is_owner: bool, is_admin: bool, what: str = "do this") -> None:
    if not (is_owner or is_admin):
        raise HTTPException(403, f"Only the group owner or an admin can {what}")


def _earliest_member(gid: str, exclude: str | None = None) -> str | None:
    """取最早加入的**启用中**成员（按 joined_at，再按 rowid 兜底）。

    只认 ``active = 1`` 的账号 —— 否则会把组转给一个登录不了的人，
    等于又造出一个谁都管不了的僵尸组。
    """
    sql = ("SELECT m.user_id FROM tool_group_members m "
           "JOIN users u ON u.id = m.user_id "
           "WHERE m.tool_group_id = ? AND u.active = 1 ")
    args: list = [gid]
    if exclude:
        sql += "AND m.user_id <> ? "
        args.append(exclude)
    sql += "ORDER BY m.joined_at, m.rowid LIMIT 1"
    row = database.query_one(sql, tuple(args))
    return row["user_id"] if row else None


def _drop_group(gid: str, folder_name: str) -> None:
    """删除一个工具组的全部痕迹：包目录 + 成员 + 密钥绑定 + 数据库行。"""
    pkg_dir = config.TOOLGROUPS_DIR / folder_name
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir, ignore_errors=True)
    database.execute("DELETE FROM tool_group_members WHERE tool_group_id = ?", (gid,))
    database.execute("UPDATE api_keys SET tool_group_id = NULL WHERE tool_group_id = ?", (gid,))
    database.execute("DELETE FROM tool_groups WHERE id = ?", (gid,))
    manager.invalidate(gid)


def _transfer_group(row, new_owner: str) -> None:
    """把组的所有权转给某人（并确保他在成员表里）。"""
    gid = row["id"]
    database.execute("UPDATE tool_groups SET owner_id = ? WHERE id = ?", (new_owner, gid))
    if not database.query_one(
        "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
        (gid, new_owner),
    ):
        database.execute(
            "INSERT INTO tool_group_members (tool_group_id, user_id, joined_at) VALUES (?,?,?)",
            (gid, new_owner, database.now()),
        )


def dispose_owned_groups(owner: str, transfer: bool = True) -> dict:
    """账号被停用/删除时处置他名下所有工具组。

    ``transfer=True``（注销）按三级判定：
      1. 组内还有**其它启用中的系统管理员**成员 → **不作为**（他有超管权限，可自行接管）；
      2. 否则还有**启用中的成员** → **转给最早加入的那位**；
      3. 都没有 → **删除该组**（连同包目录、日志、KV）。

    ``transfer=False``（彻底删除）：一律删除。

    返回 ``{"untouched": [...], "transferred": {gid: 新属主}, "deleted": [...]}``。
    """
    rows = database.query("SELECT * FROM tool_groups WHERE owner_id = ?", (owner,))
    untouched: list[str] = []
    transferred: dict[str, str] = {}
    deleted: list[str] = []

    for row in rows:
        gid = row["id"]
        if transfer:
            cover = database.query_one(
                "SELECT 1 FROM tool_group_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.tool_group_id = ? AND m.user_id <> ? "
                "AND u.is_admin = 1 AND u.active = 1 LIMIT 1",
                (gid, owner),
            )
            if cover:
                untouched.append(gid)          # 有管理员兜底，保持原样
                continue
            heir = _earliest_member(gid, exclude=owner)
            if heir:
                _transfer_group(row, heir)
                transferred[gid] = heir
                continue
        _drop_group(gid, row["folder_name"])
        deleted.append(gid)

    return {"untouched": untouched, "transferred": transferred, "deleted": deleted}


def _out(row, user_id: str, is_owner: bool, is_member: bool) -> models.ToolGroupOut:
    return models.ToolGroupOut(
        id=row["id"],
        name=row["name"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        is_owner=is_owner,
        is_member=is_member,
    )


@router.get("/api/toolgroups", response_model=list[models.ToolGroupOut])
def list_toolgroups(user: dict = Depends(get_current_user)):
    rows = database.query(
        "SELECT tg.* FROM tool_groups tg "
        "JOIN tool_group_members m ON m.tool_group_id = tg.id "
        "WHERE m.user_id = ? ORDER BY tg.created_at DESC",
        (user["id"],),
    )
    return [_out(r, user["id"], r["owner_id"] == user["id"], True) for r in rows]


@router.post("/api/toolgroups", response_model=models.ToolGroupOut)
def create_toolgroup(req: models.ToolGroupCreate, user: dict = Depends(get_current_user)):
    gid = uuid.uuid4().hex
    folder = "tg_" + secrets.token_hex(4)
    pkg_dir = config.TOOLGROUPS_DIR / folder
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(DEFAULT_INIT, encoding="utf-8")
    now = database.now()
    database.execute(
        "INSERT INTO tool_groups (id, name, owner_id, folder_name, created_at) VALUES (?,?,?,?,?)",
        (gid, req.name, user["id"], folder, now),
    )
    database.execute(
        "INSERT INTO tool_group_members (tool_group_id, user_id, joined_at) VALUES (?,?,?)",
        (gid, user["id"], now),
    )
    manager.invalidate(gid)
    row = database.query_one("SELECT * FROM tool_groups WHERE id = ?", (gid,))
    return _out(row, user["id"], True, True)


@router.get("/api/toolgroups/{gid}", response_model=models.ToolGroupOut)
def get_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    row, is_owner, is_member, _admin = _get_group_or_404(gid, user)
    return _out(row, user["id"], is_owner, is_member)


@router.post("/api/toolgroups/{gid}/join")
def join_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    row = database.query_one("SELECT id FROM tool_groups WHERE id = ?", (gid,))
    if row is None:
        raise HTTPException(404, "Tool group not found")
    existing = database.query_one(
        "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
        (gid, user["id"]),
    )
    if existing is None:
        database.execute(
            "INSERT INTO tool_group_members (tool_group_id, user_id, joined_at) VALUES (?,?,?)",
            (gid, user["id"], database.now()),
        )
    return {"ok": True}


@router.delete("/api/toolgroups/{gid}")
def delete_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "delete a tool group")
    _drop_group(gid, row["folder_name"])
    return {"ok": True}


# -- package __init__.py ----------------------------------------------
@router.get("/api/toolgroups/{gid}/init")
def get_init(gid: str, user: dict = Depends(get_current_user)):
    row, _owner, _member, _admin = _get_group_or_404(gid, user)
    init_path = config.TOOLGROUPS_DIR / row["folder_name"] / "__init__.py"
    content = init_path.read_text(encoding="utf-8") if init_path.exists() else ""
    return {"content": content}


@router.put("/api/toolgroups/{gid}/init")
def put_init(gid: str, req: models.InitFileUpdate, user: dict = Depends(get_current_user)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "edit __init__.py")
    pkg_dir = config.TOOLGROUPS_DIR / row["folder_name"]
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(req.content, encoding="utf-8")
    manager.invalidate(gid)
    return {"ok": True}


# -- reload / tools ---------------------------------------------------
@router.post("/api/toolgroups/{gid}/reload")
async def reload_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "reload")
    manager.invalidate(gid)
    rt = await manager.get(gid)
    try:
        await rt.ensure_loaded(user["id"])
    except Exception:
        return {"tools": [], "prompt": False, "error": rt.last_error}
    return {"tools": rt.tools.list_tools(), "prompt": bool(rt.prompt), "error": None}


@router.get("/api/toolgroups/{gid}/tools")
async def tools_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    _get_group_or_404(gid, user)
    rt = await manager.get(gid)
    try:
        await rt.ensure_loaded(user["id"])
    except Exception:
        return []
    return rt.tools.list_tools() if rt.tools else []


# -- files ------------------------------------------------------------
def _safe_relpath(name: str) -> str | None:
    name = (name or "").replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ".agentgate" in parts:
        return None
    return "/".join(parts)


@router.get("/api/toolgroups/{gid}/files")
def list_files(gid: str, user: dict = Depends(get_current_user)):
    row, _owner, _member, _admin = _get_group_or_404(gid, user)
    pkg_dir = config.TOOLGROUPS_DIR / row["folder_name"]
    out = []
    if pkg_dir.exists():
        for p in sorted(pkg_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name in (".agentgate", "__pycache__"):
                continue
            out.append({"name": p.name, "size": 0 if p.is_dir() else p.stat().st_size, "is_dir": p.is_dir()})
    return out


@router.post("/api/toolgroups/{gid}/files")
async def upload_file(gid: str, user: dict = Depends(get_current_user), file: UploadFile = File(...)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "upload files")
    rel = _safe_relpath(file.filename or "upload.bin")
    if rel is None:
        raise HTTPException(400, "Invalid file name")
    pkg_dir = config.TOOLGROUPS_DIR / row["folder_name"]
    target = pkg_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    contents = await file.read()
    if len(contents) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large")
    target.write_bytes(contents)
    manager.invalidate(gid)
    return {"ok": True, "name": rel}


@router.delete("/api/toolgroups/{gid}/files/{name:path}")
def delete_file(gid: str, name: str, user: dict = Depends(get_current_user)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "delete files")
    rel = _safe_relpath(name)
    if rel is None:
        raise HTTPException(400, "Invalid file name")
    if rel == "__init__.py":
        raise HTTPException(400, "Cannot delete __init__.py")
    target = config.TOOLGROUPS_DIR / row["folder_name"] / rel
    if target.exists() and target.is_file():
        target.unlink()
    manager.invalidate(gid)
    return {"ok": True}


# -- logs / members ---------------------------------------------------
@router.get("/api/toolgroups/{gid}/logs")
async def logs_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    _get_group_or_404(gid, user)
    rt = await manager.get(gid)
    return rt.logs.list()


@router.get("/api/toolgroups/{gid}/members")
def members_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    rows = database.query(
        "SELECT user_id, joined_at FROM tool_group_members WHERE tool_group_id = ? "
        "ORDER BY joined_at, rowid",
        (gid,),
    )
    return [{"user_id": r["user_id"], "joined_at": r["joined_at"],
             "is_owner": r["user_id"] == row["owner_id"],
             "is_me": r["user_id"] == user["id"]} for r in rows]


@router.delete("/api/toolgroups/{gid}/members/{name}")
def remove_member(gid: str, name: str, user: dict = Depends(get_current_user)):
    """把某个成员移出工具组（**组属主**或**系统管理员**）。

    同时解绑他绑定到本组的 API 密钥 —— 否则移除只是形式，密钥仍会注入本组的工具。
    """
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "remove members")
    if name == row["owner_id"]:
        raise HTTPException(400, "不能移除属主；请让对方主动退出，或先转让所有权")
    if not database.query_one(
        "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?", (gid, name)
    ):
        raise HTTPException(404, "该用户不在这个工具组里")
    database.execute(
        "DELETE FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?", (gid, name)
    )
    database.execute(
        "UPDATE api_keys SET tool_group_id = NULL WHERE tool_group_id = ? AND user_id = ?",
        (gid, name),
    )
    return {"ok": True}


@router.post("/api/toolgroups/{gid}/leave")
def leave_toolgroup(gid: str, user: dict = Depends(get_current_user)):
    """主动退出工具组（所有人都可以，包括属主与系统管理员）。

    属主退出时把所有权**转给最早加入的成员**；若已无其他成员，则删除该组。
    退出会同时解绑自己绑定到本组的密钥。
    """
    row, is_owner, _member, _admin = _get_group_or_404(gid, user)
    database.execute(
        "UPDATE api_keys SET tool_group_id = NULL WHERE tool_group_id = ? AND user_id = ?",
        (gid, user["id"]),
    )
    if is_owner:
        heir = _earliest_member(gid, exclude=user["id"])
        if heir:
            _transfer_group(row, heir)
            database.execute(
                "DELETE FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
                (gid, user["id"]),
            )
            return {"ok": True, "transferred_to": heir}
        _drop_group(gid, row["folder_name"])
        return {"ok": True, "deleted": True}
    database.execute(
        "DELETE FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?", (gid, user["id"])
    )
    return {"ok": True}


@router.post("/api/toolgroups/{gid}/transfer")
def transfer_toolgroup(gid: str, req: models.TransferRequest,
                       user: dict = Depends(get_current_user)):
    """把工具组转让给指定成员（组属主或系统管理员）。"""
    row, is_owner, _member, is_admin = _get_group_or_404(gid, user)
    _need_write(is_owner, is_admin, "transfer ownership")
    target = req.user_id
    if not database.query_one(
        "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?", (gid, target)
    ):
        raise HTTPException(400, "对方不是该工具组的成员")
    _transfer_group(row, target)
    return {"ok": True, "owner_id": target}
