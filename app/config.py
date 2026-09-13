"""AgentGate configuration.

All runtime state lives under DATA_DIR. A secret key for JWT signing and a
master key for encrypting upstream API keys are generated once and persisted
in data/ so tokens/sessions survive restarts.
"""
from __future__ import annotations

import os
import secrets
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AGENTGATE_DATA", BASE_DIR / "data"))
TOOLGROUPS_DIR = DATA_DIR / "toolgroups"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# 监听地址与端口。优先级：命令行参数 > 环境变量 > 这里的默认值。
#   python run.py 9000
#   set AGENTGATE_PORT=9000 && python run.py
HOST = os.getenv("AGENTGATE_HOST", "0.0.0.0")
PORT = int(os.getenv("AGENTGATE_PORT", "8000"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
TOOLGROUPS_DIR.mkdir(parents=True, exist_ok=True)

_SECRET_FILE = DATA_DIR / "secret.json"


def _load_or_create_secrets() -> dict:
    if _SECRET_FILE.exists():
        try:
            return json.loads(_SECRET_FILE.read_text("utf-8"))
        except Exception:
            pass
    data = {
        "jwt_secret": secrets.token_hex(32),
        "fernet_key": secrets.token_urlsafe(32),
        "admin_bootstrap_token": secrets.token_urlsafe(16),
    }
    _SECRET_FILE.write_text(json.dumps(data, indent=2), "utf-8")
    return data


_secrets = _load_or_create_secrets()
JWT_SECRET = _secrets["jwt_secret"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7
FERNET_KEY = _secrets["fernet_key"]

# First-run bootstrap admin registration token (printed once on startup).
ADMIN_BOOTSTRAP_TOKEN = _secrets["admin_bootstrap_token"]

GLOBAL_DB_PATH = DATA_DIR / "global.db"
TOOL_CALL_TIMEOUT_SECONDS = 30
LOG_RETENTION_SECONDS = 3600  # 1 hour
# ---- 上传防爆限制（针对工具组包目录）----
MAX_UPLOAD_BYTES = 30 * 1024 * 1024        # 单个文件最大 30 MB
MAX_UPLOAD_FILES = 30                      # 单次上传的文件数必须「小于」此值（即最多 29 个）
MAX_UPLOAD_DEPTH = 3                       # 目录最深 3 层
MAX_TOTAL_UPLOAD_BYTES = 100 * 1024 * 1024  # 单次请求所有文件合计上限 100 MB（防超大 body）

# Username used by the bootstrap admin account (created on startup).
# Credentials are read from BASE_DIR/admin.json (copy admin.json.example),
# then AGENTGATE_ADMIN_USER / AGENTGATE_ADMIN_PASS env vars, then fall back to
# the random persisted ADMIN_BOOTSTRAP_TOKEN (printed once on first run).


def _load_admin_creds() -> tuple[str, str]:
    user = os.environ.get("AGENTGATE_ADMIN_USER", "admin")
    password = None
    admin_file = BASE_DIR / "admin.json"
    if admin_file.exists():
        try:
            cfg = json.loads(admin_file.read_text("utf-8"))
            if cfg.get("username"):
                user = cfg["username"]
            password = cfg.get("password")
        except Exception:
            pass
    if not password:
        password = os.environ.get("AGENTGATE_ADMIN_PASS")
    if not password:
        password = ADMIN_BOOTSTRAP_TOKEN
    return user, password


ADMIN_USERNAME, ADMIN_PASSWORD = _load_admin_creds()
