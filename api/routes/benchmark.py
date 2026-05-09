"""
Benchmark evaluation routes — dataset-backed model evaluation.
"""
import os
import json
import time as _time
import threading
import asyncio
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import requests

router = APIRouter(prefix="/api/training", tags=["benchmark"])


# ── Proxy helper (respects HTTP_PROXY / HTTPS_PROXY env vars) ──

def _get_proxies():
    """Get proxy from env vars, system settings, or config."""
    proxies = {}
    # 1. Check environment variables
    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        val = os.environ.get(var, "")
        if val:
            proxies["http"] = proxies["https"] = val
            break
    # 2. Check system proxy settings (works on Windows/macOS/Linux)
    if not proxies:
        try:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            for k in ["https", "http"]:
                if k in sys_proxies and sys_proxies[k]:
                    proxies["http"] = proxies["https"] = sys_proxies[k]
                    break
        except Exception:
            pass
    # 3. Check config for proxy setting
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
    """requests.get with proxy support."""
    proxies = _get_proxies()
    if proxies:
        kwargs.setdefault("proxies", proxies)
    return requests.get(url, **kwargs)


# ── shared state (imported from server at registration time) ──

_db_path = None
_llamacpp_download_state = None
_training_install_state = None
_broadcast = None
_get_training_engine = None
_get_llamacpp_manager = None
_load_config = None


def init_benchmark_routes(db_path, download_state, install_state, broadcast_fn,
                          get_engine, get_llamacpp, load_config):
    global _db_path, _llamacpp_download_state, _training_install_state
    global _broadcast, _get_training_engine, _get_llamacpp_manager, _load_config
    _db_path = db_path
    _llamacpp_download_state = download_state
    _training_install_state = install_state
    _broadcast = broadcast_fn
    _get_training_engine = get_engine
    _get_llamacpp_manager = get_llamacpp
    _load_config = load_config


# ── Benchmark Registry ──

BENCHMARK_REGISTRY = {
    "mmlu": {
        "name": "MMLU (Massive Multitask Language Understanding)",
        "desc": "57 subjects x 100+ questions, multiple choice. Dataset: cais/mmlu, ~14K Q",
        "hf_repo": "cais/mmlu",
        "sample_size": 100,
        "scoring": "multiple_choice",
        "subjects": ["abstract_algebra","anatomy","astronomy","business_ethics","clinical_knowledge",
                     "college_biology","college_chemistry","college_computer_science","college_mathematics",
                     "college_physics","computer_security","conceptual_physics","econometrics",
                     "electrical_engineering","elementary_mathematics","high_school_biology",
                     "high_school_chemistry","high_school_computer_science","high_school_mathematics",
                     "high_school_physics","human_sexuality","international_law","jurisprudence",
                     "logical_fallacies","machine_learning","management","marketing","medical_genetics",
                     "miscellaneous","moral_disputes","philosophy","prehistory","professional_accounting",
                     "professional_psychology","public_relations","security_studies","sociology",
                     "us_foreign_policy","virology","world_religions"],
    },
    "hellaswag": {
        "name": "HellaSwag (Commonsense NLI)",
        "desc": "Commonsense reasoning. Dataset: Rowan/hellaswag, ~10K Q",
        "hf_repo": "Rowan/hellaswag",
        "sample_size": 50,
        "scoring": "multiple_choice",
    },
    "hle": {
        "name": "HLE (Humanity's Last Exam)",
        "desc": "Hard multi-discipline reasoning. Dataset: cais/hle, ~3K Q",
        "hf_repo": "cais/hle",
        "sample_size": 20,
        "scoring": "keyword_match",
    },
    "swe_bench": {
        "name": "SWE-bench (Software Engineering)",
        "desc": "Real GitHub issue fixes. Dataset: princeton-nlp/SWE-bench_Verified, ~2K Q",
        "hf_repo": "princeton-nlp/SWE-bench_Verified",
        "sample_size": 10,
        "scoring": "keyword_match",
    },
    "latency": {
        "name": "Latency Test (TTFT & TPS)",
        "desc": "Time-to-first-token and tokens/sec measurement",
        "sample_size": 5,
        "scoring": "latency_only",
    },
}


# ── Dataset Loading ──

def _load_benchmark_dataset(benchmark_type: str, sample_size: int = None) -> list:
    info = BENCHMARK_REGISTRY.get(benchmark_type, {})
    hf_repo = info.get("hf_repo", "")
    n = sample_size or info.get("sample_size", 30)

    bench_dir = os.path.join(os.path.dirname(_db_path), "benchmarks", benchmark_type)
    os.makedirs(bench_dir, exist_ok=True)

    cache_file = os.path.join(bench_dir, "questions.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                questions = json.load(f)
            if len(questions) >= n:
                return questions[:n]
        except Exception:
            pass

    if not hf_repo:
        return [
            {"question": "1+1=?", "answer": "2", "subject": "latency"},
            {"question": "Capital of France?", "answer": "Paris", "subject": "latency"},
            {"question": "Translate: Hello World", "answer": "Hello World", "subject": "latency"},
            {"question": "2^10 = ?", "answer": "1024", "subject": "latency"},
            {"question": "What year is it?", "answer": "2026", "subject": "latency"},
        ][:n]

    questions = []
    try:
        if benchmark_type == "mmlu":
            questions = _download_mmlu_subset(bench_dir, n, info)
        elif benchmark_type == "hellaswag":
            questions = _download_hellaswag_subset(n)
        elif benchmark_type == "hle":
            questions = _download_hle_subset(n)
        elif benchmark_type == "swe_bench":
            questions = _download_swebench_subset(n)
    except Exception as e:
        print(f"[Benchmark] Download failed for {benchmark_type}: {e}")

    if not questions:
        return []

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(questions, f, ensure_ascii=False)
    except Exception:
        pass

    return questions[:n]


def _download_mmlu_subset(bench_dir: str, n: int, info: dict) -> list:
    import csv, io as _io
    questions = []
    subjects = info.get("subjects", [])
    per_subject = max(n // min(len(subjects), 10), 1)

    for subject in subjects[:10]:
        try:
            url = f"https://huggingface.co/datasets/cais/mmlu/resolve/main/data/test/{subject}_test.csv"
            resp = _get(url, timeout=30)
            if resp.status_code == 200:
                reader = csv.reader(_io.StringIO(resp.text))
                next(reader, None)
                for row in reader:
                    if len(row) >= 6:
                        questions.append({
                            "question": row[0],
                            "choices": [row[1], row[2], row[3], row[4]],
                            "answer": row[5].strip(),
                            "subject": subject,
                        })
                    if len(questions) >= n:
                        return questions[:n]
        except Exception as e:
            print(f"[Benchmark] MMLU {subject}: {e}")
    return questions[:n]


def _download_hellaswag_subset(n: int) -> list:
    questions = []
    try:
        url = "https://huggingface.co/datasets/Rowan/hellaswag/resolve/main/data/hellaswag_val.jsonl"
        resp = _get(url, stream=True, timeout=120)
        if resp.status_code == 200:
            import random
            lines = [l for l in resp.iter_lines(decode_unicode=True) if l and l.strip()]
            random.shuffle(lines)
            for line in lines[:n]:
                data = json.loads(line)
                questions.append({
                    "question": data.get("ctx", ""),
                    "choices": data.get("endings", []),
                    "answer": str(data.get("label", 0)),
                    "subject": "commonsense",
                })
        else:
            print(f"[Benchmark] HellaSwag: HTTP {resp.status_code} from {url}")
    except Exception as e:
        print(f"[Benchmark] HellaSwag: {e}")
    return questions[:n]


def _download_hle_subset(n: int) -> list:
    questions = []
    try:
        url = "https://huggingface.co/datasets/cais/hle/resolve/main/data/test/hle_test.jsonl"
        resp = _get(url, stream=True, timeout=120)
        if resp.status_code == 200:
            import random
            lines = [l for l in resp.iter_lines(decode_unicode=True) if l and l.strip()]
            random.shuffle(lines)
            for line in lines[:n]:
                data = json.loads(line)
                questions.append({
                    "question": data.get("question", data.get("prompt", "")),
                    "answer": data.get("answer", data.get("solution", "")),
                    "subject": data.get("subject", "general"),
                })
        else:
            print(f"[Benchmark] HLE: HTTP {resp.status_code} from {url}")
    except Exception as e:
        print(f"[Benchmark] HLE: {e}")
    return questions[:n]


def _download_swebench_subset(n: int) -> list:
    questions = []
    try:
        url = "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/main/data/test-00000-of-00001.parquet"
        resp = _get(url, timeout=120)
        if resp.status_code == 200:
            try:
                import pyarrow.parquet as pq
                import io as _io
                import random
                table = pq.read_table(_io.BytesIO(resp.content))
                indices = list(range(len(table)))
                random.shuffle(indices)
                for i in indices[:n]:
                    row = table.column("problem_statement")[i].as_py()
                    patch = table.column("patch")[i].as_py() if "patch" in table.column_names else ""
                    questions.append({
                        "question": str(row)[:2000],
                        "answer": str(patch)[:500],
                        "subject": "software_engineering",
                    })
            except ImportError:
                pass
    except Exception as e:
        print(f"[Benchmark] SWE-bench: {e}")
    return questions[:n]


# ── Checkpoint helpers ──

def _checkpoint_dir():
    d = os.path.join(os.path.dirname(_db_path), "benchmarks", "checkpoints")
    os.makedirs(d, exist_ok=True)
    return d


def _checkpoint_path(model_id, bench_types):
    slug = model_id.replace("/","_").replace("\\","_")[:40]
    key = "_".join(sorted(bench_types))
    return os.path.join(_checkpoint_dir(), f"{slug}_{key}.json")


def _save_checkpoint(model_id, bench_types, completed, results,
                     total_time, total_tokens, questions_cache):
    try:
        with open(_checkpoint_path(model_id, bench_types), "w", encoding="utf-8") as f:
            json.dump({
                "model_id": model_id,
                "benchmark_types": bench_types,
                "completed": completed,   # {btype: count_done}
                "results": results,
                "total_time": total_time,
                "total_tokens": total_tokens,
                "questions_cache": questions_cache,
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Benchmark] Checkpoint save failed: {e}")


def _load_checkpoint(model_id, bench_types):
    path = _checkpoint_path(model_id, bench_types)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _clear_checkpoint(model_id, bench_types):
    path = _checkpoint_path(model_id, bench_types)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


# ── API Endpoints ──

class PreDownloadRequest(BaseModel):
    benchmark_type: str
    sample_size: Optional[int] = None


@router.post("/benchmark/pre-download")
async def pre_download_benchmark(req: PreDownloadRequest):
    """Pre-download benchmark dataset into the eval cache (benchmarks/<type>/questions.json)."""
    info = BENCHMARK_REGISTRY.get(req.benchmark_type)
    if not info:
        raise HTTPException(status_code=400, detail=f"未知的测评类型: {req.benchmark_type}")
    try:
        questions = _load_benchmark_dataset(req.benchmark_type, req.sample_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败: {e}")
    if not questions:
        raise HTTPException(status_code=500, detail="下载完成但未获取到题目，请检查网络或安装依赖: pip install datasets pyarrow pandas")
    return {
        "status": "ok",
        "count": len(questions),
        "message": f"{info['name']} 预下载完成，缓存 {len(questions)} 道题目"
    }


@router.get("/benchmark/cache-status")
async def get_benchmark_cache_status():
    """Check which benchmark datasets are already cached."""
    bench_dir = os.path.join(os.path.dirname(_db_path), "benchmarks")
    status = {}
    for btype, info in BENCHMARK_REGISTRY.items():
        cache_file = os.path.join(bench_dir, btype, "questions.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                status[btype] = {"cached": True, "count": len(data) if isinstance(data, list) else 0}
            except Exception:
                status[btype] = {"cached": False, "count": 0}
        else:
            status[btype] = {"cached": False, "count": 0}
    return {"status": "ok", "caches": status}


class BenchmarkRequest(BaseModel):
    model_id: str
    model_source: str = "online"
    benchmark_types: List[str] = ["mmlu", "latency"]
    resume: bool = False


@router.get("/benchmark/checkpoint-status")
async def get_checkpoint_status():
    """List all saved checkpoints for resume."""
    ckpt_dir = _checkpoint_dir()
    checkpoints = []
    if os.path.exists(ckpt_dir):
        for fname in os.listdir(ckpt_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(ckpt_dir, fname), "r", encoding="utf-8") as f:
                    ckpt = json.load(f)
                total_q = {bt: len(qs) for bt, qs in ckpt.get("questions_cache", {}).items()}
                done_q = ckpt.get("completed", {})
                checkpoints.append({
                    "model_id": ckpt.get("model_id", ""),
                    "benchmark_types": ckpt.get("benchmark_types", []),
                    "completed": done_q,
                    "total_questions": total_q,
                    "progress": {bt: f"{done_q.get(bt, 0)}/{total_q.get(bt, 1)}" for bt in total_q},
                })
            except Exception:
                pass
    return {"checkpoints": checkpoints}


class BenchmarkModelInfo(BaseModel):
    id: str
    name: str
    source: str


@router.get("/benchmarks")
async def list_benchmarks():
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM benchmark_results ORDER BY created_at DESC LIMIT 30")
    rows = cursor.fetchall()
    conn.close()
    return {"benchmarks": [dict(r) for r in rows]}


@router.get("/benchmarks/{bench_id}")
async def get_benchmark_detail(bench_id: int):
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM benchmark_results WHERE id=?", (bench_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.delete("/benchmarks/{bench_id}")
async def delete_benchmark(bench_id: int):
    conn = sqlite3.connect(_db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM benchmark_results WHERE id=?", (bench_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}


@router.post("/benchmark")
async def run_benchmark(req: BenchmarkRequest):
    """Run benchmark tasks against a model (in background thread for real-time progress)."""
    engine = _get_training_engine()
    if engine.get_state()["active"]:
        raise HTTPException(status_code=409, detail="Training in progress")

    model_id = req.model_id

    if "llamacpp/" in model_id:
        lm = _get_llamacpp_manager()
        if not lm.is_running():
            model_filename = model_id.replace("llamacpp/", "")
            lm.start(model_filename)
            for _ in range(120):
                if lm.is_running():
                    break
                await asyncio.sleep(0.5)
            if not lm.is_running():
                raise HTTPException(status_code=500, detail="llama-server failed to start")

    # Load trained model from disk
    trained_model_info = None  # (model, tokenizer, device)
    if "trained/" in model_id:
        run_id_str = model_id.replace("trained/run_", "")
        try:
            run_id = int(run_id_str)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的训练模型ID: {model_id}")
        conn_tm = sqlite3.connect(_db_path)
        conn_tm.row_factory = sqlite3.Row
        cur_tm = conn_tm.cursor()
        cur_tm.execute("SELECT * FROM training_runs WHERE id=?", (run_id,))
        row_tm = cur_tm.fetchone()
        conn_tm.close()
        if not row_tm or not row_tm["checkpoint_dir"]:
            raise HTTPException(status_code=404, detail="训练模型不存在或路径为空")
        save_dir = row_tm["checkpoint_dir"]
        if not os.path.isdir(save_dir):
            raise HTTPException(status_code=404, detail=f"模型目录不存在: {save_dir}")
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            tokenizer = AutoTokenizer.from_pretrained(save_dir, trust_remote_code=True)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            model = AutoModelForCausalLM.from_pretrained(
                save_dir, trust_remote_code=True,
                torch_dtype=torch.float16 if device.type == "cuda" else torch.float32,
            ).to(device)
            model.eval()
            trained_model_info = (model, tokenizer, device)
            _broadcast({"type": "benchmark_progress", "task": "system", "stage": "loaded",
                         "label": f"已加载训练模型: {row_tm['name']}", "progress": 0, "active": True})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"加载训练模型失败: {e}")

    # ── Load or resume checkpoint ──
    checkpoint = _load_checkpoint(model_id, req.benchmark_types) if req.resume else None
    if req.resume and not checkpoint:
        raise HTTPException(status_code=400, detail='没有可恢复的测评记录，请点击"开始测评"重新开始')
    questions_cache = checkpoint.get("questions_cache", {}) if checkpoint else {}

    all_questions = []
    for btype in req.benchmark_types:
        info = BENCHMARK_REGISTRY.get(btype, {})
        if not info:
            continue
        if btype in questions_cache:
            questions = questions_cache[btype]
        else:
            questions = _load_benchmark_dataset(btype)
            if not questions:
                _broadcast({
                    "type": "benchmark_progress",
                    "task": btype,
                    "stage": "error",
                    "label": f"{info.get('name', btype)}: 下载失败，已跳过",
                    "progress": 0,
                    "active": True
                })
                continue
            questions_cache[btype] = questions
        all_questions.append((btype, info, questions))
        _broadcast({
            "type": "benchmark_progress",
            "task": btype,
            "stage": "loaded",
            "label": f"{info.get('name', btype)}: {len(questions)} 题已就绪",
            "progress": 0,
            "active": True
        })

    if not all_questions:
        raise HTTPException(status_code=400, detail="All benchmarks failed to load")

    result_holder = {}

    def run_benchmark_thread():
        nonlocal checkpoint
        # Init from checkpoint if resuming
        if checkpoint:
            results = checkpoint.get("results", [])
            total_q = checkpoint.get("total_questions_done", 0)
            total_time = checkpoint.get("total_time", 0.0)
            total_tokens = checkpoint.get("total_tokens", 0)
            completed = checkpoint.get("completed", {})
        else:
            results = []
            total_q = 0
            total_time = 0.0
            total_tokens = 0
            completed = {}

        for btype, info, questions in all_questions:
            benchmark_name = info.get("name", btype)
            scoring = info.get("scoring", "keyword_match")
            n = len(questions)
            start_idx = completed.get(btype, 0)

            # Restore or init per-benchmark counters from checkpoint
            if checkpoint:
                existing = [r for r in results if r.get("type") == btype]
                if existing:
                    prev = existing[0]
                    task_results = prev.get("_task_results", [])
                    correct = prev.get("correct", 0)
                else:
                    task_results = []
                    correct = 0
            else:
                task_results = []
                correct = 0

            for idx in range(start_idx, n):
                item = questions[idx]
                total_q += 1
                prompt = item.get("question", "")
                choices = item.get("choices", [])
                if choices:
                    labels = ["A","B","C","D","E","F"][:len(choices)]
                    prompt += "\n" + "\n".join(f"{l}) {c}" for l, c in zip(labels, choices))
                    prompt += "\nAnswer with the letter only."

                t0 = _time.time()
                try:
                    if trained_model_info is not None:
                        # Direct HuggingFace inference for trained models
                        import torch
                        model, tokenizer, device = trained_model_info
                        inputs = tokenizer(prompt, return_tensors="pt", truncation=True,
                                          max_length=2048).to(device)
                        with torch.no_grad():
                            outputs = model.generate(
                                **inputs, max_new_tokens=256, do_sample=False,
                                pad_token_id=tokenizer.pad_token_id,
                            )
                        answer = tokenizer.decode(
                            outputs[0][inputs.input_ids.shape[1]:],
                            skip_special_tokens=True)
                        elapsed = (_time.time() - t0) * 1000
                        tok_count = outputs.shape[1] - inputs.input_ids.shape[1]
                        total_time += elapsed
                        total_tokens += tok_count
                    else:
                        from core.llm_client import LLMClient
                        client = LLMClient(default_model=model_id)
                        response, _ = client.chat(messages=[{"role": "user", "content": prompt}])
                        answer = response.choices[0].message.content if response else ""
                        elapsed = (_time.time() - t0) * 1000
                        usage = getattr(response, "usage", None)
                        tok_count = usage.total_tokens if usage else (len(answer)//3)
                        total_time += elapsed
                        total_tokens += tok_count

                    expected = str(item.get("answer","")).strip()
                    score = 0
                    ans = answer.strip().upper()

                    if scoring == "multiple_choice":
                        fl = ans[0] if ans else ""
                        if fl == expected.upper(): score = 1.0
                        elif expected.upper() in ans[:5]: score = 0.8
                        correct += 1 if score >= 0.5 else 0
                    elif scoring == "latency_only":
                        score = 1.0; correct += 1
                    else:
                        if expected and expected.lower() in answer.lower(): score = 0.8
                        kw = sum(1 for w in expected.lower().split() if w in answer.lower())
                        if expected: score = max(score, min(kw/max(len(expected.split()),1), 1.0)*0.7)
                        correct += 1 if score >= 0.4 else 0

                    task_results.append({
                        "idx": idx, "question": prompt, "choices": item.get("choices", []),
                        "answer": answer, "expected": expected,
                        "score": round(score,2), "scoring": scoring,
                        "latency_ms": round(elapsed,1), "tokens": tok_count,
                        "subject": item.get("subject","")
                    })
                except Exception as e:
                    task_results.append({
                        "idx": idx, "question": prompt, "choices": item.get("choices", []),
                        "answer": "", "expected": str(item.get("answer","")),
                        "score": 0, "scoring": scoring,
                        "latency_ms": 0, "tokens": 0, "error": str(e)[:200],
                        "subject": item.get("subject","")
                    })

                # Save checkpoint after every question
                completed[btype] = idx + 1
                # Rebuild current results list
                cur_results = []
                for b2, info2, qs2 in all_questions:
                    bname2 = info2.get("name", b2)
                    if b2 == btype:
                        # In-progress benchmark — use live state
                        n2 = len(qs2)
                        done2 = completed.get(b2, 0)
                        acc2 = correct / max(done2, 1)
                        cur_results.append({
                            "type": b2, "name": bname2,
                            "accuracy": round(acc2, 3), "num_questions": n2,
                            "correct": correct, "completed": done2,
                            "_task_results": task_results,
                        })
                    else:
                        # Other benchmarks — carry from previous or checkpoint
                        prev_r = [r for r in results if r.get("type") == b2]
                        if prev_r:
                            cur_results.append(prev_r[0])
                _save_checkpoint(model_id, req.benchmark_types,
                                 completed, cur_results, total_time, total_tokens,
                                 questions_cache)

                _broadcast({
                    "type": "benchmark_progress", "task": btype, "stage": "running",
                    "label": f"{benchmark_name}: {idx+1}/{n} | {prompt[:60]}",
                    "progress": (idx+1)/n, "active": True
                })

            # Build final result entry for this benchmark
            accuracy = correct / max(n, 1)
            subject_scores = {}
            for r in task_results:
                subj = r.get("subject","general") or "general"
                subject_scores.setdefault(subj, {"correct":0,"total":0})
                subject_scores[subj]["total"] += 1
                if r.get("score",0) >= 0.5: subject_scores[subj]["correct"] += 1

            entry = {
                "type": btype, "name": benchmark_name,
                "accuracy": round(accuracy,3), "num_questions": n, "correct": correct,
                "subjects": {k: {"accuracy": round(v["correct"]/max(v["total"],1),2), "n": v["total"]}
                             for k,v in sorted(subject_scores.items())},
                "details": task_results
            }
            # Remove internal field before saving
            entry.pop("_task_results", None)
            # Replace or append
            replaced = False
            for i, r in enumerate(results):
                if r.get("type") == btype:
                    results[i] = entry
                    replaced = True
                    break
            if not replaced:
                results.append(entry)

        avg_latency = total_time / max(total_q, 1)
        tps = total_tokens / (total_time/1000) if total_time > 0 else 0
        result_holder["data"] = {
            "model_id": model_id, "results": results,
            "avg_latency_ms": round(avg_latency,1),
            "tokens_per_second": round(tps,1),
            "total_questions": total_q
        }

        # Persist final results
        try:
            conn = sqlite3.connect(_db_path)
            conn.cursor().execute(
                "INSERT INTO benchmark_results (model_id,model_source,benchmark_type,metrics_json,num_questions,avg_latency_ms,tokens_per_second) VALUES (?,?,?,?,?,?,?)",
                (model_id, req.model_source, ",".join(req.benchmark_types),
                 json.dumps(results, ensure_ascii=False), total_q,
                 round(avg_latency,1), round(tps,1)))
            conn.commit()
            result_holder["id"] = conn.cursor().lastrowid
            conn.close()
        except Exception:
            pass

        try:
            results_dir = os.path.join(os.path.dirname(_db_path), "benchmarks", "results")
            os.makedirs(results_dir, exist_ok=True)
            ts = _time.strftime("%Y%m%d_%H%M%S")
            slug = model_id.replace("/","_").replace("\\","_")[:40]
            with open(os.path.join(results_dir, f"{ts}_{slug}.json"), "w", encoding="utf-8") as f:
                json.dump(result_holder["data"], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # Clear checkpoint on success
        _clear_checkpoint(model_id, req.benchmark_types)

        _broadcast({"type":"benchmark_complete","benchmark_id":result_holder.get("id",0),
                     "model_id":model_id,"results":results,"avg_latency_ms":round(avg_latency,1),
                     "tokens_per_second":round(tps,1),"active":False})

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_benchmark_thread)
    return result_holder.get("data", {"error":"Benchmark failed"})


# ── Model Listing for Benchmark ──

@router.get("/all-models")
async def list_all_models():
    """List all available models (online + local) with proper litellm prefixes."""
    models = []
    config = _load_config()
    seen = set()

    def add_model(mid, label, source="online"):
        if mid and mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "name": label, "source": source})

    dm = config.get("default_model","")
    if dm: add_model(dm, f"{dm} (默认)", "online")
    for fb in config.get("fallback_models",[]):
        add_model(fb.strip(), f"{fb.strip()} (备用)", "online")

    add_model("moonshot/kimi-latest", "moonshot/kimi-latest (Kimi)", "online")
    add_model("deepseek/deepseek-chat", "deepseek/deepseek-chat (DeepSeek)", "online")
    add_model("deepseek/deepseek-reasoner", "deepseek/deepseek-reasoner (R1)", "online")
    add_model("zai/glm-4.5", "zai/glm-4.5 (GLM)", "online")
    add_model("minimax/MiniMax-M2.1", "minimax/MiniMax-M2.1 (MiniMax)", "online")
    add_model("gpt-4o", "gpt-4o (OpenAI)", "online")
    add_model("gpt-4o-mini", "gpt-4o-mini (OpenAI)", "online")
    add_model("gemini/gemini-2.5-pro-preview-05-06", "gemini/gemini-2.5-pro (Google)", "online")
    add_model("claude-3-5-sonnet-20240620", "claude-3-5-sonnet (Anthropic)", "online")

    lm = _get_llamacpp_manager()
    for f in lm.list_models():
        mid = f"llamacpp/{f}"
        if mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "name": f"🦙 {f} (本地 GGUF)", "source": "local"})

    # Include trained/finetuned models
    try:
        conn = sqlite3.connect(_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, checkpoint_dir FROM training_runs "
            "WHERE status='completed' AND checkpoint_dir IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 20"
        )
        rows = cursor.fetchall()
        for row in rows:
            ckpt = row["checkpoint_dir"]
            if ckpt and os.path.isdir(ckpt):
                mid = f"trained/run_{row['id']}"
                if mid not in seen:
                    seen.add(mid)
                    models.append({
                        "id": mid,
                        "name": f"🎓 {row['name']} (训练模型)",
                        "source": "trained",
                    })
        conn.close()
        if rows:
            print(f"[Benchmark] Loaded {len(rows)} trained models for benchmark list")
    except Exception as e:
        print(f"[Benchmark] Failed to list trained models: {e}")

    return {"models": models}
