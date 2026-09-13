# AgentGate

一个多租户 LLM 网关，建立在 **ContextusAgent** 的「下游密钥鉴权 + 上游透明代理」「**提示词注入**」与
「**全有或全无的工具拦截**」原理之上，并在其上扩展了用户账户、**账户级上游 API（每个用户可配多个，
对下游合并暴露）**、token 用量统计，以及相互隔离的工具组 —— 每个工具组是一个独立 Python 包，
拥有自己的数据库和 1 小时工具调用日志。

```
客户端（OpenAI SDK） --Bearer sk-...--> AgentGate /v1/chat/completions
                                             |
                          解析密钥 -> 用户 -> 按 `model` 选择上游
                                             |     （所有上游模型的并集）
                          绑定了工具组？ --+-- 否 -> 直接透传到上游
                                             +-- 是 -> 注入提示词 + 工具 schema
                                        调用上游 -> 拦截 tool_calls
                                        若全是服务端工具：执行并循环
                                        若有客户端工具：整包透传
                                             |
                          记录 token 用量（按用户 / 密钥 / 工具组）
```

## 功能

- **账户** —— 用管理员签发的*一次性令牌*注册；用户名即唯一 ID。登录基于 JWT。管理员可查看 /
  注销 / 恢复。
- **上游 API（账户级通道）** —— 用户可配置**多个**上游 API，每个都有自己的 base URL / 密钥 /
  模型列表。所有已选模型会被**合并**，并通过 `GET /v1/models` 暴露给下游；请求里的 `model`
  会被路由到声明了它的那个上游。
- **多协议上游** —— 网关对上游可以说 **OpenAI 兼容、Anthropic Messages、Google Gemini 与
  DashScope 原生**四种协议，对下游则始终暴露 OpenAI 格式。协议通过**实际探测**你填的地址自动
  识别（用三种鉴权方式请求 `/models`，并对响应体结构分类 —— 成功形态*或*错误形态都算线索），
  也可以手动指定。见 [上游协议](#上游协议)。
- **API 密钥（下游令牌）** —— `sk-...` 密钥，每个最多绑定一个工具组。密钥串在创建时可以
  **自定义**（`sk-<你的前缀>-<4 位随机>`，默认 `sk-<48 位随机>`），并且可以随时在面板
  **再次复制**。每个密钥都能看到该账户下的全部上游模型。
- **Token 用量** —— 按请求记录 prompt / completion / total token，可按模型与日期在面板查看。
- **工具组** —— 工具组是一个 Python 包目录（`__init__.py` + 上传的辅助文件），有组 ID 供他人
  **加入**。一个组可以容纳多个用户的 API 密钥，但**一个 API 密钥最多绑定一个组**。
- **注入 + 拦截** —— 绑定了组的密钥会被注入该组的提示词，其工具也会与客户端的工具合并（同名时
  服务端工具优先）。若模型**只**调用服务端工具，网关执行它们并循环（≤ 8 轮）；只要有一次调用是
  客户端工具，整个响应就透传。流式与非流式都支持。
- **组间隔离** —— 每个组一份 SQLite KV 存储 + 一份工具调用日志库（位于该组的 `.agentgate/`
  目录下）。
- **1 小时日志** —— 每个组**只**记录工具调用（正常 + 异常）；超过 1 小时的条目会被丢弃，别的
  什么也不记。
- **管理页** —— 生成 / 吊销一次性注册令牌；管理账户；升降管理员。**注销** 会停用账户（登录、
  JWT 与全部下游 API 密钥立即失效），并把它名下的工具组交给最早加入的活跃成员；**彻底删除**
  是真正的清除，会连同这些工具组一并删除。见 [注意事项与安全](#注意事项与安全)。

## 快速开始

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt   # fastapi, uvicorn, httpx, jinja2, python-multipart, pyjwt
python run.py                     # 或： python main.py   -> http://127.0.0.1:8000
```

### 改端口 / 监听地址

不用改代码，三种方式（优先级从高到低）：

```bash
python run.py 9000                # 1) 命令行参数
set AGENTGATE_PORT=9000           # 2) 环境变量（PowerShell: $env:AGENTGATE_PORT=9000）
                                  # 3) 直接改 app/config.py 里的 PORT 默认值
```

默认监听 `0.0.0.0`（局域网可访问）。只想给本机用：

```bash
set AGENTGATE_HOST=127.0.0.1
python run.py
```

### 管理员登录

管理员角色是硬编码的，始终存在。凭据按以下顺序读取：

1. 项目根目录下的 `admin.json` —— `{"username":"admin","password":"..."}`
2. 环境变量 `AGENTGATE_ADMIN_USER` / `AGENTGATE_ADMIN_PASS`
3. `data/secret.json` 里的随机一次性令牌（首次运行会打印到控制台）

仓库里**包含**一份可直接用的 `admin.json`（`admin` / `admin`），克隆下来就能登录。
对外暴露前**务必改掉**它 —— 改完重启即可生效，不需要重建数据库。

```bash
# 想自己指定，也可以从模板复制一份再改
cp admin.json.example admin.json
```

以管理员登录 → **管理员** → 生成注册令牌 → 交给新用户。

## 使用网关

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
    messages=[{"role": "user", "content": "东京今天天气怎么样？"}],
)
print(resp.choices[0].message.content)
```

- **没有绑定工具组** → 透明透传到路由到的那个上游。
- **绑定了工具组** → 注入提示词与工具，工具调用被拦截并在服务端执行（agent 循环）。

## 上游协议

网关对下游始终说 **OpenAI 格式**（`/v1/chat/completions`、`/v1/models`）。对上游支持四种协议，
并双向翻译 —— 请求、响应、流式分片、工具调用与用量。

| `protocol` | 上游协议 | 使用的端点 | 鉴权 |
|---|---|---|---|
| `openai` | OpenAI 兼容 | `{base}/chat/completions` | `Authorization: Bearer` |
| `anthropic` | Anthropic Messages | `{base}/messages` | `x-api-key` + `anthropic-version` |
| `gemini` | Google Gemini | `{base}/models/{model}:generateContent`（流式时 `?alt=sse`） | `x-goog-api-key` |
| `dashscope` | 阿里 DashScope 原生 | `{base}/services/aigc/text-generation/generation` | `Authorization: Bearer` + `X-DashScope-SSE` |

`openai` 的覆盖面最广：DeepSeek、通义千问（兼容模式）、Moonshot/Kimi、智谱 GLM、MiniMax、
SiliconFlow、Ollama、vLLM、LM Studio、one-api/new-api 与 OpenRouter 都讲它 —— 无需翻译。

### 协议是怎么探测出来的

在上游表单里点 **探测协议并获取模型**。没有任何按厂商硬编码的规则；网关**探测你填的地址**：

1. 对每个候选 base（`{base}` 与 `{base}/v1`），用上面三种鉴权方式各请求三次 `/models`。
2. 它对**响应体结构**分类 —— 成功的列表*或错误体*都算线索，因为各厂商的错误形态很有辨识度：

   | 协议 | 成功形态 | 错误形态 |
   |---|---|---|
   | OpenAI | `{"object":"list","data":[{"id","object":"model"}]}` | `{"error":{"message","type"}}` |
   | Anthropic | `{"data":[{"id","type":"model"}]}` | `{"type":"error","error":{...}}` |
   | Gemini | `{"models":[{"name":"models/..."}]}` | `{"error":{"code","status","message"}}` |
   | DashScope | `{"output":{"choices":[...]}}` | `{"code":"...","message":"...","request_id":"..."}` |

3. 然后用**匹配到该协议**的鉴权方式重新请求 `/models`，取回模型列表并确认密钥可用。
4. DashScope 原生没有 `/models`，所以它会额外探测生成端点（空 body 会返回 `code`/`message`
   错误，这个结果是确定的）。

如果形态有歧义，可以在表单里手动选协议。结果存进 `upstreams.protocol`，用于路由每个请求。

## 编写工具组

工具组的目录就是一个 Python 包。它的 `__init__.py` 在加载时执行，并带有以下**注入的全局名字**
（无需 import）：

| 名字 | 用途 |
|------|---------|
| `prompt(text)` | 追加一段系统提示词片段，注入到每次对话 |
| `@tool` | 把一个函数注册为可调用工具；它的 JSON Schema 由类型标注 + docstring（`:param x: ...`）自动推导 |
| `database` | 组内持久化键值存储：`database["k"] = v`、`database.get("k")`、`database.keys()` |
| `name` | 当前调用者的用户名 —— 用 `str(name)` |

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
prompt("任何城市的天气都可以调用 get_weather 查询。")

@tool
def get_weather(city: str) -> str:
    """返回某个城市的天气。
    :param city: 城市名
    """
    database["last_city"] = city    # 存进本组独立的数据库
    return f"{city} 晴（由 {name} 询问）"
```

上传到该目录的辅助 `.py` 文件可以用相对导入引用，例如 `from . import helpers`。
只有 `__init__.py` 会被自动执行（整个目录作为**一个**包导入，不像 ContextusAgent
那样逐文件执行）。在界面里点 **保存并重载** 即可热重载。

### 上传：可以整个文件夹一起传

界面上的 **上传整个文件夹** 用的是浏览器的 `webkitdirectory`，会保留子目录结构
（选中的那一层文件夹名会被剥掉 —— 工具组目录本身就是根）。`__pycache__`、`.git`、
`.venv`、`.DS_Store` 等会被自动跳过。

**防爆限制**（都在 `app/config.py`，改完重启生效）：

| 限制 | 值 | 常量 |
| --- | --- | --- |
| 单次文件数 | 必须 **小于 30**（即最多 29） | `MAX_UPLOAD_FILES` |
| 目录深度 | 最深 **3 层** | `MAX_UPLOAD_DEPTH` |
| 单个文件 | ≤ **30 MB** | `MAX_UPLOAD_BYTES` |
| 整批合计 | ≤ **100 MB** | `MAX_TOTAL_UPLOAD_BYTES` |

约束分三道，从粗到细：

1. **中间件**先看 `Content-Length`，超过合计上限直接 413 —— 免得几百 MB 的 body
   被完整收下来写进临时文件，才轮到接口里的校验（`app/main.py`）。
2. **整批校验**：数量、深度、单文件大小、重名、路径穿越（`..`）全部通过**才开始落盘**。
   一批里只要有一个不合法，一个字节都不会写 —— 不会留下残缺文件。
3. **前端**同样先挡一道，报错能直接指出是哪个文件超了。

前端的限制是从 `GET /api/toolgroups/{id}/files/limits` 取的，不写死常量，避免
和 `config.py` 改得对不上。

> 目录层级 = 路径里的斜杠个数（不含文件名）：`lib/util.py` 是 1 层，
> `a/b/c/d.py` 是 3 层（卡在上限），`a/b/c/d/e.py` 就是 4 层，会被拒。
>
> 文件列表接口是**递归**的（`name` 是相对路径，如 `lib/util.py`），删除按钮也支持
> 嵌套路径 —— 否则上传了文件夹，子目录里的文件在页面上既看不见也删不掉。

## 目录结构

```
app/
  config.py            数据目录、JWT、管理员凭据、各种开关
  database.py          全局 SQLite（users, tokens, upstreams, api_keys, tool_groups, members, usage）
  upstreams.py         账户级上游：增删改查、模型并集、按 model 路由
  auth.py / security.py  pbkdf2 口令、JWT、API 密钥生成与查找
  models.py            pydantic schema
  main.py              FastAPI app 工厂 + 启动引导
  routers/             auth, upstreams, apikeys, usage, toolgroups, admin, pages
  proxy/router.py      /v1/chat/completions, /v1/models（并集）
  gateway/             ContextusAgent 风格的引擎：
      storage.py       组内 KV（PluginStorage）
      prompt.py        PromptInjector
      tools.py         ToolInterceptor（schema + 注入 + 提取）
      plugin_api.py    prompt/@tool/db/name + 按上下文绑定
      plugin_manager.py  包加载器 + 热重载
      logger.py        组内 1 小时工具调用日志
      provider.py      上游客户端
      group_manager.py GroupRuntime 缓存
      chat.py          注入 -> 上游 -> 拦截 -> 执行 -> 循环
templates/  static/    UI
tools/      check_routes.py    前/后端路径一致性检查
            check_encoding.py  源码乱码/编码检查
data/       toolgroups/<folder>/   运行期状态 + 包（运行后生成）
```

### 改完代码建议跑一遍

```bash
python tools/check_routes.py     # 前端 API(...) 调用的路径是否都有后端路由（方法+形状比对）
python tools/check_encoding.py   # 所有源码是否被按错误编码读写（私用区字符 / U+FFFD / 乱码汉字）
```

> ⚠️ 在本机用 PowerShell 改这些文件时**务必指定 UTF-8**。Windows PowerShell 5.1 的
> `Get-Content -Raw` 默认按 ANSI(GBK) 解码，再配合 `Set-Content`/`WriteAllText` 写回
> 会把中文**不可逆**地写成乱码。用编辑器改，或显式 `-Encoding utf8`。

## 注意事项与安全

- **登录从不发送口令。** 浏览器先取一次性挑战（`POST /api/auth/challenge` → `nonce` + `salt`），
  在**本地**推导 `verifier = PBKDF2-HMAC-SHA256(password, salt, 200000)`，再提交
  `proof = SHA256(nonce ‖ verifier)`。明文永不离开浏览器，nonce 一次性使用（所以抓到的 proof
  无法重放），线路上传的既不是口令也不是存储的 verifier。有 WebCrypto 时用 WebCrypto 跑 KDF，
  否则走内置的纯 JS 实现（所以在局域网 HTTP 下也能用）。口令以
  `pbkdf2_sha256$<salt>$<dk>` 存储 —— 见 `app/auth.py` 与 `static/js/crypto.js`。
  仍然接受明文 `password` 字段，作为显式、可选的回退路径。
- **管理员口令以明文存在于 `admin.json`**（这是设计使然 —— 它是引导用的唯一事实来源）。启动时
  它会被哈希进数据库，所以数据库本身不留明文。`admin.json` **已纳入版本控制**，因此它的默认值
  （`admin` / `admin`）是公开的 —— 对外暴露前请改掉，或用 `AGENTGATE_ADMIN_USER` /
  `AGENTGATE_ADMIN_PASS` 覆盖。
- **`admin.json` 决定了引导管理员。** 每次启动 `bootstrap_admin()` 都会强制：
  - `admin.json` 里 `username` 指定的账户始终存在、`is_admin = 1`、`active = 1`，且口令会从
    文件重新同步（所以改这个文件就是被锁在门外时的找回入口）；
  - 改配置里的 `username` 会**改名**该账户，而不是新建第二个 —— 改名会在一个事务里迁移它名下的
    全部数据（API 密钥、上游、用量、工具组、成员关系），因此已有的下游 API 密钥不受影响；
  - 当配置的名字不存在、且当前恰好只有一个管理员时，那个管理员会被改名。**允许存在多个管理员**，
    且永不被降级：在管理页提升即可（`POST /api/admin/users/{name}/grant-admin`、
    `…/revoke-admin`）。`admin.json` 指定的账户本身无法被撤销管理员。
- **注销账户会一并处置它名下的工具组**（否则会留下「僵尸组」：仍在运行，但没人能编辑或删除）。
  对每个它拥有的组：
  1. **有另一个活跃的系统管理员是成员** → 原样保留；该管理员对该组有超级权限，可以接手；
  2. 否则**存在活跃成员** → 所有权转给**最早加入**的那个（按 `joined_at` 排序）；
  3. **没有可用成员** → 该组连同它的包目录、日志与 KV 存储一起删除。

  恢复账户可以恢复登录，但已经交出去或已删除的组不会回来。
- **组的拥有权与成员关系。**
  - 组的**拥有者**（以及任何**系统管理员**）可以编辑 `__init__.py`、上传/删除文件、重载、
    删除组、移除成员与转让所有权；普通成员只读；
  - 任何人都可以**退出**组（`POST /api/toolgroups/{gid}/leave`）。如果*拥有者*退出，所有权转给
    最早加入的活跃成员；如果一个人都不剩，组会被删除；
  - 移除成员（或退出）也会**把这些用户绑定在该组上的 API 密钥解绑**，并且聊天路径会额外拒绝
    为「密钥拥有者已不是成员」的情况注入该组的提示词和工具 —— 所以移除是真的收回权限，而不只是
    表面功夫；
  - 管理员对每个组都有超级通道（即使没加入，也算成员，可读可管）。
- `DELETE /api/admin/users/{name}` 是真正的清除（不做转让）：删除账户及其上游、API 密钥、
  用量记录、成员关系，以及它名下的**全部**工具组。注销与删除都不能施加于你自己的账户。
- 工具组会在服务端运行**用户提供的 Python**（与 ContextusAgent 插件同一信任模型）。请把组拥有者
  视为可信；否则请放在隔离环境中运行。
- 上游厂商的 API 密钥存放在本地 SQLite 库中（类似 ContextusAgent 的 config）。请保护好 `data/`
  目录。
- 下游 API 密钥同时以 SHA-256 哈希**和**明文存储（明文副本正是面板能再次显示/复制密钥的原因）。
  请保护好 `data/`。
- 工具调用日志刻意只包含工具名、状态、耗时与一小段细节 —— 绝不包含提示词或 LLM 回复 ——
  并在 1 小时后过期。
