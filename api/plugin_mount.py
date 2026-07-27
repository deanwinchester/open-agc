"""Plugin router/static mounting helpers.

Kept in an import-light module (no plugin discovery / DB side effects) so the
ghost-route pruning behavior can be unit-tested without importing api.server.
"""
import os

# 所有插件的 API 路由与静态文件都挂在这两个前缀之下（router_prefix 默认值；
# 自定义 router_prefix 另由 _prune_plugin_routes 按插件名精确剪除）。
PLUGIN_API_PREFIX = "/api/plugin/"
PLUGIN_STATIC_PREFIX = "/static/plugins/"


def _is_plugin_scoped(path: str) -> bool:
    """True if *path* belongs to any plugin's API routes or static mount."""
    return (
        path == PLUGIN_API_PREFIX[:-1]
        or path.startswith(PLUGIN_API_PREFIX)
        or path == PLUGIN_STATIC_PREFIX[:-1]
        or path.startswith(PLUGIN_STATIC_PREFIX)
    )


def _prune_plugin_routes(app, name: str, prefix: str) -> None:
    """Drop one plugin's previously included API routes and static mount.

    Starlette matches routes in registration order; routes included by an
    earlier scan would shadow the re-included (new-code) router.
    """
    static_path = f"{PLUGIN_STATIC_PREFIX}{name}"

    def _stale(r):
        p = getattr(r, "path", "") or ""
        return p == prefix or p.startswith(prefix + "/") or p == static_path

    app.router.routes[:] = [r for r in app.router.routes if not _stale(r)]


def mount_plugins(app, plugins, logger=print) -> None:
    """(Re)mount plugin routers and static dirs on *app*.

    Ghost-route guard: prune ALL plugin-scoped routes/mounts up front, not just
    those of plugins in *plugins*. A plugin that was deleted, moved to the
    trash, or whose init failed is absent from the new discovery list — without
    the blanket prune its old routes would keep serving stale code ("ghost
    service"). Plugins present in the list are re-included right after.
    """
    app.router.routes[:] = [
        r for r in app.router.routes
        if not _is_plugin_scoped(getattr(r, "path", "") or "")
    ]

    for p in plugins:
        inst = p.instance
        if not inst:
            continue
        prefix = inst.router_prefix or f"{PLUGIN_API_PREFIX}{p.name}"
        # 先剪后加（且只剪一次）：静态分支不得再剪 API 前缀，否则刚 include 的
        # 路由会被紧随的第二次剪除误删。
        _prune_plugin_routes(app, p.name, prefix)
        if inst.router:
            app.include_router(inst.router, prefix=prefix)
            logger(f"[Server] Mounted plugin router: {p.name} -> {prefix}")
        if inst.static_dir and os.path.isdir(inst.static_dir):
            from fastapi.staticfiles import StaticFiles
            app.mount(f"{PLUGIN_STATIC_PREFIX}{p.name}", StaticFiles(directory=inst.static_dir), name=f"plugin_{p.name}_static")
            logger(f"[Server] Mounted plugin static: {p.name}")
