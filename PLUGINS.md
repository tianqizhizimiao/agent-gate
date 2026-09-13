# AgentGate 插件开发指南

> 插件（也叫**工具组**，tool group）是这个网关的核心机制。
> 本文分三部分：**插件有什么用** → **怎么工作的** → **怎么写**。

---

# 一、插件有什么用

## 模型缺三样东西

一个裸的 LLM 只能做一件事：**根据你给的文字，续写出文字**。它有三个硬伤：

| 硬伤 | 表现 |
| --- | --- |
| **拿不到实时信息** | 问它今天天气、现在的股价、你数据库里的订单，它只能编 |
| **不能操作任何东西** | 让它发邮件、写文件、下单，它做不到 |
| **不记得任何事** | 每次请求都是全新的，上一轮聊过什么它不知道（除非你把整个历史塞回去） |

插件就是补这三个洞的：

```
                    ┌─────────────────────────────┐
   模型缺的  ──────▶│  插件（= 一个 Python 包）    │
                    └─────────────────────────────┘
   ① 工具能力   →   @tool 注册函数，模型可以「调用」它
   ② 行为约束   →   prompt(...) 注入系统提示词
   ③ 持久记忆   →   database 每组独立 KV 存储
   ④ 多租户身份 →   name 告诉你现在是谁在调
```

## 什么时候该写插件

| 场景 | 用插件吗 | 怎么做 |
| --- | --- | --- |
| 让模型查公司内部数据（订单、库存、工单） | ✅ | `@tool` 包一层你的内部 API |
| 让模型算准确的数（财务、统计） | ✅ | `@tool` 里用 Python 算，别让模型心算 |
| 让模型记住用户偏好、跨会话上下文 | ✅ | `database` + `name` |
| 给模型灌领域知识、限制它的说话方式 | ✅ | `prompt(...)`，不用写任何工具 |
| 让模型生成随机数/UUID/密码 | ✅ | 模型的"随机"是假的，必须走工具 |
| 单纯想换个模型 | ❌ | 那是「上游 API」的事，不用插件 |
| 只是想调 temperature / 换 system prompt | ❌ | 客户端直接传就行，不必做成插件 |
| 想给模型的输出加个固定前缀 | ❌ | 没必要绕一圈 |

## 一个插件能有多简单

只需要一个 `prompt()` 调用，不需要任何工具：

```python
# 这个插件一行代码，作用是「让模型只说中文，且回答尽量短」
prompt("无论用户用什么语言提问，你都必须用简体中文回答。回答尽量简短，不超过三句话。")
```

把这段保存成 `__init__.py` 上传，这个组里的模型行为就变了。**工具是插件的可选项，不是必需项。**

## 一个插件能有多复杂

上限是「Python 能做的事」：

- 调任意 HTTP API（内部系统、第三方服务）
- 读数据库、读文件、跑计算
- 多轮任务编排（模型可以连续调 8 轮工具）
- 按用户区分的状态机、审批流、计数器
- 每个工具组**完全隔离**：一组一个 SQLite，A 组看不到 B 组的数据

---

# 二、插件怎么工作的

## 全链路

```
  下游客户端（带 sk-xxx 密钥）
        │
        │  POST /v1/chat/completions
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 1. 认证    sk-xxx → 找到密钥记录 → 得到 user_id、group_id │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 2. 取组运行时（GroupManager）                             │
  │    · 该组是否已加载？→ 否则 importlib 导入它的 __init__.py │
  │    · 包的 .py 改动过？→ 自动重新导入（热重载）             │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 3. 注入                                                   │
  │    messages ← 插件 prompt 片段（放到最前面的 system）      │
  │    tools    ← 插件的工具（+ 客户端自带工具，重名时插件赢） │
  │    tool_choice 默认 auto                                  │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 4. 发给上游模型（协议差异由适配器吸收）                    │
  └──────────────────────────────────────────────────────────┘
        │
        │  模型返回：要么是文字，要么是 tool_calls
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 5. 拦截判断（all-or-nothing，见下）                        │
  │    · 这一批工具调用全是插件的？ → 网关自己执行              │
  │    · 只要有一个不是？          → 原样交回客户端，不执行任何 │
  └──────────────────────────────────────────────────────────┘
        │  执行的话
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 6. 执行工具：线程池里跑 handler，30 秒超时，记日志         │
  │    结果以 role="tool" 的消息追加进 messages                │
  └──────────────────────────────────────────────────────────┘
        │
        └──▶ 回到第 4 步，最多循环 8 轮（MAX_STEPS）
```

## 关键机制

### 1) prompt 注入：合并，不是覆盖

`prompt("...")` 只是登记一个片段。真正注入时：

- 片段之间用 `\n\n` 拼接；
- 如果对话**已经有** `system` 消息 → 片段拼在它**前面**（原有内容保留）；
- 如果**没有** → 在最前面插入一条新的 `system` 消息。

所以插件不会把客户端自己的 system prompt 冲掉。

**同一个片段重复调用 `prompt()` 只会登记一次**（内部去重）。

### 2) 工具合并：插件优先

```
最终发给模型的 tools = [插件的全部工具] + [客户端自带的工具，去掉与插件重名的]
```

重名时**插件赢**。`tool_choice` 如果客户端没传（或传了 `none`）而又有工具存在，自动变成 `auto`。

### 3) all-or-nothing 拦截（核心设计）

这是最容易踩坑的地方，单独讲清楚。

一轮回复里，模型可能同时请求调用多个工具。网关的判断是：

```
这一批 tool_calls 里的每一个，都是本插件注册的工具吗？
  ├─ 是一整批 → 网关**全部自己执行**，结果喂回模型，继续下一轮
  └─ 有一个不是 → 网关**一个都不执行**，把整个回复（含 tool_calls）
                  原样交回客户端，由客户端自己处理
```

为什么要这样？**避免"一半网关执行、一半客户端执行"的混乱局面。**

- 网关执行会消耗掉这一轮的响应，客户端再也看不到这些 tool_calls；
- 如果混着来，客户端会收到一个被截断的响应，而它期待的工具调用却不见了。

所以设计上选择了**全有或全无**。

**实践含义**：如果你希望插件行为稳定可预测，就让客户端**不要**传自己的工具，或者让两边的工具名完全不冲突。混用时只要模型在一轮里同时调了两边的工具，插件这轮就不生效。

### 4) 多轮循环：最多 8 轮

模型调工具 → 拿到结果 → 再决定要不要继续调。这个循环**上限 8 轮**（`chat.py` 里的 `MAX_STEPS`）。

超过 8 轮就直接收尾返回。这防止模型陷入"无限调工具"的死循环，也防止一次请求烧掉大量 token。

> 如果你的场景确实需要更多轮（比如长链路的 Agent 任务），改 `app/gateway/chat.py` 的 `MAX_STEPS`。

### 5) 工具执行：线程池 + 30 秒超时

```python
ret = await asyncio.wait_for(
    asyncio.to_thread(run_handler, ctx, handler, tc["arguments"]),
    timeout=30,
)
```

- handler 跑在**线程池**里，所以你在工具里写**同步阻塞代码是允许的**（`requests.get()`、`time.sleep()`、读文件都行），不会卡住整个服务。
- 默认线程池上限 `min(32, CPU+4)`。同一时刻超过这个数的工具调用会排队。
- **30 秒硬超时**。超时会返回给模型一句 `Tool error: timed out after 30s`。
- 返回值：字符串原样返回；其它类型（dict / list / int）自动 `json.dumps`。

### 6) 工具抛异常不会失败整个请求

```python
@tool
def risky() -> str:
    raise ValueError("炸了")
```

模型收到的是 `Tool error: 炸了`，然后**它会基于这句话继续对话**（通常会向用户解释出了问题）。

这是个好设计——工具内部出错不会让用户的整轮对话 500。但也意味着**你必须自己写清楚错误信息**，因为模型看到的就是你抛出的那句话。

### 7) 每个组完全隔离

```
data/toolgroups/tg_xxxxxxxx/
  __init__.py            你写的插件代码
  helpers.py             你的辅助模块
  .agentgate/
    data.db              这个组独立的 KV 存储（database）
    logs.db              这个组的工具调用日志
```

A 组的 `database` 和 B 组毫无关系，即使两个组的插件代码一模一样、用同样的 key。

### 8) 热重载

网关记录包目录下所有 `*.py` 的**最大修改时间**。每次要用这个组时对比一下，变了就重新导入整个包。

所以：**改完代码不用重启服务**。页面上点「保存并重载」，或者直接改磁盘上的文件都行。

> 只有 `.py` 会触发热重载。改 `.json`、`.txt` 之类的数据文件不会（但下次调用时读到的是新内容）。

### 9) 什么样的请求会「退化为直连」

以下情况插件**不生效**，请求原样转发给上游：

| 情况 | 原因 |
| --- | --- |
| 密钥没绑定工具组 | 这个密钥本来就没插件 |
| 密钥属主已被移出该组 | 故意的：否则「移除成员」只是形式 |
| 插件包加载失败（语法错误等） | 让用户至少还能拿到回复；错误信息在「保存并重载」那里能看到 |

---

# 三、怎么写插件

## 3.1 最小可运行插件

上传一个 `__init__.py` 就够了：

```python
prompt("你现在是一位天气助手。用户问天气时，调用 get_weather 工具，不要凭记忆回答。")


@tool
def get_weather(city: str) -> str:
    """查询某个城市的天气。
    :param city: 城市名，例如「北京」
    """
    # 真实场景这里应该调天气 API
    return f"{city}：晴，24℃"
```

**上面这段代码里没有 `import`，也没有定义 `tool` 和 `prompt`** —— 它们是网关在导入你的包**之前**注入到模块命名空间里的。

## 3.2 四个注入的全局名字

| 名字 | 类型 | 作用 |
| --- | --- | --- |
| `prompt(text)` | 函数 | 登记一段系统提示词片段 |
| `@tool` | 装饰器 | 把函数注册成模型可调用的工具 |
| `database` | 对象 | 本组独立的 KV 存储（`db` 是它的旧别名，仍可用） |
| `name` | 代理对象 | 当前调用者的用户名，用 `str(name)` 取值 |

### `prompt(text)`

```python
prompt("你是客服助手，语气友好。")
prompt("金额一律用人民币，保留两位小数。")     # 可以多次调用，会拼接
```

### `@tool`

```python
@tool
def my_tool(a: str) -> str:
    """一句话描述这个工具做什么。      ← 会变成工具描述
    :param a: 参数 a 是干什么的        ← 会变成参数的 description
    """
    return "结果"
```

也支持显式覆盖名字和描述：

```python
@tool(name="search_orders", description="按订单号查询订单详情")
def _impl(order_id: str) -> dict:
    ...
```

> 工具名重复时**后注册的覆盖先注册的**，不会报错。注意别手滑。

### `database`

按组隔离的持久化 KV，值以 JSON 存储。**list / dict 返回"写穿代理"**：改一下就立刻落盘，支持任意深度。

```python
# 标量
database["count"] = 0
database["count"] += 1              # 读出来 +1 再写回
n = database["count"]               # 不存在会抛 KeyError
n = database.get("count", 0)        # 不存在给默认值

# 列表：改一下立刻落盘
database["history"] = []
database["history"].append("x")     # ✅ 立即写回
len(database["history"])            # 2

# 字典 + 任意深度
database["tree"] = {"a": {"b": []}}
database["tree"]["a"]["b"].append(1)    # ✅ 深层修改也能落盘
database["tree"]["a"]["c"] = 2          # ✅ 深层新增键

# 常用方法
database.setdefault("list", []).append("x")   # 不存在就建，返回活动视图
database.delete("key")
del database["key"]
"key" in database
database.keys()                     # 所有键
database.all()                      # 全部键值（普通 dict）
```

**并发安全**：工具跑在线程池里，并发是真实的。代理**不缓存值**——每次读或改都在锁内重新读取该 key 的最新内容再写回，所以这种写法在多线程下**不会丢数据**：

```python
database["list"].append(x)          # ✅ 安全，即使 100 个并发
```

**什么时候需要显式加锁**：多个语句组成的"读-改-写"。

```python
# ❌ 危险：两步之间可能被别的线程插进来
v = database["n"]
database["n"] = v + 1

# ✅ 方法一：锁住整段
with database.lock():
    database["n"] = database["n"] + 1

# ✅ 方法二：原子封装（更短，推荐）
database.modify("n", lambda v: (v or 0) + 1)
```

**性能注意**：每次修改都会**重写整个 key**。往一个几 MB 的列表里高频 append 会很慢，那种场景改用多个 key 或换自己的存储。

> 存进去的 `set` / `tuple` 会被转成 list，取出来就是 list。

### `name`

```python
@tool
def whoami() -> str:
    """告诉用户他是谁。"""
    return f"你是 {name}"
```

`name` 是个**动态代理**：每次字符串化时取当前上下文里的调用者。所以：

- `str(name)` ✅
- `f"{name}"` ✅（等价于 `str()`）
- `name == "alice"` ✅（重载了 `==`）
- `database[str(name)] = ...` ✅ 按用户分区

> ⚠️ 别把它当普通字符串存起来复用。要用的时候再 `str(name)`，否则可能拿到上一次调用的值。

## 3.3 类型注解 → JSON Schema

网关用**类型注解 + docstring**自动生成工具的参数 schema，你不用手写 JSON。

| Python 写法 | 生成的 schema | 说明 |
| --- | --- | --- |
| `str` | `{"type": "string"}` | |
| `int` | `{"type": "integer"}` | |
| `float` | `{"type": "number"}` | |
| `bool` | `{"type": "boolean"}` | |
| `list[str]` | `{"type": "array", "items": {"type": "string"}}` | |
| `list[int]` | 同上，items 是 integer | |
| `dict` | `{"type": "object"}` | 不展开内部结构 |
| `Optional[int]` | `{"type": "integer"}` | 等价于 `int \| None` |
| 不写注解 | `{"type": "string"}` | 不推荐 |
| **其它任何类型** | `{"type": "string"}` | ⚠️ 见下面的坑 |

**参数的必填性**：没有默认值的 → `required`；有默认值的 → 可选。

```python
@tool
def search(keyword: str, limit: int = 10) -> str:
    """搜索。
    :param keyword: 关键词
    :param limit: 返回条数上限
    """
    ...
# → required: ["keyword"]，limit 可选
```

**几个坑**：

```python
@tool
def bad(tags: tuple[str, ...]) -> str: ...      # ⚠️ tuple 会被当成 string
@tool
def bad2(x: Literal["a", "b"]) -> str: ...      # ⚠️ Literal 也会被当成 string
@tool
def bad3(x: MyClass) -> str: ...                # ⚠️ 自定义类同样

# ✅ 改成这样：
@tool
def good(tags: list[str]) -> str: ...
@tool
def good2(x: str) -> str:
    """...
    :param x: 取值必须是 "a" 或 "b" 之一     ← 用文字描述约束
    """
```

## 3.4 返回值怎么写

| 返回类型 | 模型收到 |
| --- | --- |
| `str` | 原样 |
| `dict` / `list` | 自动 `json.dumps` |
| `int` / `float` / `bool` | 自动转成 JSON 字符串 |
| `None` | `null` |

**建议**：返回**信息密度高、模型好读**的文本，而不是一大坨 JSON。比如：

```python
# ❌ 模型要自己解析
return {"code": 0, "data": {"list": [{"n": "A", "v": 1}]}}

# ✅ 直接可读
return "共 1 条：\n- A：1"
```

数据量大时务必**截断**，否则会撑爆上下文。

## 3.5 多文件组织

工具组是一个 Python **包**，可以有任意多个文件：

```
myplugin/                 ← 上传时选这个文件夹
  __init__.py             ← 只有这个会被自动执行
  helpers.py
  lib/
    api.py
```

`__init__.py` 里用**相对导入**引用它们：

```python
from . import helpers
from .lib import api
from .helpers import format_row
```

> 这就是为什么 AgentGate 和 ContextusAgent 不一样：后者会把插件目录里**每个** `.py` 都当成独立插件执行；AgentGate 只执行 `__init__.py`，其余模块由你显式导入。这样你可以自由地拆分代码，而不用把临时工具函数也变成插件。

**上传时**：用「选择文件 / 文件夹 ▾」→「选择整个文件夹」，子目录结构会原样保留，`__pycache__`、`.git`、`.venv`、`.DS_Store` 会自动跳过。限制：单次少于 30 个文件、目录最深 3 层、单文件 ≤ 30MB。

## 3.6 调试

| 手段 | 怎么做 |
| --- | --- |
| **看工具列表** | 页面「工具」表格：确认工具名、描述、参数 schema 是否符合预期 |
| **看调用日志** | 页面「工具调用日志」：每次调用的工具名、状态（normal/exception）、耗时、返回内容（截断到 2000 字符）。**只保留最近 1 小时** |
| **看加载错误** | 「保存并重载」那里会显示导入失败的完整 traceback |
| **验证返回值** | 日志里的「详情」列就是工具返回给模型的原文 |
| **端口测试** | 不用真的叫模型，直接用 `curl` 打 `/v1/chat/completions`，在 prompt 里明确要求它调某个工具 |

**最有效的调试方式**：在工具里直接 `print()` 是看不到的（输出在服务端控制台）。要留痕就用 `database`：

```python
@tool
def debug_me(x: str) -> str:
    """调试用。"""
    log = database.setdefault("_debug", [])
    log.append({"x": x})
    return "ok"
```

然后写个查看工具，或者直接看 `data.db`。

## 3.7 完整示例：一个待办事项插件

演示 `prompt` + `@tool` + `database` + `name` + 多文件。

分工原则：**`__init__.py` 负责和网关打交道（注入的名字、工具注册、存储访问），
辅助模块只写纯函数**（参数进、结果出，不依赖任何注入的名字）。这样辅助模块
可以被单独 import 出来做单元测试。

**`__init__.py`**

```python
"""待办事项 —— 每个用户一份独立的清单。"""
from . import store

prompt(
    "你可以帮用户管理待办事项。"
    "用户说「记一下 / 待办 / 提醒我」时调用 todo_add；"
    "问「我还有什么没做」时调用 todo_list；"
    "说「做完了」时调用 todo_done。"
    "每个用户的清单是独立的，你不需要也不应该问用户是谁。"
)


def _mine() -> list:
    """取当前用户的清单，不存在就创建。

    返回的是**活动视图**，直接改它就会落盘（见 3.2 的 database 一节）。
    """
    data = database.setdefault("todos", {})
    return data.setdefault(str(name), [])


@tool
def todo_add(text: str) -> str:
    """给当前用户添加一条待办事项。
    :param text: 待办内容
    """
    return store.add_item(_mine(), text)


@tool
def todo_list() -> str:
    """列出当前用户所有未完成的待办事项（含编号）。"""
    return store.render(_mine())


@tool
def todo_done(index: int) -> str:
    """把某条待办标记为已完成。
    :param index: 待办序号，从 1 开始（用 todo_list 看到的序号）
    """
    return store.finish_item(_mine(), index)


@tool
def todo_clear() -> str:
    """清空当前用户的所有待办事项（已完成和未完成的都清掉）。"""
    data = database.setdefault("todos", {})
    n = len(data.get(str(name)) or [])
    data[str(name)] = []
    return f"已清空 {n} 条待办。"
```

**`store.py`** —— 纯逻辑，不碰 `database` / `name`

```python
"""待办的纯逻辑。参数进、字符串出，不依赖任何注入的名字。"""


def add_item(items: list, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "待办内容不能为空。"
    items.append({"text": text, "done": False})
    return f"已记下第 {len(items)} 条：{text}"


def finish_item(items: list, index: int) -> str:
    if not (1 <= index <= len(items)):
        return f"序号 {index} 不存在，当前共 {len(items)} 条。"
    items[index - 1]["done"] = True          # 深层修改也会落盘
    return f"已完成：{items[index - 1]['text']}"


def render(items: list) -> str:
    """列出未完成的条目。

    编号故意用**原始位置**（enumerate 的下标 + 1），不因为前面的条目完成而重排 ——
    否则 todo_done 的序号会指向错的条目。用方括号包起来是想让模型和用户都明白
    这是个稳定编号，不是"第几条"。
    """
    todo = [(i + 1, it) for i, it in enumerate(items) if not it.get("done")]
    if not todo:
        return "你目前没有未完成的待办事项。"
    lines = [f"你有 {len(todo)} 条未完成（方括号里是编号，完成时用这个编号）："]
    for n, it in todo:
        lines.append(f"  [{n}] {it['text']}")
    return "\\n".join(lines)
```

**关于编号**：`render` 用的是**原始位置**（第几条加进来的），不是"当前第几条"。
所以完成一条之后，剩下条目的编号**不会往前挪** —— 这样 `todo_done(2)` 永远指向同一条，
不会因为前面的条目被完成而打错目标。展示成 `[2] 写文档` 而不是 `2. 写文档`，
就是为了让它看起来像 ID 而不是序号。

### ⚠️ 一个必须知道的坑：子模块看不到注入的名字

`prompt` / `tool` / `database` / `name` 只被注入到 **`__init__.py` 自己的模块命名空间**。
子模块是**独立的模块**，它里面写裸的 `database` 会直接 `NameError`：

```python
# helper.py
def read():
    return database.get("x")     # ❌ NameError: name 'database' is not defined
```

> 这条是实测确认的，不是推测。原因是加载器把注入的名字放进了 `__init__` 那个 module
> 对象的 `__dict__`，而 `helper` 有自己的 `__dict__`。

如果确实需要让子模块访问存储，**显式传进去**：

```python
# __init__.py
from . import helper
helper.database = database      # 或者调用 helper.init(database)
helper.name = name
```

```python
# helper.py
database = None
name = None

def read():
    return database.get("x")     # ✅ 现在能用了
```

但更推荐本文示例的写法：**把存储访问收在 `__init__.py`，子模块只接收数据、返回结果**。
这样 `store.py` 可以 `import` 出来直接写单测，不需要造一个假的 `database`。

## 3.8 从零写一个插件的完整流程

1. **建一个文件夹**（名字随意，上传后会被改名为 `tg_xxxx`）
2. **写 `__init__.py`**：需要的话加 `prompt(...)`，然后 `@tool` 注册函数
3. **本地先测**：把函数当普通 Python 函数调一遍，确认逻辑对
4. **页面上传**：进入工具组页面 →「选择文件 / 文件夹 ▾」→ 选整个文件夹
5. **点「保存并重载」**：看有没有 traceback
6. **看「工具」表格**：确认工具名、描述、参数 schema 都对
7. **拿下游密钥测**：在客户端里问一个会触发工具的问题
8. **看「工具调用日志」**：确认真的被调用了、状态是 normal、返回内容符合预期

---

# 四、限制与注意事项

## 安全

> ⚠️ **插件代码在网关进程里以网关的权限执行。**

- 插件作者**等于**能在这台机器上跑任意 Python 代码：读你的文件、连你的内网、读环境变量。
- 因此：**只让信任的人成为工具组的成员**，尤其是**拥有者**（拥有者能编辑 `__init__.py`）。
- 工具组有「属主」和「成员」的概念。成员能用这个组的工具，但不一定能改代码——具体看页面上的权限提示（只有属主和管理员能上传/编辑文件）。
- 每个组的数据是隔离的，但**代码不隔离**：任意一个组的插件都能访问整个文件系统。

## 资源限制

| 限制 | 值 | 在哪改 |
| --- | --- | --- |
| 单次请求最多循环轮数 | 8 | `app/gateway/chat.py` 的 `MAX_STEPS` |
| 单个工具执行超时 | 30 秒 | `app/config.py` 的 `TOOL_CALL_TIMEOUT_SECONDS` |
| 工具日志保留 | 1 小时 | `app/config.py` 的 `LOG_RETENTION_SECONDS` |
| 工具并发 | `min(32, CPU+4)` | Python 默认线程池 |
| 单次上传文件数 | 少于 30 | `app/config.py` 的 `MAX_UPLOAD_FILES` |
| 目录深度 | 3 层 | `MAX_UPLOAD_DEPTH` |
| 单文件大小 | 30 MB | `MAX_UPLOAD_BYTES` |

## 性能建议

- **工具里别做慢查询**：30 秒超时是硬限制，超时对用户体验很差。
- **database 每次修改重写整个 key**：别往里塞大数组然后高频 append。
- **返回给模型的内容要截断**：工具返回 10 万字，下一轮请求的 token 会直接爆掉。
- **同步阻塞代码可以写**，但会占用线程池的一个槽位；并发高的时候注意。

## 常见坑速查

| 症状 | 原因 |
| --- | --- |
| 按钮/工具没反应，模型说"没有这个工具" | 包加载失败（语法错误），去「保存并重载」看 traceback |
| 工具被调用了但报 `Tool error: ...` | 工具内部抛异常了，看日志「详情」列 |
| 模型不调工具，自己编答案 | `prompt` 里没明确要求它调用，或工具描述太模糊 |
| 参数类型不对 | 用了 `tuple` / `Literal` / 自定义类，被当成 string 了 |
| 改了代码没生效 | 只有 `.py` 会触发热重载；或者文件没真正传上去 |
| `KeyError` | 用了 `database["x"]` 但键不存在，改用 `database.get("x", 默认值)` |
| 并发下数据丢了 | 跨语句的读-改-写没用 `lock()` / `modify()` |
| 子模块里用 `database` 报 `NameError` | 注入只在 `__init__.py` 里，见 3.7 |
| 插件这轮没生效 | 客户端自带的工具和服务端工具在同一轮被混着调用 → all-or-nothing 退化为直连 |
