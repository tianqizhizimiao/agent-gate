# AgentGate

A multi-tenant LLM gateway built on the **ContextusAgent** principle —
downstream API-key auth + upstream transparent proxy, **prompt injection**, and
**all-or-nothing tool interception** — extended with user accounts, **account-level
upstream APIs (multiple per user, aggregated for downstream)**, token-usage
tracking, and tool groups that are isolated Python packages each with their own
DB and 1-hour tool-call logs.

```
client (OpenAI SDK) --Bearer sk-...--> AgentGate /v1/chat/completions
                                              |
                          resolve key -> user -> pick an upstream by `model`
                                              |     (union of all upstreams' models)
                          bound tool group? --+-- no  -> passthrough to upstream
                                              +-- yes -> INJECT prompt + tool schemas
                                         call upstream -> INTERCEPT tool_calls
                                         if ALL are server tools: run them, loop
                                         if any is a client tool: pass through
                                              |
                          record token usage (per user/key/tool-group)
```

## Features

- **Accounts** — register with a *one-time admin-issued token*; the username is
  the unique id. Login is JWT-based. Admin can list / cancel / reactivate.
- **Upstream APIs (account-level channels)** — a user configures **multiple**
  upstream APIs, each with its own base URL / key / model list. All selected
  models are **merged** and exposed downstream via `GET /v1/models`; a request's
  `model` is routed to the upstream that declares it.
- **Multi-protocol upstreams** — the gateway speaks **OpenAI-compatible,
  Anthropic Messages, Google Gemini and native DashScope** to upstreams, and
  always exposes OpenAI format downstream. The protocol is **auto-detected by
  actually probing** the address you type (it tries `/models` with three auth
  styles and classifies the response body — success *or* error shape), and can
  be overridden manually. See [Protocols](#protocols).
- **API keys (downstream tokens)** — `sk-...` keys, each bindable to one tool
  group. The key string may be **customized** at creation (`sk-<yours>-<4 random>`,
  default `sk-<48 random>`), and can be **copied again anytime** from the
  dashboard. Every key sees all of the account's upstream models.
- **Token usage** — prompt / completion / total tokens per request, by model &
  day, visible in the dashboard.
- **Tool groups** — a tool group is a Python package folder (`__init__.py` +
  uploaded helpers). It has a group id others **join**. A group can hold API
  keys from many users, but **one API key binds to at most one group**.
- **Injection + interception** — a bound key gets the group's prompt injected
  and its tools merged with the client's tools (server tools win on name
  collisions). If the model calls **only** server tools, the gateway executes
  them and loops (≤ 8 rounds); if any call is a client tool, the whole response
  is passed through. Works streaming and non-streaming.
- **Per-group isolation** — one SQLite KV store + one tool-call log DB per group
  (under the group's `.agentgate/` folder).
- **1-hour logs** — each group logs **only** tool invocations (normal +
  exception); entries older than 1 hour are dropped. Nothing else is logged.
- **Admin page** — generate / revoke one-time registration tokens; manage
  accounts; promote/demote admins. **注销 (cancel)** deactivates an account
  (login, JWTs and all downstream API keys stop working immediately) and hands
  its tool groups to the earliest-joined active member; **彻底删除 (delete)** is a
  true purge that also removes those groups outright. See
  [Notes / security](#notes--security).

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt   # fastapi, uvicorn, httpx, jinja2, python-multipart, pyjwt
python run.py                     # or: python main.py   -> http://127.0.0.1:8000
```

### Admin login

The admin role is hardcoded and always exists. Credentials are read, in order,
from:

1. `admin.json` next to the project root — `{"username":"admin","password":"..."}`
2. env vars `AGENTGATE_ADMIN_USER` / `AGENTGATE_ADMIN_PASS`
3. a random one-time token in `data/secret.json` (printed to the console on
   first run)

A default `admin.json` (`admin` / `change-me`) ships with the repo — **change
it** before exposing the server. Log in as admin → **Admin** → generate a
registration token → hand it to a new user.

## Using the gateway

1. **上游 API** 区添加一个或多个上游（地址 / 密钥 / 勾选模型）。
2. **API 密钥** 区创建一个下游密钥（`sk-...`，可自定义、可随时复制）。
3. 把下游客户端指向网关：

```python
from openai import OpenAI
client = OpenAI(api_key="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                base_url="http://localhost:8000/v1")

print([m.id for m in client.models.list()])   # 账户下所有上游模型的并集
resp = client.chat.completions.create(
    model="deepseek-chat",                     # 自动路由到声明了它的上游
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
)
print(resp.choices[0].message.content)
```

- **No tool group bound** → transparent passthrough to the routed upstream.
- **Tool group bound** → prompt + tools injected, tool calls intercepted and run
  server-side (the agent loop).

## Protocols

The gateway always speaks **OpenAI format downstream** (`/v1/chat/completions`,
`/v1/models`). Upstream, it supports four protocols and translates both ways —
request, response, streaming chunks, tool calls and usage.

| `protocol` | Upstream protocol | Endpoint used | Auth |
|---|---|---|---|
| `openai` | OpenAI compatible | `{base}/chat/completions` | `Authorization: Bearer` |
| `anthropic` | Anthropic Messages | `{base}/messages` | `x-api-key` + `anthropic-version` |
| `gemini` | Google Gemini | `{base}/models/{model}:generateContent` (`?alt=sse` when streaming) | `x-goog-api-key` |
| `dashscope` | Alibaba DashScope native | `{base}/services/aigc/text-generation/generation` | `Authorization: Bearer` + `X-DashScope-SSE` |

`openai` is by far the widest: DeepSeek, 通义千问 (compatible mode), Moonshot/Kimi,
智谱 GLM, MiniMax, SiliconFlow, Ollama, vLLM, LM Studio, one-api/new-api and
OpenRouter all speak it — no translation needed.

### How the protocol is detected

Click **探测协议并获取模型** in the upstream form. Nothing is hard-coded by vendor;
the gateway **probes the address you typed**:

1. For each candidate base (`{base}` and `{base}/v1`), it requests `/models` three
   times with the three auth styles above.
2. It classifies the **response body structure** — a successful list *or an error
   body*, since each vendor's error shape is distinctive:

   | Protocol | Success shape | Error shape |
   |---|---|---|
   | OpenAI | `{"object":"list","data":[{"id","object":"model"}]}` | `{"error":{"message","type"}}` |
   | Anthropic | `{"data":[{"id","type":"model"}]}` | `{"type":"error","error":{...}}` |
   | Gemini | `{"models":[{"name":"models/..."}]}` | `{"error":{"code","status","message"}}` |
   | DashScope | `{"output":{"choices":[...]}}` | `{"code":"...","message":"...","request_id":"..."}` |

3. It then re-requests `/models` using the auth style **matching the detected
   protocol**, to fetch the model list and confirm the key.
4. Native DashScope has no `/models`, so it additionally probes the generation
   endpoint (an empty body yields a `code`/`message` error, which is definitive).

If the shape is ambiguous you can pick the protocol manually in the form. The
result is stored in `upstreams.protocol` and used to route every request.

## Writing a tool group

A tool group's folder is a Python package. Its `__init__.py` runs at load time
with these **injected globals** (no import needed):

| name | purpose |
|------|---------|
| `prompt(text)` | append a system-prompt fragment injected into every chat |
| `@tool` | register a function as a callable tool; its JSON schema is auto-derived from type hints + docstring (`:param x: ...`) |
| `database` | per-group persistent key-value store: `database["k"] = v`, `database.get("k")`, `database.keys()` |
| `name` | current caller's username — use `str(name)` |

`database` 的值以 JSON 存储，且 `database[key]` 返回的是**写穿代理**：对 list / dict
的任何修改都会立刻落盘，且支持**任意深度**。标量则直接返回原值，所以 `+=`、
`.upper()` 这类写法照旧可用。

```python
database["history"] = []
database["history"].append(city)          # 立即写回
database["tree"]  = {"a": {"b": []}}
database["tree"]["a"]["b"].append(1)      # 任意深度
database["tree"]["a"]["c"] = "x"          # 深度新增键
database["count"] = (database["count"] or 0) + 1
```

并发方面（工具处理函数跑在线程池里，并发是真实的）：所有读、改、写都在一个
`RLock` 内完成，而且**每次修改都会在锁内重新读取该 key 的最新内容再写回**。
由此得到一条简单的规则：

| 写法 | 是否安全 |
|------|---------|
| `database["l"].append(x)` / `database["d"][k] = v` / `.update()` / `.pop()` / `.append()` | ✅ 单条语句，内部重读，天然原子 |
| `database["d"][k] = database["d"].get(k, 0) + 1` | ❌ 读和写是两次操作，会丢 |
| `with database.lock(): ...` 或 `database.modify(k, fn)` | ✅ |

实测（8 线程 × 200 次，期望 1600）：

```
同一键 读后写  不加锁                 -> 537    丢了 1063
同一键 读后写  with database.lock()   -> 1600   无丢失
database["lst"].append(1)  不加锁     -> 1600   无丢失
```

所以「先读出来算、再写回去」这种跨语句的复合操作，务必用锁框住：

```python
with database.lock():                       # RLock，可重入，内部还会再取锁
    d = database["counts"]
    d[k] = d.get(k, 0) + 1

with database.lock():
    database["a"] += 1
    database["b"] += 1

database.modify("n", lambda v: (v or 0) + 1)   # 单 key 的原子读-改-写
```

> 性能：每次修改都会重写整个 key，超大容器（几 MB 以上）频繁追加会比较慢。

```python
prompt("You can call get_weather for any city.")

@tool
def get_weather(city: str) -> str:
    """Return the weather for a city.
    :param city: the city name
    """
    database["last_city"] = city    # persist in this group's isolated db
    return f"Sunny in {city} (asked by {name})"
```

Helper `.py` files uploaded to the folder are importable via relative imports,
e.g. `from . import helpers`. Only `__init__.py` is auto-executed (the folder is
imported as **one** package, not file-by-file like ContextusAgent). Click
**Save & Reload** in the UI to hot-reload after editing.

## Layout

```
app/
  config.py            data dir, JWT, admin creds, knobs
  database.py          global SQLite (users, tokens, upstreams, api_keys, tool_groups, members, usage)
  upstreams.py         账户级上游：增删改查、模型并集、按 model 路由
  auth.py / security.py  pbkdf2 passwords, JWT, API-key gen/lookup
  models.py            pydantic schemas
  main.py              FastAPI app factory + bootstrap
  routers/             auth, upstreams, apikeys, usage, toolgroups, admin, pages
  proxy/router.py      /v1/chat/completions, /v1/models（并集）
  gateway/             ContextusAgent-style engine:
      storage.py       per-group KV (PluginStorage)
      prompt.py        PromptInjector
      tools.py         ToolInterceptor (schema + inject + extract)
      plugin_api.py    prompt/@tool/db/name + per-context binding
      plugin_manager.py  package loader + hot reload
      logger.py        per-group 1h tool-call logs
      provider.py      upstream client
      group_manager.py GroupRuntime cache
      chat.py          inject -> upstream -> intercept -> execute -> loop
templates/  static/    UI
tools/      check_routes.py    前/后端路径一致性检查
            check_encoding.py  源码乱码/编码检查
data/       toolgroups/<folder>/   runtime state + packages (created on run)
```

### 改完代码建议跑一遍

```bash
python tools/check_routes.py     # 前端 API(...) 调用的路径是否都有后端路由（方法+形状比对）
python tools/check_encoding.py   # 所有源码是否被按错误编码读写（私用区字符 / U+FFFD / 乱码汉字）
```

> ⚠️ 在本机用 PowerShell 改这些文件时**务必指定 UTF-8**。Windows PowerShell 5.1 的
> `Get-Content -Raw` 默认按 ANSI(GBK) 解码，再配合 `Set-Content`/`WriteAllText` 写回
> 会把中文**不可逆**地写成乱码。用编辑器改，或显式 `-Encoding utf8`。

## Notes / security

- **Login never sends the password.** The browser asks for a one-time challenge
  (`POST /api/auth/challenge` → `nonce` + `salt`), derives
  `verifier = PBKDF2-HMAC-SHA256(password, salt, 200000)` **locally**, and posts
  `proof = SHA256(nonce ‖ verifier)`. Plaintext never leaves the browser, the
  nonce is single-use (so a captured proof cannot be replayed) and the wire value
  is neither the password nor the stored verifier. The KDF runs via WebCrypto
  when available, otherwise through a built-in pure-JS implementation (so it also
  works over plain HTTP on a LAN IP). Passwords are stored as
  `pbkdf2_sha256$<salt>$<dk>` — see `app/auth.py` and `static/js/crypto.js`.
  A plaintext `password` field is still accepted as an explicit, opt-in fallback.
- **The admin password lives in plaintext in `admin.json`** (by design — it is
  the bootstrap source of truth). At startup it is hashed into the DB, so the
  database itself never holds it. Keep `admin.json` out of version control.
- **`admin.json` defines the bootstrap admin.** At every start
  `bootstrap_admin()` enforces:
  - the account named by `admin.json`'s `username` always exists, is
    `is_admin = 1`, `active = 1`, and its password is re-synced from the file
    (so editing that file is the recovery path if you get locked out);
  - changing the configured `username` **renames** that account instead of
    creating a second one — the rename migrates all of its data (API keys,
    upstreams, usage, tool groups, memberships) in one transaction, so existing
    downstream API keys keep working unchanged;
  - when the configured name does not exist and exactly one admin does, that
    admin is renamed. **Additional admins are allowed** and are never demoted:
    promote them from the admin page (`POST /api/admin/users/{name}/grant-admin`,
    `…/revoke-admin`). The `admin.json` account itself cannot be revoked.
- **Cancelling an account also disposes of its tool groups** (otherwise you get a
  "zombie group" that still runs but nobody can edit or delete). For each group it
  owned:
  1. **another active system admin is a member** → left exactly as is; that admin
     has superuser access over the group and can take it over;
  2. otherwise **an active member exists** → ownership is transferred to the
     **earliest-joined** one (`joined_at` order);
  3. **no usable member** → the group is deleted together with its package folder,
     logs and KV store.

  Reactivating the account restores its login but not groups that were handed over
  or deleted.
- **Group ownership & membership.**
  - the **owner** of a group (and any **system admin**) can edit `__init__.py`,
    upload/delete files, reload, delete the group, remove members and transfer
    ownership; plain members are read-only;
  - anyone can **leave** a group (`POST /api/toolgroups/{gid}/leave`). If the
    *owner* leaves, ownership passes to the earliest-joined active member, or the
    group is deleted when nobody is left;
  - removing a member (or leaving) also **unbinds that user's API keys from the
    group**, and the chat path additionally refuses to inject a group's prompt
    and tools when the key's owner is no longer a member — so removal actually
    revokes access rather than just being cosmetic;
  - admins have a superuser channel over every group (they count as members for
    reading and managing, even without joining).
- `DELETE /api/admin/users/{name}` is a true purge (no transfer): it removes the
  account plus its upstreams, API keys, usage rows, memberships and **all** of
  its tool groups. Neither cancel nor delete may be applied to your own account.
- Tool groups run **user-supplied Python** on the server (same trust model as
  ContextusAgent plugins). Treat group owners as trusted; run in an isolated
  environment otherwise.
- Upstream provider API keys are stored in the local SQLite DB (like
  ContextusAgent's config). Protect the `data/` directory.
- Downstream API keys are stored with a SHA-256 hash **and** in plaintext (the
  plaintext copy is what lets the dashboard show/copy the key again). Protect
  `data/`.
- Tool-call logs deliberately contain only the tool name, status, duration and a
  short detail — never prompts or LLM responses — and expire after 1 hour.
