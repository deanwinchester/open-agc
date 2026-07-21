# -*- coding: utf-8 -*-
"""Stage3-Task3 consistency evidence: single shared download-state dict,
single downloads _db_path, plugin-specific benchmark _db_path/engine."""
import api.server  # noqa: F401  (runs main init + plugin discovery/mount)

import api.state
import api.ws
import api.routes.downloads as dl
import api.routes.benchmark as bm
import api.routes.routes_settings as rs
from api.db import DB_PATH as MAIN_DB

state = api.state._llamacpp_download_state
checks = [
    ("api.state", state),
    ("api.ws", api.ws._llamacpp_download_state),
    ("api.routes.downloads", dl._llamacpp_download_state),
    ("api.routes.routes_settings", rs._llamacpp_download_state),
    ("api.routes.benchmark", bm._llamacpp_download_state),
]
print("== _llamacpp_download_state identity ==")
for name, ref in checks:
    print(f"{name:32s} id={id(ref)} same_as_api.state={ref is state}")

print("\n== in-place mutation visible everywhere ==")
state["stage"] = "evidence_probe"
print("via api.ws      :", api.ws._llamacpp_download_state.get("stage"))
print("via downloads   :", dl._llamacpp_download_state.get("stage"))
print("via settings    :", rs._llamacpp_download_state.get("stage"))
state["stage"] = ""

print("\n== downloads module points at MAIN db ==")
print("downloads._db_path:", dl._db_path)
print("main DB_PATH      :", MAIN_DB)
print("match:", dl._db_path == MAIN_DB)
print("install_state id (downloads):", id(dl._training_install_state))

print("\n== benchmark module points at PLUGIN db + plugin engine ==")
print("benchmark._db_path:", bm._db_path)
engine = bm._get_training_engine() if bm._get_training_engine else None
print("benchmark engine  :", type(engine).__name__ if engine else None)
print("load_config is live api.config.load_config:", bm._load_config)

import core.plugin_manager as pm
print("\nplugins loaded:", [p.name for p in pm.list_plugins()] if hasattr(pm, 'list_plugins') else 'n/a')
print("EVIDENCE_DONE")
