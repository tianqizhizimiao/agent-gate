"""入口：python run.py [端口]

端口优先级：命令行参数 > 环境变量 AGENTGATE_PORT > app/config.py 里的 PORT 默认值（8000）。

    python run.py             # 用默认端口
    python run.py 9000        # 换端口
    set AGENTGATE_PORT=9000   # 或先设环境变量（PowerShell: $env:AGENTGATE_PORT=9000）

监听地址默认 0.0.0.0（局域网可访问）；只给本机用就设 AGENTGATE_HOST=127.0.0.1。
"""
import sys

import uvicorn

from app.config import HOST, PORT


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"[AgentGate] http://{HOST}:{port}")
    uvicorn.run("app.main:app", host=HOST, port=port, reload=False)


if __name__ == "__main__":
    main()
