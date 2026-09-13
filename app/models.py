"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class ChallengeRequest(BaseModel):
    """申请一次挑战（登录或注册前）。"""
    username: str
    purpose: str = "login"          # login | register


class ChallengeResponse(BaseModel):
    nonce: str                      # 一次性随机数，120 秒内有效
    salt: str                       # 十六进制盐（注册时由服务端生成）
    iterations: int
    algo: str = "pbkdf2_sha256"
    proof: str = "sha256(nonce||verifier)"


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    registration_token: str
    # 推荐路径：前端算好的 verifier（明文不出浏览器）
    nonce: str = ""
    verifier: str = ""
    # 兼容路径：直接传明文（非安全上下文时回退）
    password: str = ""


class LoginRequest(BaseModel):
    username: str
    # 推荐路径：一次性挑战 + proof
    nonce: str = ""
    proof: str = ""
    # 兼容路径：直接传明文（非安全上下文时回退）
    password: str = ""


class AuthResponse(BaseModel):
    token: str
    username: str
    is_admin: bool


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
class ApiKeyCreate(BaseModel):
    label: str = ""
    custom_key: str = ""              # 可选：手动定义的密钥主体
    upstream_name: str = ""
    upstream_base_url: str = ""
    upstream_api_key: str = ""
    upstream_model: str = ""
    upstream_models: List[str] = []   # 可供下游选择的模型列表


class ApiKeyBind(BaseModel):
    tool_group_id: Optional[str] = None


class ApiKeyUpdate(BaseModel):
    """部分更新：仅传入的字段会被修改。"""
    label: Optional[str] = None
    upstream_name: Optional[str] = None
    upstream_base_url: Optional[str] = None
    upstream_api_key: Optional[str] = None
    upstream_model: Optional[str] = None
    upstream_models: Optional[List[str]] = None


class ApiKeyOut(BaseModel):
    id: str
    key_prefix: str
    key: str = ""                     # 明文密钥（可在控制台随时复制）
    label: str
    upstream_name: str
    upstream_base_url: str
    upstream_model: str
    upstream_models: List[str] = []
    tool_group_id: Optional[str]
    created_at: float


class ApiKeyCreated(ApiKeyOut):
    full_key: str


# --------------------------------------------------------------------------- #
# Upstreams（账户级上游 API / 渠道）
# --------------------------------------------------------------------------- #
class UpstreamCreate(BaseModel):
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    protocol: str = ""          # 探测出的协议（openai/anthropic/gemini/dashscope）
    models: List[str] = []
    default_model: str = ""


class UpstreamUpdate(BaseModel):
    """部分更新；api_key 传空字符串表示保持原值不变。"""
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    protocol: Optional[str] = None
    models: Optional[List[str]] = None
    default_model: Optional[str] = None


class ProbeModelsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


# --------------------------------------------------------------------------- #
# Tool groups
# --------------------------------------------------------------------------- #
class ToolGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ToolGroupOut(BaseModel):
    id: str
    name: str
    owner_id: str
    created_at: float
    is_owner: bool
    is_member: bool


class InitFileUpdate(BaseModel):
    content: str


class TransferRequest(BaseModel):
    user_id: str


class FilesDelete(BaseModel):
    """批量删除工具组包内文件。``names`` 是相对包根目录的路径。"""
    names: list[str] = Field(default_factory=list, max_length=500)


class ToolOut(BaseModel):
    name: str
    description: str
    parameters: dict


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class RegistrationTokenOut(BaseModel):
    token: str
    created_at: float
    used: bool
    used_by: Optional[str]


class AdminUserOut(BaseModel):
    id: str
    is_admin: bool
    active: bool
    created_at: float
    is_bootstrap: bool = False   # 是否由 admin.json 指定的那个管理员


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
class UsageSummary(BaseModel):
    total_requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    by_model: List[dict]
    by_day: List[dict]
