"""AgentGate FastAPI application factory + startup bootstrap."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, database
from .proxy.router import router as proxy_router
from .routers import admin, apikeys, auth as auth_router, pages, toolgroups, upstreams, usage


def bootstrap_admin() -> None:
    """确保 ``admin.json`` 指定的管理员账号存在、启用、密码一致。

    规则：
      * ``admin.json`` 里的**用户名与密码决定这个账号**；
      * 该账号必定 ``is_admin = 1``、``active = 1``，密码与配置文件一致
        —— 所以编辑那个文件重启就是「找回入口」；
      * 改用户名 = **改名**（连同名下的 API 密钥 / 上游 / 用量 / 工具组在一个
        事务里迁移），而**不是**多出一个账号 —— 否则改一次配置就会凭空多出一个
        新旧并存的管理员；
      * **管理员可以有多个**：由页面上的「设为管理员」主动添加，启动时不再强制
        降级其他管理员。
    """
    name = config.ADMIN_USERNAME
    pw_hash = auth.hash_password(config.ADMIN_PASSWORD)
    target = database.query_one("SELECT * FROM users WHERE id = ?", (name,))
    others = database.query(
        "SELECT id FROM users WHERE is_admin = 1 AND id <> ? ORDER BY created_at", (name,)
    )

    if target is None:
        # 名称不存在：只有唯一一个管理员时把它「改名」过来，避免多出一个账号
        if len(others) == 1:
            old = others[0]["id"]
            _rename_user(old, name)
            print(f"[bootstrap] 管理员账号已由 {old!r} 改名为 {name!r}（名下数据一并迁移）")
        else:
            if others:
                print(f"[bootstrap] 已有多个管理员 {[o['id'] for o in others]}，"
                      f"无法判断该改名哪一个 → 直接新建 {name!r}")
            database.execute(
                "INSERT INTO users (id, password_hash, is_admin, active, created_at) "
                "VALUES (?,?,?,?,?)",
                (name, pw_hash, 1, 1, database.now()),
            )
            print(f"[bootstrap] 已创建管理员账号 {name!r} / 密码 {config.ADMIN_PASSWORD!r}")

    database.execute(
        "UPDATE users SET is_admin = 1, active = 1, password_hash = ? WHERE id = ?",
        (pw_hash, name),
    )
    admins = [r["id"] for r in database.query(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY created_at")]
    print(f"[bootstrap] 配置账号 = {name!r}；当前管理员（{len(admins)}）：{admins}")


# 与用户 id 关联的所有外键（改名时要级联迁移）
_OWNERSHIP = (
    ("api_keys", "user_id"),
    ("upstreams", "user_id"),
    ("usage", "user_id"),
    ("tool_groups", "owner_id"),
    ("tool_group_members", "user_id"),
    ("registration_tokens", "used_by"),
)


def _owns_anything(uid: str) -> bool:
    for table, col in _OWNERSHIP:
        if database.query_one(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (uid,)):
            return True
    return False


def _rename_user(old: str, new: str) -> None:
    """把账号 ``old`` 改名成 ``new``，并级联迁移它名下的全部数据（单事务，原子）。"""
    with database.transaction(defer_fk=True) as conn:
        for table, col in _OWNERSHIP:
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new, old))
        conn.execute("UPDATE users SET id = ? WHERE id = ?", (new, old))


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    bootstrap_admin()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AgentGate", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _guard_upload_size(request: Request, call_next):
        """上传接口在解析 multipart **之前**先用 Content-Length 拦一道。

        否则一个几百 MB 的 body 会被完整收下来（Starlette 会把超过 1 MB 的部分
        落到临时文件），等到接口里的校验生效时磁盘已经被写过了。
        """
        if request.method == "POST" and request.url.path.endswith("/files"):
            raw = request.headers.get("content-length")
            if raw and raw.isdigit():
                # multipart 的边界/头部本身也占字节，留 1 MB 余量，免得卡在上限的文件被误杀
                if int(raw) > config.MAX_TOTAL_UPLOAD_BYTES + (1 << 20):
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": "请求体过大：单次上传合计最多 "
                            f"{config.MAX_TOTAL_UPLOAD_BYTES // 1048576} MB"
                        },
                    )
        return await call_next(request)

    app.include_router(pages.router)
    app.include_router(auth_router.router)
    app.include_router(upstreams.router)
    app.include_router(apikeys.router)
    app.include_router(toolgroups.router)
    app.include_router(usage.router)
    app.include_router(admin.router)
    app.include_router(proxy_router)

    return app


app = create_app()
