"""
Plugin APIRouter — wraps all training endpoints.

Uses existing route module functions from api/routes/benchmark.py
and api/routes/downloads.py, re-initialized with the plugin's
database path and engine instance.
"""
import os
import sys

# Ensure the main project is on the path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastapi import APIRouter


def create_router(db_path: str, engine, broadcast_fn, server_config: dict):
    """Create and configure the training APIRouter.

    Args:
        db_path: Path to training.db
        engine: TrainingEngine instance
        broadcast_fn: WebSocket broadcast function
        server_config: Server config dict
    """
    router = APIRouter(prefix="", tags=["training"])

    # ── Re-initialize benchmark routes with plugin deps ──
    from api.routes.benchmark import (
        router as benchmark_router,
        init_benchmark_routes,
    )
    init_benchmark_routes(
        db_path=db_path,
        download_state={},
        install_state={},
        broadcast_fn=broadcast_fn,
        get_engine=lambda: engine,
        get_llamacpp=get_llamacpp_manager,
        load_config=lambda: server_config,
    )
    # Mount benchmark endpoints
    for route in benchmark_router.routes:
        router.routes.append(route)

    # ── Re-initialize download routes with plugin deps ──
    try:
        from api.routes.downloads import (
            router as downloads_router,
            init_download_routes,
        )
        init_download_routes(
            db_path=db_path,
            download_state={},
            install_state={"active": False, "stage": "idle", "label": "", "progress": 0, "error": ""},
            broadcast_fn=broadcast_fn,
            training_avail=True,
            get_llamacpp=get_llamacpp_manager,
            load_config=lambda: server_config,
        )
        for route in downloads_router.routes:
            router.routes.append(route)
    except ImportError:
        pass

    # ── Plugin-specific endpoints (PPL, eval) ──
    _register_eval_endpoints(router, db_path, engine, broadcast_fn)

    # ── Training runs CRUD endpoints ──
    _register_training_endpoints(router, db_path, engine, broadcast_fn, server_config)

    # ── Training status (health check) ──
    @router.get("/status")
    async def training_status():
        return {"status": "ok", "plugin": "open-agc-train", "version": "1.0.0"}

    return router


def _register_eval_endpoints(router, db_path, engine, broadcast_fn):
    """Register PPL and metrics evaluation endpoints."""
    import json, sqlite3, threading
    from fastapi import HTTPException
    from pydantic import BaseModel
    from typing import Optional

    class EvalPPLRequest(BaseModel):
        dataset_path: str = ""
        max_samples: int = 500
        stride: int = 512
        max_length: int = 1024
        dataset_id: Optional[int] = None

    @router.post("/runs/{run_id}/eval-ppl")
    async def eval_model_ppl(run_id: int, req: EvalPPLRequest):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM training_runs WHERE id=? AND status IN ('completed','aborted_saved')", (run_id,))
        run = cursor.fetchone()
        conn.close()
        if not run:
            raise HTTPException(status_code=404, detail="训练运行不存在或未完成")
        save_dir = run["checkpoint_dir"]
        if not save_dir or not os.path.isdir(save_dir):
            raise HTTPException(status_code=400, detail=f"模型目录不存在: {save_dir}")

        dataset_path = req.dataset_path
        if not dataset_path and save_dir:
            val_file = os.path.join(save_dir, "validation.jsonl")
            if os.path.exists(val_file):
                dataset_path = val_file
        if not dataset_path and req.dataset_id:
            conn2 = sqlite3.connect(db_path)
            conn2.row_factory = sqlite3.Row
            cur = conn2.cursor()
            cur.execute("SELECT storage_path FROM datasets WHERE id=?", (req.dataset_id,))
            ds = cur.fetchone()
            conn2.close()
            if ds and ds["storage_path"]:
                dataset_path = ds["storage_path"]

        result_holder = {}

        def run_eval():
            try:
                from .eval import compute_ppl
                kwargs = {"model_path": save_dir, "max_samples": req.max_samples,
                          "stride": req.stride, "max_length": req.max_length}
                if dataset_path and os.path.exists(dataset_path):
                    kwargs["dataset_path"] = dataset_path
                result = compute_ppl(**kwargs)
                conn3 = sqlite3.connect(db_path)
                conn3.cursor().execute(
                    "INSERT INTO benchmark_results (model_id,model_source,benchmark_type,metrics_json,num_questions,avg_latency_ms,tokens_per_second) VALUES (?,?,?,?,?,?,?)",
                    (f"trained/run_{run_id}", "trained", "ppl",
                     json.dumps(result, ensure_ascii=False), result.get("num_windows", 0),
                     result.get("eval_time_seconds", 0) * 1000, 0))
                conn3.commit(); conn3.close()
                result_holder["result"] = result
            except Exception as e:
                result_holder["error"] = str(e)

        threading.Thread(target=run_eval, daemon=True).start()
        return {"status": "started", "message": f"开始 PPL 评估 (run_{run_id})..."}

    @router.get("/runs/{run_id}/eval-ppl")
    async def get_eval_ppl_result(run_id: int):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM benchmark_results WHERE model_id=? AND benchmark_type='ppl' ORDER BY created_at DESC LIMIT 1",
            (f"trained/run_{run_id}",))
        row = cursor.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="未找到 PPL 评估结果")
        d = dict(row)
        d["metrics_json"] = json.loads(d["metrics_json"]) if isinstance(d["metrics_json"], str) else d["metrics_json"]
        return d

    class EvalMetricsRequest(BaseModel):
        model_path: str = ""
        dataset_path: str = ""
        dataset_id: Optional[int] = None
        max_samples: int = 100

    @router.post("/eval-metrics")
    async def eval_generation_metrics(req: EvalMetricsRequest):
        model_path = req.model_path
        if model_path.startswith("run_"):
            run_id = int(model_path.replace("run_", ""))
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT checkpoint_dir FROM training_runs WHERE id=?", (run_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row["checkpoint_dir"]:
                model_path = row["checkpoint_dir"]
            else:
                raise HTTPException(status_code=404, detail="训练运行不存在")
        dataset_path = req.dataset_path
        if not dataset_path and req.dataset_id:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT storage_path FROM datasets WHERE id=?", (req.dataset_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                dataset_path = row["storage_path"]
        if not dataset_path:
            raise HTTPException(status_code=400, detail="请提供评测数据集")

        from .eval import compute_generation_metrics
        result = compute_generation_metrics(model_path, dataset_path, req.max_samples)
        return {"status": "ok", "metrics": result}


def _register_training_endpoints(router, db_path, engine, broadcast_fn, server_config):
    """Register training run CRUD, model configs, datasets, and benchmark endpoints."""
    import json, sqlite3, threading, time as _time
    from fastapi import HTTPException, UploadFile, File, Form
    from pydantic import BaseModel
    from typing import Optional, List

    # ── Model Configs ──
    class ModelConfigRequest(BaseModel):
        name: str
        architecture: str
        config_json: str
        param_count_estimate: int = 0

    @router.get("/model-configs")
    async def list_model_configs():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM model_configs ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return {"configs": [dict(r) for r in rows]}

    @router.post("/model-configs")
    async def create_model_config(req: ModelConfigRequest):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO model_configs (name, architecture, config_json, param_count_estimate) VALUES (?, ?, ?, ?)",
            (req.name, req.architecture, req.config_json, req.param_count_estimate))
        config_id = cursor.lastrowid
        conn.commit(); conn.close()
        return {"status": "success", "id": config_id}

    @router.get("/model-configs/{config_id}")
    async def get_model_config(config_id: int):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM model_configs WHERE id=?", (config_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Config not found")
        return dict(row)

    @router.delete("/model-configs/{config_id}")
    async def delete_model_config(config_id: int):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM model_configs WHERE id=?", (config_id,))
        conn.commit(); conn.close()
        return {"status": "success"}

    # ── Estimate ──
    class EstimateRequest(BaseModel):
        architecture: str
        config_json: str

    @router.post("/model-configs/estimate")
    async def estimate_model(req: EstimateRequest):
        import math
        config = json.loads(req.config_json)
        arch = req.architecture
        num_layers = config.get("num_layers", 12)
        hidden_size = config.get("hidden_size", 768)
        num_heads = config.get("num_attention_heads", 12)
        intermediate_size = config.get("intermediate_size", hidden_size * 4)
        vocab_size = config.get("vocab_size", 50000)
        max_seq_len = config.get("max_seq_len", config.get("max_seq_length", 2048))
        head_dim = hidden_size // num_heads
        norm_type = config.get("norm_type", "layer_norm")
        norm_params = hidden_size if norm_type == "rms_norm" else 2 * hidden_size
        tie_embeddings = config.get("tie_word_embeddings", False)
        embed_params = vocab_size * hidden_size
        pos_encoding = config.get("pos_encoding", "rope")
        if pos_encoding == "learned":
            embed_params += max_seq_len * hidden_size

        if arch in ("gpt_decoder", "llama"):
            kv_heads = int(config.get("kv_heads", num_heads))
            attn_params = (hidden_size * hidden_size + 2 * hidden_size * kv_heads * head_dim + hidden_size * hidden_size)
            activation = config.get("activation", "gelu")
            ffn_params = 3 * hidden_size * intermediate_size if activation in ("swiglu","silu") else 2 * hidden_size * intermediate_size
            np = 3 if config.get("norm_position","pre_norm") == "sandwich_norm" else 2
            layer_params = attn_params + ffn_params + np * norm_params
            total_params = embed_params + num_layers * layer_params
            if not tie_embeddings: total_params += hidden_size * vocab_size
            total_params += norm_params
        elif arch == "moe":
            kv_heads = int(config.get("kv_heads", num_heads))
            attn_params = hidden_size * hidden_size + 2 * hidden_size * kv_heads * head_dim + hidden_size * hidden_size
            num_experts = config.get("num_experts", 8)
            expert_ffn = 3 * hidden_size * intermediate_size
            router_params = hidden_size * num_experts
            layer_params = attn_params + num_experts * expert_ffn + router_params + 2 * norm_params
            total_params = embed_params + num_layers * layer_params
            if not tie_embeddings: total_params += hidden_size * vocab_size
            total_params += norm_params
        else:
            attn_params = 4 * hidden_size * hidden_size
            ffn_params = 2 * hidden_size * intermediate_size
            layer_params = attn_params + ffn_params + 2 * norm_params
            total_params = embed_params + num_layers * layer_params + norm_params
            if not tie_embeddings: total_params += hidden_size * vocab_size

        flops_per_token = 2 * total_params
        fp32_bytes = total_params * 4
        memory_mb = round(fp32_bytes / (1024 * 1024))
        training_memory_mb = round(fp32_bytes * 4 / (1024 * 1024))
        return {
            "total_params": total_params,
            "total_params_formatted": f"{total_params/1e6:.1f}M" if total_params < 1e9 else f"{total_params/1e9:.2f}B",
            "flops_per_forward": round(flops_per_token * max_seq_len),
            "flops_per_token": round(flops_per_token),
            "embed_params": embed_params,
            "per_layer_params": round(total_params / num_layers) if num_layers else 0,
            "num_layers": num_layers,
            "architecture": arch,
            "memory_mb": memory_mb,
            "training_memory_mb": training_memory_mb,
        }

    # ── Training Runs ──
    class CreateTrainingRunRequest(BaseModel):
        name: str
        model_config_id: Optional[int] = None
        dataset_id: Optional[int] = None
        base_model_id: str = ""
        base_model_source: str = "huggingface"
        training_params_json: str = "{}"
        val_ratio: float = 0.1

    @router.post("/runs")
    async def create_training_run(req: CreateTrainingRunRequest):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO training_runs (name, model_config_id, dataset_id, base_model_id, base_model_source, training_params_json, status) VALUES (?,?,?,?,?,?, 'pending')",
            (req.name, req.model_config_id, req.dataset_id, req.base_model_id, req.base_model_source, req.training_params_json))
        run_id = cursor.lastrowid
        conn.commit()
        cursor.execute("SELECT * FROM training_runs WHERE id=?", (run_id,))
        run_record = dict(cursor.fetchone())
        conn.close()
        run_record["val_ratio"] = req.val_ratio

        if engine.is_available():
            ok = engine.start_training(run_id, run_record)
            if ok:
                return {"status": "started", "run_id": run_id}
            return {"status": "queued", "message": "另一个训练已在运行中"}
        return {"status": "error", "detail": "训练环境不可用"}

    @router.get("/runs")
    async def list_training_runs():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM training_runs ORDER BY created_at DESC LIMIT 50")
        rows = cursor.fetchall()
        conn.close()
        return {"runs": [dict(r) for r in rows]}

    @router.get("/runs/{run_id}")
    async def get_training_run(run_id: int):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM training_runs WHERE id=?", (run_id,))
        row = cursor.fetchone()
        conn.close()
        if not row: raise HTTPException(status_code=404)
        return dict(row)

    @router.delete("/runs/{run_id}")
    async def delete_training_run(run_id: int):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM training_runs WHERE id=?", (run_id,))
        conn.commit(); conn.close()
        return {"status": "success"}

    # ── Training Run Control ──
    @router.post("/runs/{run_id}/{action}")
    async def control_training_run(run_id: int, action: str):
        state = engine.get_state()
        if state.get("run_id") != run_id:
            raise HTTPException(status_code=400, detail="该运行不是当前活动运行")
        action_map = {
            "pause": engine.pause_training, "resume": engine.resume_training,
            "step": engine.step_training, "abort": engine.abort_training,
            "abort_save": engine.abort_and_save_training,
        }
        if action not in action_map:
            raise HTTPException(status_code=400, detail=f"未知操作: {action}")
        ok = action_map[action]()
        if not ok:
            raise HTTPException(status_code=400, detail=f"无法执行 {action}")
        return {"status": engine.get_state()["status"]}

    # ── Datasets ──
    @router.get("/datasets")
    async def list_datasets():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM datasets ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return {"datasets": [dict(r) for r in rows]}

    @router.get("/datasets/{ds_id}")
    async def get_dataset(ds_id: int):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM datasets WHERE id=?", (ds_id,))
        row = cursor.fetchone()
        conn.close()
        if not row: raise HTTPException(status_code=404)
        return dict(row)

    @router.get("/datasets/{ds_id}/preview")
    async def preview_dataset(ds_id: int, n: int = 5):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM datasets WHERE id=?", (ds_id,))
        row = cursor.fetchone()
        conn.close()
        if not row: raise HTTPException(status_code=404)
        path = row["storage_path"]
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="文件不存在")
        samples = []
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= n: break
                if line.strip():
                    try: samples.append(json.loads(line))
                    except: samples.append(line.strip())
        return {"samples": samples, "name": row["name"], "count": row["sample_count"]}

    @router.delete("/datasets/{ds_id}")
    async def delete_dataset(ds_id: int):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM datasets WHERE id=?", (ds_id,))
        conn.commit(); conn.close()
        return {"status": "success"}

    # ── Recommendations ──
    @router.get("/recommended-datasets")
    async def get_recommended():
        return {"datasets": RECOMMENDED_DATASETS}


# ── Shared constants ──
RECOMMENDED_DATASETS = [
    {"repo_id": "fuguowen/alpaca_zh", "name": "Alpaca 中文指令", "desc": "中文指令微调数据集", "size": "~50MB", "splits": ["train"]},
    {"repo_id": "tatsu-lab/alpaca", "name": "Alpaca 英文指令", "desc": "经典英文指令微调数据集", "size": "~25MB", "splits": ["train"]},
    {"repo_id": "databricks/databricks-dolly-15k", "name": "Dolly 15K", "desc": "Databricks 通用指令数据集", "size": "~10MB", "splits": ["train"]},
    {"repo_id": "Salesforce/wikitext", "name": "WikiText-103", "desc": "语言建模基准数据集", "size": "~180MB", "splits": ["train","validation","test"], "config": "wikitext-103-raw-v1"},
]


def get_llamacpp_manager():
    """Lazy import for llama.cpp manager (from main project)."""
    try:
        from core.llamacpp_manager import get_llamacpp_manager as _get
        return _get()
    except ImportError:
        return None
