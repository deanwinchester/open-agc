"""
Download management — dataset downloads, install deps, recommended datasets.
"""
import os
import json
import time as _time
import threading
import sqlite3
import sys

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Optional
import requests

router = APIRouter(prefix="", tags=["downloads"])


def _get_proxies():
    proxies = {}
    for var in ["HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"]:
        val = os.environ.get(var,"")
        if val:
            proxies["http"] = proxies["https"] = val
            break
    if not proxies:
        try:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            for k in ["https","http"]:
                if k in sys_proxies and sys_proxies[k]:
                    proxies["http"] = proxies["https"] = sys_proxies[k]
                    break
        except Exception:
            pass
    if not proxies:
        try:
            if _load_config:
                cfg = _load_config()
                p = cfg.get("http_proxy", "") or cfg.get("proxy", "")
                if p:
                    proxies["http"] = proxies["https"] = p
        except Exception:
            pass
    return proxies if proxies else None


def _get(url, **kwargs):
    proxies = _get_proxies()
    if proxies:
        kwargs.setdefault("proxies", proxies)
    
    # Inject HF Token for gated datasets
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token and "huggingface.co" in url:
        headers = kwargs.get("headers", {})
        if "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {hf_token}"
            kwargs["headers"] = headers
            
    return requests.get(url, **kwargs)

# ── shared state (injected at init) ──

_db_path = None
_llamacpp_download_state = None
_training_install_state = None
_broadcast = None
_training_available = False
_get_llamacpp_manager = None
_load_config = None
_download_slots = {}  # Parallel download support: {download_id: state}
_MAX_PARALLEL = 3


def init_download_routes(db_path, download_state, install_state, broadcast_fn,
                         training_avail, get_llamacpp, load_config=None):
    global _db_path, _llamacpp_download_state, _training_install_state
    global _broadcast, _training_available, _get_llamacpp_manager, _load_config
    _db_path = db_path
    _llamacpp_download_state = download_state
    _training_install_state = install_state
    _broadcast = broadcast_fn
    _training_available = training_avail
    _get_llamacpp_manager = get_llamacpp
    _load_config = load_config


def _active_download_count():
    return sum(1 for s in _download_slots.values() if s.get("active"))


# ── DB Helpers ──

def _create_download_record(type_: str, label: str, repo_id: str = None,
                            filename: str = None, source: str = "huggingface",
                            url: str = None, target_path: str = "",
                            partial_path: str = "", total_size: int = 0) -> int:
    conn = sqlite3.connect(_db_path)
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO downloads (type, label, repo_id, filename, source, url,
           target_path, partial_path, total_size, downloaded_bytes, status, progress)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'downloading', 0.0)''',
        (type_, label, repo_id, filename, source, url, target_path, partial_path, total_size)
    )
    dl_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return dl_id


def _update_download_progress(download_id: int, progress: float,
                               downloaded_bytes: int = None,
                               status: str = None, error_message: str = None):
    conn = sqlite3.connect(_db_path)
    cursor = conn.cursor()
    fields = ["progress=?", "updated_at=CURRENT_TIMESTAMP"]
    params = [progress]
    if downloaded_bytes is not None:
        fields.append("downloaded_bytes=?"); params.append(downloaded_bytes)
    if status is not None:
        fields.append("status=?"); params.append(status)
    if error_message is not None:
        fields.append("error_message=?"); params.append(error_message)
    params.append(download_id)
    cursor.execute(f"UPDATE downloads SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    conn.close()


# ── Recommended Datasets ──

RECOMMENDED_DATASETS = [
    {"repo_id": "fuguowen/alpaca_zh", "name": "Alpaca 中文指令", "desc": "中文指令微调数据集，约4.8万条", "size": "~50MB", "splits": ["train"]},
    {"repo_id": "tatsu-lab/alpaca", "name": "Alpaca 英文指令 (52K)", "desc": "经典英文指令微调数据集", "size": "~25MB", "splits": ["train"]},
    {"repo_id": "databricks/databricks-dolly-15k", "name": "Dolly 15K", "desc": "Databricks 通用指令数据集", "size": "~10MB", "splits": ["train"]},
    {"repo_id": "Open-Orca/OpenOrca", "name": "OpenOrca", "desc": "大规模推理链微调数据", "size": "~800MB", "splits": ["train"]},
    {"repo_id": "Open-Orca/SlimOrca", "name": "SlimOrca", "desc": "精简版推理链数据集", "size": "~200MB", "splits": ["train"]},
    {"repo_id": "HuggingFaceH4/ultrachat_200k", "name": "UltraChat 200K", "desc": "多轮对话微调数据", "size": "~1.5GB", "splits": ["train_sft", "test_sft"]},
    {"repo_id": "Salesforce/wikitext", "name": "WikiText-103", "desc": "语言建模基准数据集", "size": "~180MB", "splits": ["train", "validation", "test"], "config": "wikitext-103-raw-v1"},
    {"repo_id": "cnn_dailymail", "name": "CNN/DailyMail", "desc": "新闻摘要数据集", "size": "~550MB", "splits": ["train", "validation", "test"], "config": "3.0.0"},
]


@router.get("/api/training/recommended-datasets")
async def get_recommended_datasets():
    return {"datasets": RECOMMENDED_DATASETS}


# ── Unified Dataset Download ──

class DatasetDownloadRequest(BaseModel):
    repo_id: str
    name: str = ""
    split: str = "train"
    config: Optional[str] = None
    target_file: Optional[str] = None   # specific file to download (from siblings list)


@router.get("/api/downloads/dataset-files/{repo_id:path}")
async def list_dataset_files(repo_id: str, split: str = "", config: str = ""):
    """List available data files in a HuggingFace dataset repo.

    Returns files grouped by inferred split, so the frontend can let the user
    pick which files to download.
    """
    api_url = f"https://huggingface.co/api/datasets/{repo_id}"
    resp = _get(api_url, timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"HF API returned {resp.status_code}")
    ds_info = resp.json()
    siblings = ds_info.get("siblings", [])

    data_exts = {".jsonl", ".parquet", ".arrow", ".json", ".csv"}
    data_files = []
    for s in siblings:
        rf = s.get("rfilename", "")
        ext = os.path.splitext(rf)[1].lower()
        if ext in data_exts:
            # Guess split from path components
            parts = rf.replace("\\", "/").split("/")
            inferred_split = "unknown"
            for known in ["train", "test", "validation", "val", "dev", "eval"]:
                if known in parts:
                    inferred_split = known
                    break
            # Also look at directory name (e.g. data/train/, data/test/)
            if inferred_split == "unknown":
                for p in parts:
                    if p in ("train", "test", "validation", "val", "dev", "eval"):
                        inferred_split = p
                        break

            # Estimate file size
            size_bytes = s.get("size", 0)
            size_str = ""
            if size_bytes > 1e9:
                size_str = f"{size_bytes/1e9:.1f} GB"
            elif size_bytes > 1e6:
                size_str = f"{size_bytes/1e6:.1f} MB"
            elif size_bytes > 1e3:
                size_str = f"{size_bytes/1e3:.1f} KB"
            elif size_bytes > 0:
                size_str = f"{size_bytes} B"

            data_files.append({
                "rfilename": rf,
                "basename": os.path.basename(rf),
                "ext": ext,
                "split": inferred_split,
                "size": size_bytes,
                "size_str": size_str,
            })

    # Filter by requested split/config
    if split:
        data_files = [f for f in data_files if split in f.get("split", "")]
    if config:
        data_files = [f for f in data_files if config in f["rfilename"]]

    # Group by split
    by_split = {}
    for f in data_files:
        sp = f["split"]
        by_split.setdefault(sp, []).append(f)

    return {
        "repo_id": repo_id,
        "total_files": len(data_files),
        "by_split": {k: sorted(v, key=lambda x: x["rfilename"]) for k, v in sorted(by_split.items())},
        "all_files": sorted(data_files, key=lambda x: x["rfilename"]),
    }


@router.get("/api/downloads/dataset-configs/{repo_id:path}")
async def list_dataset_configs(repo_id: str):
    """List available configs/subset names for a HuggingFace dataset.

    Some datasets (e.g. Salesforce/wikitext, cnn_dailymail) have multiple
    configs that must be specified in `load_dataset(name=...)`.
    """
    api_url = f"https://huggingface.co/api/datasets/{repo_id}"
    resp = _get(api_url, timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"HF API returned {resp.status_code}")
    ds_info = resp.json()

    # HF API returns configs in `configs` or `card.configs` or `dataset_info.configs`
    configs = []

    # The HF datasets-server API may nest configs differently
    # Try the direct key first
    raw_configs = ds_info.get("configs", [])
    if not raw_configs:
        # Try nested paths
        raw_configs = ds_info.get("cardData", {}).get("configs", [])
    if not raw_configs:
        raw_configs = ds_info.get("dataset_info", {}).get("configs", [])

    for c in raw_configs:
        if isinstance(c, str):
            configs.append({"name": c, "label": c})
        elif isinstance(c, dict):
            configs.append({
                "name": c.get("config", c.get("name", c.get("subset", ""))),
                "label": c.get("config", c.get("name", c.get("subset", ""))),
                "description": c.get("description", ""),
            })

    # If HF API doesn't expose configs, try the old-school approach: siblings
    if not configs:
        # Try cardData.dataset_info
        card_data = ds_info.get("cardData", {}) or {}
        dataset_info = card_data.get("dataset_info", {}) or {}
        features = dataset_info.get("features", {})
        # If no configs array, assume single-config dataset
        pass

    return {
        "repo_id": repo_id,
        "configs": configs,
        "needs_config": len(configs) > 1,
    }


@router.post("/api/downloads/dataset")
async def download_dataset(req: DatasetDownloadRequest):
    """Download a HF dataset with progress tracking (supports parallel downloads)."""
    global _llamacpp_download_state

    if _active_download_count() >= _MAX_PARALLEL:
        raise HTTPException(status_code=409, detail=f"已达最大并行下载数 ({_MAX_PARALLEL})，请等待其他任务完成")

    use_datasets_lib = False
    try:
        import datasets  # noqa: F401
        use_datasets_lib = True
    except ImportError:
        pass

    ds_name = req.name or req.repo_id.split("/")[-1]
    dataset_dir = os.path.join(os.path.dirname(_db_path), "datasets")
    os.makedirs(dataset_dir, exist_ok=True)
    storage_dir = os.path.join(dataset_dir, f"ds_{int(_time.time())}")
    os.makedirs(storage_dir, exist_ok=True)
    partial_path = os.path.join(storage_dir, "data.jsonl.partial")
    target_path = os.path.join(storage_dir, "data.jsonl")

    db_download_id = _create_download_record(
        type_="model", label=f"数据集: {ds_name} ({req.split})",
        repo_id=req.repo_id, source="huggingface",
        target_path=target_path, partial_path=partial_path
    )
    conn = sqlite3.connect(_db_path)
    conn.cursor().execute("UPDATE downloads SET type='dataset' WHERE id=?", (db_download_id,))
    conn.commit(); conn.close()

    # Register in download slots for parallel tracking
    slot_state = {
        "active": True, "type": "dataset",
        "label": f"正在下载数据集 {ds_name}...", "progress": 0.0,
        "stage": "downloading", "error": "",
        "repo_id": req.repo_id, "name": ds_name,
        "download_id": db_download_id
    }
    _download_slots[db_download_id] = slot_state
    # Update global state for banner
    _llamacpp_download_state.update(slot_state)

    def run_download():
        global _llamacpp_download_state
        dl_id = db_download_id
        slot = _download_slots[dl_id]
        try:
            def progress_cb(ratio, label=None):
                slot["progress"] = ratio
                _update_download_progress(dl_id, ratio)
                _broadcast({"type":"llamacpp_download","task":"dataset",
                            "download_id": dl_id,
                            "label": label or f"正在下载 {ds_name}...",
                            "progress":ratio,"stage":"downloading"})

            count = 0
            if use_datasets_lib:
                # Ensure HF Hub sees our proxy config (datasets lib reads env vars)
                proxies = _get_proxies()
                if proxies:
                    for env_key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
                        if proxies.get("http") and not os.environ.get(env_key):
                            os.environ[env_key] = proxies["http"]
                from datasets import load_dataset
                hf_token = os.environ.get("HF_TOKEN")
                kw = {"path": req.repo_id, "split": req.split, "streaming": False,
                      "trust_remote_code": True, "cache_dir": storage_dir,
                      "token": hf_token}
                if req.config: kw["name"] = req.config
                try:
                    ds = load_dataset(**kw)
                except ValueError as e:
                    msg = str(e)
                    if "Config name is missing" in msg or "available configs" in msg:
                        raise Exception(f"{msg}\n\n请在下载前选择 config，或通过前端弹窗指定。")
                    raise
                total = len(ds)
                with open(partial_path, "w", encoding="utf-8") as f:
                    for sample in ds:
                        f.write(json.dumps(sample, ensure_ascii=False)+"\n")
                        count += 1
                        if count % 100 == 0: progress_cb(count/max(total,1))
            else:
                _download_via_http(req, partial_path, progress_cb, ds_name, count)
                count = sum(1 for _ in open(partial_path,"r",encoding="utf-8")) if os.path.exists(partial_path) else 0

            if os.path.exists(partial_path):
                os.rename(partial_path, target_path)
            if count == 0 and os.path.exists(target_path):
                count = sum(1 for _ in open(target_path,"r",encoding="utf-8") if _.strip())

            conn2 = sqlite3.connect(_db_path); cur2 = conn2.cursor()
            cur2.execute("INSERT INTO datasets (name,source,source_path,storage_path,format,sample_count) VALUES (?,'huggingface',?,?,'jsonl',?)",
                         (ds_name, req.repo_id, target_path, count))
            conn2.commit(); conn2.close()

            _llamacpp_download_state.update({"active":False,"stage":"complete","progress":1.0})
            slot.update({"active":False,"stage":"complete","progress":1.0})
            _update_download_progress(dl_id, 1.0, status="completed", downloaded_bytes=count)
            _broadcast({"type":"llamacpp_download","task":"dataset","download_id":dl_id,
                        "label":f"{ds_name} 下载完成 ({count} 条)","progress":1.0,"stage":"complete"})
            _download_slots.pop(dl_id, None)
        except Exception as e:
            slot.update({"active":False,"stage":"error","error":str(e)})
            _llamacpp_download_state.update({"active":False,"stage":"error","error":str(e)})
            _update_download_progress(dl_id, 0.0, status="failed", error_message=str(e))
            _broadcast({"type":"llamacpp_download","task":"dataset","download_id":dl_id,
                        "label":f"下载失败: {e}","progress":0.0,"stage":"error","error":str(e)})
            _download_slots.pop(dl_id, None)

    threading.Thread(target=run_download, daemon=True).start()
    return {"status":"started","message":f"开始下载数据集 {ds_name}...","download_id":db_download_id}


def _download_via_http(req, partial_path, progress_cb, ds_name, count_ref):
    """Fallback: download dataset via HF REST API (no datasets lib needed)."""
    # If a specific target_file was requested, use it directly
    if req.target_file:
        rfilename = req.target_file
    else:
        progress_cb(0.05, f"正在获取 {ds_name} 的文件列表...")
        api_url = f"https://huggingface.co/api/datasets/{req.repo_id}"
        resp = _get(api_url, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"HF API HTTP {resp.status_code}")
        ds_info = resp.json()
        siblings = ds_info.get("siblings", [])

        # Search for data files: jsonl, parquet, arrow, json, csv
        data_exts = [".jsonl", ".parquet", ".arrow", ".json", ".csv"]
        data_files = [s for s in siblings
                      if any(s.get("rfilename","").endswith(ext) for ext in data_exts)]
        if not data_files:
            raise Exception(f"未找到数据文件，请安装 datasets 库: pip install datasets pyarrow")

        target_split = req.split
        matching = [f for f in data_files if target_split in f["rfilename"]]
        if req.config:
            matching = [f for f in matching if req.config in f["rfilename"]]
        if not matching:
            matching = data_files

        rfilename = matching[0]["rfilename"]

    dl_url = f"https://huggingface.co/datasets/{req.repo_id}/resolve/main/{rfilename}"
    progress_cb(0.1, f"下载 {rfilename}...")
    dl_resp = _get(dl_url, stream=True, timeout=120)

    # Download to temp file first
    tmp_path = partial_path + ".tmp"
    total = int(dl_resp.headers.get("content-length", 0))
    downloaded = 0
    with open(tmp_path, "wb") as f:
        for chunk in dl_resp.iter_content(8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    progress_cb(0.1 + 0.7 * downloaded / total,
                                f"下载中 ({downloaded/1024**2:.0f}MB)...")

    progress_cb(0.85, "处理中...")

    # Convert to JSONL based on file format
    ext = os.path.splitext(rfilename)[1].lower()
    if ext in (".parquet", ".arrow"):
        _convert_parquet_to_jsonl(tmp_path, partial_path, progress_cb)
    elif ext == ".csv":
        _convert_csv_to_jsonl(tmp_path, partial_path, progress_cb)
    elif ext == ".json":
        _convert_json_to_jsonl(tmp_path, partial_path, progress_cb)
    else:
        # .jsonl — already in the right format, just rename
        os.rename(tmp_path, partial_path)

    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    progress_cb(0.95, "处理中...")


def _convert_parquet_to_jsonl(src_path, dst_path, progress_cb):
    """Convert parquet/arrow file to JSONL."""
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(src_path)
        df = table.to_pandas()
    except ImportError:
        try:
            import pandas as pd
            df = pd.read_parquet(src_path)
        except ImportError:
            raise Exception("需要 pyarrow 或 pandas 读取 Parquet 文件: pip install pyarrow pandas")
    records = df.to_dict(orient="records")
    with open(dst_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _convert_csv_to_jsonl(src_path, dst_path, progress_cb):
    """Convert CSV file to JSONL."""
    import csv as _csv
    with open(src_path, "r", encoding="utf-8", errors="replace") as inf, \
         open(dst_path, "w", encoding="utf-8") as outf:
        reader = _csv.DictReader(inf)
        for row in reader:
            outf.write(json.dumps(row, ensure_ascii=False) + "\n")


def _convert_json_to_jsonl(src_path, dst_path, progress_cb):
    """Convert JSON array file to JSONL (one object per line)."""
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        with open(dst_path, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    else:
        # Single JSON object — write as one line
        with open(dst_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


# ── Training Deps Install ──

@router.post("/api/training/install-deps")
async def install_training_deps():
    """Install training dependencies via pip with real-time progress."""
    global _training_install_state
    if _training_install_state["active"]:
        raise HTTPException(status_code=409, detail="安装任务正在进行中")

    # Check which core packages are actually missing using importlib (cache-safe)
    import importlib
    _core_pkgs = {
        "torch": "torch", "transformers": "transformers",
        "peft": "peft", "datasets": "datasets"
    }
    missing = []
    for pkg_name, import_name in _core_pkgs.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg_name)

    if not missing:
        raise HTTPException(status_code=400, detail="训练依赖已安装")

    deps = ["torch>=2.1.0","transformers>=4.35.0","peft>=0.6.0",
            "accelerate>=0.24.0","datasets>=2.14.0",
            "sentencepiece>=0.1.99","scikit-learn>=1.0.0","pyyaml>=6.0.0"]
    if sys.platform.startswith("linux"):
        deps.append("bitsandbytes>=0.41.0")

    _training_install_state.update({
        "active":True,"stage":"installing",
        "label":f"正在安装训练依赖 (缺失: {', '.join(missing)})...","progress":0.0,
        "current_pkg":", ".join(deps),"error":""
    })

    def run_install():
        global _training_install_state
        import subprocess as sp, re, importlib
        try:
            cmd = [sys.executable,"-m","pip","install","--progress-bar","on"] + deps
            proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.STDOUT, text=True, bufsize=1)
            pkg_count = len(deps); last_bc = 0; output_lines = []

            # Track progress by top-level package names (not transitive deps)
            top_level_names = set()
            for d in deps:
                # Extract package name from version specifier like "torch>=2.1.0"
                name = re.split(r'[><=!]', d)[0].strip().lower().replace('-', '_')
                top_level_names.add(name)
            seen_satisfied = set()

            for line in proc.stdout:
                line = line.strip()
                if not line: continue
                output_lines.append(line)
                if len(output_lines) > 50: output_lines.pop(0)
                now = _time.time()

                if "Successfully installed" in line:
                    # "Successfully installed torch-2.x transformers-4.x ..."
                    # Count how many of our top-level deps were in this line
                    parts = line.replace("Successfully installed", "").strip().split()
                    for part in parts:
                        pkg_base = re.split(r'[-]?\d', part)[0].strip().lower().replace('-', '_')
                        if pkg_base in top_level_names:
                            seen_satisfied.add(pkg_base)
                    progress = min(len(seen_satisfied) / pkg_count, 0.98)
                    _training_install_state["progress"] = progress
                    _training_install_state["label"] = f"已安装 {len(seen_satisfied)}/{pkg_count} 个包"
                elif "Requirement already satisfied" in line:
                    # Only count if it's a top-level package, not a transitive dep
                    m = re.match(r'Requirement already satisfied:\s*(\S+)', line)
                    if m:
                        pkg_base = re.split(r'[><=!;\[]', m.group(1))[0].strip().lower().replace('-', '_')
                        if pkg_base in top_level_names:
                            seen_satisfied.add(pkg_base)
                            progress = min(len(seen_satisfied) / pkg_count, 0.98)
                            _training_install_state["progress"] = progress
                            _training_install_state["label"] = f"已满足 {len(seen_satisfied)}/{pkg_count} 个包"
                elif "Downloading" in line:
                    m = re.search(r'(\d+)%', line)
                    if m:
                        base_progress = len(seen_satisfied) / pkg_count
                        _training_install_state["progress"] = base_progress + (int(m.group(1))/100) / pkg_count
                    _training_install_state["label"] = line[:130]
                elif any(w in line for w in ["Collecting","Installing","Building"]):
                    _training_install_state["label"] = line[:130]
                elif "ERROR" in line or "error:" in line.lower():
                    _training_install_state["label"] = f"⚠ {line[:130]}"

                if now - last_bc > 0.5:
                    _broadcast({"type":"training_install_progress","stage":"installing",
                                "label":_training_install_state["label"],
                                "progress":_training_install_state["progress"],
                                "current_pkg":"","active":True})
                    last_bc = now

            proc.wait()

            # Verify imports using importlib (avoids Python import cache issues)
            # invalidate_caches() ensures newly-installed packages are discoverable
            importlib.invalidate_caches()
            failed_imports = []
            for pkg_name, import_name in [("torch","torch"),("transformers","transformers"),
                                           ("peft","peft"),("datasets","datasets")]:
                try:
                    importlib.import_module(import_name)
                except ImportError:
                    failed_imports.append(pkg_name)

            if failed_imports:
                # Packages are still not importable even after pip ran
                err_detail = f"以下包安装后仍无法导入: {', '.join(failed_imports)}"
                if proc.returncode != 0:
                    err_detail += f"\npip 退出码: {proc.returncode}"
                    pip_output = "\n".join(output_lines[-10:])
                    err_detail += f"\n{pip_output[:300]}"
                _training_install_state.update({"active":False,"stage":"error",
                    "error":err_detail[:500]})
                _broadcast({"type":"training_install_progress","stage":"error",
                            "label":f"安装不完整: {', '.join(failed_imports)} 无法导入",
                            "progress":0,"error":_training_install_state["error"],"active":False})
                return

            # All core packages importable — success regardless of pip exit code
            # (pip may exit 1 due to non-fatal warnings like "new pip version available")
            _training_install_state.update({"active":False,"stage":"complete","progress":1.0,
                "label":"依赖安装完成，请刷新页面启用训练功能"})
            _broadcast({"type":"training_install_progress","stage":"complete",
                        "label":_training_install_state["label"],"progress":1.0,"active":False})
        except Exception as e:
            _training_install_state.update({"active":False,"stage":"error","error":str(e)[:300]})
            _broadcast({"type":"training_install_progress","stage":"error",
                        "label":f"安装异常: {str(e)[:100]}","progress":0,
                        "error":str(e)[:300],"active":False})

    threading.Thread(target=run_install, daemon=True).start()
    return {"status":"started","message":f"正在安装训练依赖 (缺失: {', '.join(missing)})...","total":len(deps)}


# ── Status Endpoint ──

@router.get("/api/training/status")
async def get_training_status():
    from core.training_engine import get_training_engine
    engine = get_training_engine()
    available = _training_available
    import_error = ""
    if not available:
        for pkg in ["torch","transformers","peft"]:
            try:
                __import__(pkg)
            except ImportError as e:
                import_error = f"{pkg}: {e}"; break
            except Exception as e:
                import_error = f"{pkg}: {e}"; break
        if not import_error: available = True
    return {"available":available,"import_error":import_error,
            "engine_state":engine.get_state(),"install_state":_training_install_state}
