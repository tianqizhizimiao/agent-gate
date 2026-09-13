"""Chat-completion orchestration: inject → upstream → intercept → execute → loop.

上游协议差异全部由 ``gateway/adapters/`` 吸收：这里只面对统一的**归一化事件流**
（content / reasoning / tool_call / usage / finish / done / error），因此同一套
工具注入、拦截、多轮循环逻辑对 OpenAI 兼容 / Anthropic / Gemini / DashScope 都成立。

流式响应的一条硬约束（OpenAI 兼容客户端，例如 DSH 用的 pi-ai）：**流里必须出现
带 ``finish_reason`` 的 chunk**，否则客户端会报 ``Stream ended without finish_reason``
并重试。因此这里：
  * 首个上游请求先「预检」——上游报错时直接返回正确的 HTTP 状态码，而不是先吐 200；
  * 任何异常分支都会补一个 ``finish_reason`` 终块再发 ``[DONE]``；
  * 上游自己没给 ``finish_reason`` 时，网关补一个。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .. import config, database, upstreams
from . import provider
from .adapters import get_adapter
from .adapters.base import openai_chunk
from .group_manager import manager
from .plugin_api import PluginContext, run_handler

MAX_STEPS = 8
TOOL_TIMEOUT = config.TOOL_CALL_TIMEOUT_SECONDS
_DETAIL_LIMIT = 2000


def _truncate(s, n=_DETAIL_LIMIT):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def _record_usage(key_id, user_id, group_id, model, usage, status="ok"):
    u = usage or {}
    p = int(u.get("prompt_tokens", 0) or 0)
    c = int(u.get("completion_tokens", 0) or 0)
    t = int(u.get("total_tokens", (p + c)) or 0)
    database.execute(
        "INSERT INTO usage (api_key_id, user_id, tool_group_id, model, prompt_tokens, "
        "completion_tokens, total_tokens, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (key_id, user_id, group_id, model or "", p, c, t, status, database.now()),
    )


def _passthrough_fields(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in ("messages", "tools", "tool_choice", "stream", "model")}


def _with_usage_option(payload: dict) -> dict:
    so = payload.get("stream_options")
    if not isinstance(so, dict):
        so = {}
    so["include_usage"] = True
    payload["stream_options"] = so
    return payload


def _merge_usage(acc: dict, incoming: dict) -> dict:
    """同一轮内取各字段最大值（各家多次上报的累计量），跨轮再相加。"""
    for k, v in (incoming or {}).items():
        try:
            acc[k] = max(int(acc.get(k, 0) or 0), int(v or 0))
        except (TypeError, ValueError):
            pass
    return acc


class _Round:
    """一轮上游调用的产出累积。"""

    def __init__(self):
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tool_buf: dict[int, dict] = {}
        self.finish_reason = None
        self.usage: dict = {}
        self.error: str | None = None

    def add_tool_call(self, ev: dict) -> None:
        idx = ev.get("index", 0)
        slot = self.tool_buf.setdefault(idx, {"id": None, "name": None, "args": ""})
        if ev.get("id"):
            slot["id"] = ev["id"]
        if ev.get("name"):
            slot["name"] = ev["name"]
        if ev.get("arguments"):
            slot["args"] += ev["arguments"]

    @property
    def merged_tool_calls(self) -> list[dict]:
        out = []
        for i in sorted(self.tool_buf):
            m = self.tool_buf[i]
            out.append({
                "index": i,
                "id": m["id"] or f"call_{i}",
                "name": m["name"] or "",
                "args": m["args"] or "{}",
            })
        return out

    def as_openai_tool_calls(self) -> list[dict]:
        return [{
            "id": m["id"],
            "type": "function",
            "function": {"name": m["name"], "arguments": m["args"]},
        } for m in self.merged_tool_calls]


class ChatService:
    async def run(self, body: dict, key_row: dict):
        group_id = key_row["tool_group_id"]
        user_id = key_row["user_id"]
        key_id = key_row["id"]
        stream = bool(body.get("stream"))
        requested = body.get("model") or ""

        # 按请求的 model 从该账户的多个上游中挑选一个
        resolved = upstreams.resolve(user_id, requested, legacy=key_row)
        if resolved is None:
            return ("json", {"error": {
                "message": "该账户尚未配置上游 API，请先在「上游 API」中添加",
                "type": "invalid_request_error",
            }}, 400)
        upstream_base, upstream_key, model, protocol = resolved
        model = model or requested

        if not (upstream_key or "").strip():
            return ("json", {"error": {
                "message": "所选上游未设置 API 密钥，请在「上游 API」中补全后再试",
                "type": "invalid_request_error",
            }}, 400)

        adapter = get_adapter(protocol)

        rt = None
        if group_id:
            # 密钥绑定了工具组，但属主已不在组里（被移除 / 已退出）→ 退化为直连，
            # 否则「移除成员」只是形式，密钥照旧会注入该组的提示词与工具。
            still_member = database.query_one(
                "SELECT 1 FROM tool_group_members WHERE tool_group_id = ? AND user_id = ?",
                (group_id, user_id),
            ) is not None
            if still_member:
                rt = await manager.get(group_id)
                if rt is None:
                    return ("json", {"error": {
                        "message": f"工具组 {group_id} 不存在",
                        "type": "invalid_request_error",
                    }}, 400)
                try:
                    await rt.ensure_loaded(user_id)
                except Exception:
                    # 包加载失败：退化为直连，用户仍能拿到回复（加载错误在重载界面可见）
                    rt = None

        # ---------------- 非流式 ----------------
        if not stream:
            if rt is None:
                data, status = await self._passthrough_json(body, adapter, upstream_base,
                                                            upstream_key, model, key_id, user_id, group_id)
            else:
                data, status = await self._group_nonstream(body, adapter, upstream_base, upstream_key,
                                                           rt, user_id, model, key_id, group_id)
            return ("json", data, status)

        # ---------------- 流式 ----------------
        # 先建好第一个上游连接，失败就用正确的 HTTP 状态码返回 JSON 错误
        if rt is None:
            payload = _with_usage_option(self._passthrough_payload(body, model))
            session, err = await self._open_stream(adapter, upstream_base, upstream_key, payload)
            if err is not None:
                _record_usage(key_id, user_id, group_id, model, None, "error")
                return ("json", err[0], err[1])
            return ("stream", self._passthrough_stream(session, model, key_id, user_id, group_id), 200)

        messages = rt.prompt.inject(body.get("messages") or [])
        merged_tools, choice = rt.tools.inject_tools(body.get("tools"), body.get("tool_choice"))
        extra = _passthrough_fields(body)
        payload = _with_usage_option(self._group_payload(extra, model, messages, merged_tools, choice))
        session, err = await self._open_stream(adapter, upstream_base, upstream_key, payload)
        if err is not None:
            _record_usage(key_id, user_id, group_id, model, None, "error")
            return ("json", err[0], err[1])
        gen = self._group_stream(
            session, adapter, upstream_base, upstream_key, extra, model, messages,
            merged_tools, choice, rt, user_id, key_id, group_id,
        )
        return ("stream", gen, 200)

    # -- 上游连接预检 -----------------------------------------------------
    async def _open_stream(self, adapter, base, key, payload):
        """返回 ``(session, None)`` 或 ``(None, (错误JSON, 状态码))``。"""
        try:
            session = await adapter.open_stream(base, key, payload)
        except Exception as e:
            return None, ({"error": {
                "message": provider.friendly_error(e), "type": "upstream_error",
            }}, 502)
        if session.status_code >= 400:
            text = await session.error_text()
            code = session.status_code
            await session.close()
            try:
                data = json.loads(text)
            except Exception:
                data = {"error": {
                    "message": (text or f"上游返回 {code}")[:500],
                    "type": "upstream_error",
                }}
            return None, (data, code)
        return session, None

    # -- SSE 分片构造 -----------------------------------------------------
    @staticmethod
    def _sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def _chunk(self, model, delta, finish=None, usage=None,
               chunk_id="chatcmpl-agentgate", created=None) -> str:
        return self._sse(openai_chunk(model, delta, finish, usage, chunk_id, created))

    @staticmethod
    def _error_chunk(message: str) -> str:
        return "data: " + json.dumps(
            {"error": {"message": message, "type": "upstream_error"}}, ensure_ascii=False
        ) + "\n\n"

    @staticmethod
    def _passthrough_payload(body: dict, model: str) -> dict:
        payload = dict(body)
        if model:
            payload["model"] = model
        payload["stream"] = True
        return payload

    @staticmethod
    def _group_payload(extra: dict, model, messages, merged_tools, choice) -> dict:
        payload = dict(extra)
        payload["model"] = model
        payload["messages"] = messages
        payload["stream"] = True
        if merged_tools:
            payload["tools"] = merged_tools
            payload["tool_choice"] = choice
        return payload

    # -- 事件泵 -----------------------------------------------------------
    async def _pump(self, session, rnd: _Round, model: str, forward_tools: bool,
                    state: dict):
        """消费归一化事件，边收边产出 SSE；同时把产出累积到 ``rnd``。

        ``state`` 里维护 role/开始标记 与 chunk id。
        """
        try:
            async for ev in session.events():
                t = ev.get("type")
                if t == "content":
                    rnd.content.append(ev.get("text") or "")
                    yield self._delta(model, {"content": ev.get("text") or ""}, state)
                elif t == "reasoning":
                    rnd.reasoning.append(ev.get("text") or "")
                    yield self._delta(model, {"reasoning_content": ev.get("text") or ""}, state)
                elif t == "tool_call":
                    rnd.add_tool_call(ev)
                    if forward_tools:
                        tc: dict = {"index": ev.get("index", 0)}
                        if ev.get("id"):
                            tc["id"] = ev["id"]
                            tc["type"] = "function"
                        fn: dict = {}
                        if ev.get("name"):
                            fn["name"] = ev["name"]
                        if ev.get("arguments"):
                            fn["arguments"] = ev["arguments"]
                        if fn:
                            tc["function"] = fn
                        yield self._delta(model, {"tool_calls": [tc]}, state)
                elif t == "usage":
                    _merge_usage(rnd.usage, ev.get("usage") or {})
                elif t == "finish":
                    rnd.finish_reason = ev.get("reason") or rnd.finish_reason
                elif t == "error":
                    rnd.error = ev.get("message") or "上游返回错误"
                elif t == "done":
                    break
        finally:
            await session.close()
        # 收尾纠正：出现过工具调用却只报 stop 的（如 Gemini 只说 STOP），统一改成 tool_calls
        if rnd.tool_buf and rnd.finish_reason in (None, "stop"):
            rnd.finish_reason = "tool_calls"

    @staticmethod
    def _delta(model: str, delta: dict, state: dict) -> str:
        d = dict(delta)
        if not state["started"]:
            state["started"] = True
            d = {"role": "assistant", **d}          # 首个分片始终带 role
        elif "role" in d:
            d.pop("role")
        return ChatService._sse(
            openai_chunk(model, d, None, None, state["chunk_id"], state["created"])
        )

    def _finish_pair(self, model, reason, usage, state, extra_delta=None) -> str:
        out = ""
        if extra_delta:
            out += self._delta(model, extra_delta, state)
        out += self._sse(openai_chunk(model, {}, reason or "stop", usage,
                                      state["chunk_id"], state["created"]))
        out += "data: [DONE]\n\n"
        return out

    # -- tool execution (all-or-nothing) ---------------------------------
    async def _execute(self, rt, tool_calls, caller):
        for tc in tool_calls:
            if not rt.tools.is_server(tc["name"]):
                return None  # passthrough: at least one client tool
        results = []
        for tc in tool_calls:
            handler = rt.tools.handler_for(tc["name"])
            ctx = PluginContext(rt.tools, rt.prompt, rt.storage, caller)
            t0 = time.time()
            try:
                ret = await asyncio.wait_for(
                    asyncio.to_thread(run_handler, ctx, handler, tc["arguments"]),
                    timeout=TOOL_TIMEOUT,
                )
                content = ret if isinstance(ret, str) else json.dumps(ret, ensure_ascii=False, default=str)
                status = "normal"
                detail = _truncate(content)
            except asyncio.TimeoutError:
                content = f"Tool error: timed out after {TOOL_TIMEOUT}s"
                status = "exception"
                detail = content
            except Exception as e:
                content = f"Tool error: {e}"
                status = "exception"
                detail = _truncate(str(e))
            dur = int((time.time() - t0) * 1000)
            rt.logs.add(tc["name"], status, dur, detail)
            results.append({"role": "tool", "tool_call_id": tc["id"], "content": content, "name": tc["name"]})
        return results

    # -- passthrough (no tool group, or load failure) --------------------
    async def _passthrough_json(self, body, adapter, base, key, model, key_id, user_id, group_id):
        payload = dict(body)
        if model:
            payload["model"] = model
        payload["stream"] = False
        try:
            data = await adapter.chat(base, key, payload, provider.UPSTREAM_TIMEOUT)
            _record_usage(key_id, user_id, group_id, model, data.get("usage"), "ok")
            return data, 200
        except httpx.HTTPStatusError as e:
            _record_usage(key_id, user_id, group_id, model, None, "error")
            try:
                err = e.response.json()
            except Exception:
                err = {"error": {"message": e.response.text, "type": "upstream_error"}}
            return err, e.response.status_code
        except Exception as e:
            _record_usage(key_id, user_id, group_id, model, None, "error")
            return {"error": {"message": provider.friendly_error(e), "type": "upstream_error"}}, 502

    async def _passthrough_stream(self, session, model, key_id, user_id, group_id):
        rnd = _Round()
        state = {"started": False, "chunk_id": "chatcmpl-agentgate", "created": int(time.time())}
        status = "ok"
        try:
            async for piece in self._pump(session, rnd, model, forward_tools=True, state=state):
                yield piece
            if rnd.error:
                status = "error"
                yield self._error_chunk(rnd.error)
            yield self._finish_pair(model, rnd.finish_reason or "stop", rnd.usage or None, state)
        except Exception as e:
            status = "error"
            yield self._error_chunk(provider.friendly_error(e))
            yield self._finish_pair(model, rnd.finish_reason or "stop", rnd.usage or None, state)
        _record_usage(key_id, user_id, group_id, model, rnd.usage, status)

    # -- tool-group non-streaming ----------------------------------------
    async def _group_nonstream(self, body, adapter, base, key, rt, caller, model, key_id, group_id):
        messages = rt.prompt.inject(body.get("messages") or [])
        merged_tools, choice = rt.tools.inject_tools(body.get("tools"), body.get("tool_choice"))
        extra = _passthrough_fields(body)
        acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        last_data = None
        for _step in range(MAX_STEPS):
            payload = dict(extra)
            payload["model"] = model
            payload["messages"] = messages
            payload["stream"] = False
            if merged_tools:
                payload["tools"] = merged_tools
                payload["tool_choice"] = choice
            try:
                data = await adapter.chat(base, key, payload, provider.UPSTREAM_TIMEOUT)
            except httpx.HTTPStatusError as e:
                _record_usage(key_id, caller, group_id, model, acc, "error")
                try:
                    err = e.response.json()
                except Exception:
                    err = {"error": {"message": e.response.text, "type": "upstream_error"}}
                return err, e.response.status_code
            except Exception as e:
                _record_usage(key_id, caller, group_id, model, acc, "error")
                return {"error": {"message": provider.friendly_error(e), "type": "upstream_error"}}, 502

            last_data = data
            u = data.get("usage") or {}
            for k in acc:
                acc[k] += int(u.get(k, 0) or 0)
            choice0 = (data.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            tool_calls = rt.tools.extract_tool_calls(msg)
            if not tool_calls:
                _record_usage(key_id, caller, group_id, model, acc, "ok")
                return data, 200
            results = await self._execute(rt, tool_calls, caller)
            if results is None:
                _record_usage(key_id, caller, group_id, model, acc, "ok")
                return data, 200
            messages.append(msg)
            messages.extend(results)
        _record_usage(key_id, caller, group_id, model, acc, "ok")
        return last_data, 200

    # -- tool-group streaming --------------------------------------------
    async def _group_stream(self, first_session, adapter, base, key, extra, model, messages,
                            merged_tools, choice, rt, caller, key_id, group_id):
        acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        status = "ok"
        state = {"started": False, "chunk_id": "chatcmpl-agentgate", "created": int(time.time())}
        finish_reason = None

        try:
            for step in range(MAX_STEPS):
                payload = _with_usage_option(
                    self._group_payload(extra, model, messages, merged_tools, choice)
                )
                if step == 0:
                    session = first_session
                else:
                    session, err = await self._open_stream(adapter, base, key, payload)
                    if err is not None:
                        status = "error"
                        msg = ((err[0].get("error") or {}).get("message")) or "上游错误"
                        yield self._error_chunk(msg)
                        yield self._finish_pair(model, finish_reason or "stop", acc, state)
                        _record_usage(key_id, caller, group_id, model, acc, status)
                        return

                rnd = _Round()
                async for piece in self._pump(session, rnd, model, forward_tools=False, state=state):
                    yield piece

                _merge_usage(acc, rnd.usage)
                finish_reason = rnd.finish_reason or finish_reason

                if rnd.error:
                    status = "error"
                    yield self._error_chunk(rnd.error)
                    yield self._finish_pair(model, finish_reason or "stop", acc, state)
                    _record_usage(key_id, caller, group_id, model, acc, status)
                    return

                tool_calls = rnd.merged_tool_calls
                if not tool_calls:
                    yield self._finish_pair(model, finish_reason or "stop", acc, state)
                    _record_usage(key_id, caller, group_id, model, acc, status)
                    return

                exec_input = []
                for tc in tool_calls:
                    try:
                        parsed = json.loads(tc["args"] or "{}")
                    except Exception:
                        parsed = {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                    exec_input.append({"id": tc["id"], "name": tc["name"], "arguments": parsed})
                results = await self._execute(rt, exec_input, caller)
                if results is None:
                    # Passthrough：把完整的工具调用交回客户端
                    for i, tc in enumerate(tool_calls):
                        yield self._delta(model, {"tool_calls": [{
                            "index": i, "id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["args"]},
                        }]}, state)
                    yield self._finish_pair(model, finish_reason or "tool_calls", None, state)
                    _record_usage(key_id, caller, group_id, model, acc, status)
                    return

                # 全为服务端工具：吞掉工具分片，执行后继续下一轮
                messages.append({
                    "role": "assistant",
                    "content": "".join(rnd.content) or None,
                    "tool_calls": rnd.as_openai_tool_calls(),
                })
                messages.extend(results)

            yield self._finish_pair(model, finish_reason or "stop", acc, state)
            _record_usage(key_id, caller, group_id, model, acc, status)
        except Exception as e:
            status = "error"
            try:
                yield self._error_chunk(str(e))
            except Exception:
                pass
            try:
                yield self._finish_pair(model, finish_reason or "stop", acc, state)
            except Exception:
                pass
            _record_usage(key_id, caller, group_id, model, acc, status)


chat_service = ChatService()
