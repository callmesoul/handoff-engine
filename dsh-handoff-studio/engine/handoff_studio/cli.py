"""`python -m handoff_studio` 入口：起一个只跑 JSON API 的 studio 服务。

用法（Windows）：

    D:\\handoff\\app\\.venv-win\\Scripts\\python.exe -m handoff_studio ^
        --root D:\\handoff\\store-github --port 8791

插件会以这个命令拉起它；手工调试也用它。端口默认 8791：8790 是原面板、
8789 是 frontend-listener、8788 是唤醒监听，都不占。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from handoff.config import Config
from handoff.errors import HandoffError

from .server import StudioServer
from .service import StudioService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handoff_studio", description="Handoff Studio 引擎（JSON API）")
    parser.add_argument("--root", default=None, help="交接仓根目录（默认按当前目录找）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（只允许回环）")
    parser.add_argument("--port", type=int, default=8791, help="监听端口（0 = 由系统分配）")
    parser.add_argument("--once", action="store_true", help="只打印一次状态就退出（自检用）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _find_root()
    try:
        config = Config.load(root)
    except HandoffError as exc:
        print(f"配置加载失败: {exc.message}", file=sys.stderr, flush=True)
        return 2

    try:
        service = StudioService(root, config)
    except HandoffError as exc:
        print(f"Studio 初始化失败: {exc.message}", file=sys.stderr, flush=True)
        return 2

    if args.once:
        state = service.state()
        print(
            json.dumps(
                {
                    "root": str(root),
                    "executors": list(state.get("executor_options") or {}),
                    "studio": service.studio_models(),
                    "tasks": len(service.list_tasks()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    server = StudioServer(service, host=args.host, port=args.port)
    actual_host, actual_port = server.start()
    url = f"http://{actual_host}:{actual_port}"
    print(f"studio 引擎已启动: {url}（root={root}）", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            service.shutdown()
        except AttributeError:
            pass
        server.stop()
    return 0


def _find_root() -> Path:
    """默认从当前目录往上找 config/repositories.yaml。"""
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "repositories.yaml").is_file():
            return candidate
    raise SystemExit("找不到交接仓根目录（config/repositories.yaml）；用 --root 指定")


if __name__ == "__main__":
    raise SystemExit(main())
