"""界面资源。

界面是 `web/` 下的 Vue 工程，`pnpm build` 的产物落在这里的 `static/`。Python 这边
只负责把 index.html 端出去。

原来这里是手写的原生 JS，理由是「内部工具不值得装一套 npm」。后来页面涨到总览、
单店下钻、交付、店铺、提成、接表向导六个视图，还要加平台/店铺/账期三档筛选和
可翻页的下钻——手写那套里每个视图各自拼字符串、各自记筛选状态，改一处漏三处。
换框架的代价是一次性的，不换的代价按视图数量增长。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parent / "static"


def is_asset_path(path: str) -> bool:
    return path.replace("\\", "/").startswith("assets/")


class HashedStaticFiles(StaticFiles):
    """Vite哈希资源长期缓存；index.html仍由首页接口明确no-store。"""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        # StaticFiles传进来的是平台规范化后的路径；Windows上分隔符是反斜杠。
        if response.status_code == 200 and is_asset_path(path):
            response.headers["Cache-Control"] = "public,max-age=31536000,immutable"
        return response


def built() -> bool:
    """前端构建过没有。

    没构建时 static/ 是空的，`/` 会 500，而错误信息里只有一个 FileNotFoundError——
    对着它没人猜得到要去 web/ 下跑 pnpm build。
    """
    return (STATIC / "index.html").exists()


def page() -> str:
    """首页 HTML。每次读盘，构建完刷新就见效，不用重启服务。"""
    if not built():
        return (
            "<!doctype html><meta charset=utf-8>"
            "<body style='font:15px/1.6 system-ui;padding:48px;max-width:640px'>"
            "<h1>界面还没构建</h1>"
            "<p>在 <code>ledger/web</code> 下跑一次：</p>"
            "<pre style='background:#f7f8fa;padding:12px;border-radius:8px'>"
            "pnpm install\npnpm build</pre>"
            "<p>产物会落到 <code>ledger/ledger/static/</code>，刷新这一页就好了。</p>"
        )
    # Vite文件名已经带内容哈希。再追加查询参数会让异步分片以无参数URL反向导入
    # 主chunk，浏览器把两者当成不同ES module，导致主应用和启动API各执行两次。
    return (STATIC / "index.html").read_text(encoding="utf-8")
