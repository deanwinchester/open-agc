"""
Training Engine — orchestrates model training with pause/step/resume hooks.
Runs in a background thread and broadcasts progress via WebSocket.
"""
import threading
import time
import sys
import os
import subprocess
import importlib

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"


def _ensure_training_deps():
    """
    Auto-install missing training dependencies at server startup.
    Installs packages one-by-one so a single failure doesn't block others.
    Also detects corrupted packages (importable at top-level but broken internally).
    """
    required = {
        "sklearn": "scikit-learn>=1.0.0",
        "torch": "torch>=2.1.0",
        "transformers": "transformers>=4.35.0",
        "peft": "peft>=0.6.0",
        "accelerate": "accelerate>=0.24.0",
        "datasets": "datasets>=2.14.0",
        "sentencepiece": "sentencepiece>=0.1.99",
    }

    # Deep import checks: verify internal submodules to catch corrupted installs
    deep_checks = {
        "sklearn": ["sklearn.utils", "sklearn.base", "sklearn.metrics"],
        "transformers": ["transformers.AutoModel"],
    }

    def _is_package_ok(mod_name):
        """Check if package imports AND its critical submodules work."""
        try:
            importlib.import_module(mod_name)
        except (ImportError, ModuleNotFoundError):
            return False, "not installed"
        # Deep check
        for sub in deep_checks.get(mod_name, []):
            try:
                parts = sub.split(".")
                if len(parts) == 2:
                    parent = importlib.import_module(parts[0])
                    getattr(parent, parts[1])
                else:
                    importlib.import_module(sub)
            except (ImportError, ModuleNotFoundError, AttributeError) as e:
                return False, f"broken ({sub}: {e})"
        return True, "ok"

    missing = {}
    broken = {}
    for mod_name, pip_spec in required.items():
        ok, reason = _is_package_ok(mod_name)
        if not ok:
            if "broken" in reason:
                broken[mod_name] = pip_spec
                print(f"[TrainingEngine] {mod_name} is corrupted: {reason}")
            else:
                missing[mod_name] = pip_spec

    if not missing and not broken:
        return True  # All deps already available and healthy

    all_to_install = {**missing, **broken}
    print(f"[TrainingEngine] Missing training deps: {', '.join(missing.keys()) or '(none)'}, "
          f"broken: {', '.join(broken.keys()) or '(none)'}, auto-installing...")

    for mod_name, pip_spec in all_to_install.items():
        is_broken = mod_name in broken
        print(f"[TrainingEngine]   installing {pip_spec} {'(repair)' if is_broken else ''}...")
        try:
            cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
            if is_broken:
                # Force reinstall for corrupted packages
                cmd.append("--force-reinstall")
            cmd.append(pip_spec)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                # Check if it's actually importable despite pip exit code
                importlib.invalidate_caches()
                ok, reason = _is_package_ok(mod_name)
                if ok:
                    print(f"[TrainingEngine]   [OK] {mod_name} (pip exit {result.returncode} but works)")
                else:
                    stderr_short = (result.stderr or result.stdout or "")[-300:]
                    print(f"[TrainingEngine]   [FAIL] {mod_name}: {reason} | pip: {stderr_short}")
            else:
                print(f"[TrainingEngine]   [OK] {mod_name}")
        except subprocess.TimeoutExpired:
            print(f"[TrainingEngine]   [FAIL] {mod_name} timeout")
        except Exception as e:
            print(f"[TrainingEngine]   [FAIL] {mod_name}: {e}")

    # Re-verify after installation
    importlib.invalidate_caches()
    still_bad = []
    for mod_name in required:
        ok, reason = _is_package_ok(mod_name)
        if not ok:
            still_bad.append(f"{mod_name}({reason})")

    if still_bad:
        print(f"[TrainingEngine] WARNING: still broken: {', '.join(still_bad)}")
        return False
    else:
        print("[TrainingEngine] All training deps ready.")
        return True



# Run auto-install at import time (i.e. server startup)
_training_available = _ensure_training_deps()

if _training_available:
    import torch
    import transformers
    import peft

# datasets is optional — dataset download works via HTTP fallback
_datasets_available = False
try:
    import datasets  # noqa: F401
    _datasets_available = True
except ImportError:
    pass

def _collate_batch(batch, pad_token_id):
    """Dynamic padding collate: pads each batch to its longest sequence."""
    import torch
    max_len = max(item["input_ids"].size(0) for item in batch)
    input_ids_list, attention_mask_list, labels_list = [], [], []
    for item in batch:
        ids = item["input_ids"]
        am = item["attention_mask"]
        cur_len = ids.size(0)
        pad_len = max_len - cur_len
        if pad_len > 0:
            # Pad on the RIGHT for causal LM (model sees [tokens, PAD, PAD])
            ids = torch.cat([ids, torch.full((pad_len,), pad_token_id, dtype=ids.dtype)])
            am = torch.cat([am, torch.zeros(pad_len, dtype=am.dtype)])
        input_ids_list.append(ids)
        attention_mask_list.append(am)
        # Labels: clone input_ids, set PAD positions to -100 (ignored in loss)
        labels = ids.clone()
        labels[am == 0] = -100
        labels_list.append(labels)
    return {
        "input_ids": torch.stack(input_ids_list),
        "attention_mask": torch.stack(attention_mask_list),
        "labels": torch.stack(labels_list),
    }


class TokenChunkDataset(torch.utils.data.Dataset):
    """Token-based dataset: concatenates all text, splits into fixed-length chunks.

    No padding waste — every chunk is exactly `max_length` tokens.
    This is the standard approach for causal LM pre-training (GPT, LLaMA, etc.).

    One epoch = one pass through all concatenated tokens.
    """

    def __init__(self, filepath, tokenizer, max_length, progress_cb=None):
        import json
        self.max_length = max_length
        self.pad_token_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id

        # Read all text
        texts = []
        has_instruction = False
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if "instruction" in obj:
                        has_instruction = True
                        text = f"Instruction: {obj['instruction']}\n"
                        if obj.get("input"):
                            text += f"Input: {obj['input']}\n"
                        text += f"Output: {obj.get('output', '')}"
                    elif "text" in obj:
                        text = obj["text"]
                    elif "messages" in obj:
                        text = "\n".join(f"{m['role']}: {m['content']}" for m in obj["messages"])
                    else:
                        text = str(obj)
                    if text and len(text.strip()) > 10:
                        texts.append(text)
                except Exception:
                    pass

        if not texts:
            texts = ["The quick brown fox jumps over the lazy dog." * 10] * 100
            has_instruction = False

        self.has_instruction = has_instruction
        self.total_texts = len(texts)

        # Tokenize in batches
        eos_str = tokenizer.eos_token or " "
        batch_size = 100
        all_ids = []
        total_chars = 0
        report_interval = max(len(texts) // 20, 1)
        # Suppress length warnings: we are tokenizing for dataset prep, not inference
        old_max = getattr(tokenizer, "model_max_length", 1024)
        tokenizer.model_max_length = 10_000_000  # effectively unlimited

        print(f"[Dataset] Tokenizing {len(texts)} texts (batch size {batch_size})...")
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            batch_text = eos_str.join(batch)
            total_chars += len(batch_text)
            enc = tokenizer(batch_text, return_tensors="pt", truncation=False,
                           padding=False).input_ids[0]
            if enc.size(0) > 0:
                all_ids.append(enc)
            if progress_cb and (start % (report_interval * batch_size) == 0 or
                               start + batch_size >= len(texts)):
                progress_cb(start / max(len(texts), 1),
                           f"分词中: {start}/{len(texts)} 条 ({total_chars/1e6:.1f}M 字符)")

        tokenizer.model_max_length = old_max  # restore

        # Concatenate all token tensors
        tokens = torch.cat(all_ids, dim=0)
        print(f"[Dataset] Tokenized: {tokens.size(0)/1e6:.1f}M tokens from {len(texts)} texts")

        # Split into chunks
        total_len = tokens.size(0)
        chunk_count = total_len // max_length
        if chunk_count == 0:
            chunk_count = 1
        # Trim to exact multiple so all chunks are the same size
        tokens = tokens[:chunk_count * max_length]
        self.chunks = tokens.view(chunk_count, max_length)
        self.total_tokens = tokens.size(0)
        print(f"[Dataset] Created {chunk_count} chunks of {max_length} tokens each")

    def __len__(self):
        return self.chunks.size(0)

    def __getitem__(self, idx):
        ids = self.chunks[idx]
        return {
            "input_ids": ids,
            "labels": ids.clone(),
            "attention_mask": torch.ones(self.max_length, dtype=torch.long),
        }


def _collate_token_chunks(batch):
    """Simple stack collate for fixed-size token chunks (no padding needed)."""
    import torch
    return {
        "input_ids": torch.stack([b["input_ids"] for b in batch]),
        "labels": torch.stack([b["labels"] for b in batch]),
        "attention_mask": torch.stack([b["attention_mask"] for b in batch]),
    }


class JsonlDataset(torch.utils.data.Dataset):
    def __init__(self, filepath, tokenizer, max_length):
        import json
        self.data = []
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    obj = json.loads(line)
                    text = ""
                    if "instruction" in obj:
                        text = f"Instruction: {obj['instruction']}\n"
                        if obj.get("input"): text += f"Input: {obj['input']}\n"
                        text += f"Output: {obj.get('output', '')}"
                    elif "text" in obj:
                        text = obj["text"]
                    elif "messages" in obj:
                        text = "\n".join(f"{m['role']}: {m['content']}" for m in obj["messages"])
                    else:
                        text = str(obj)
                    if text:
                        self.data.append(text)
                except Exception:
                    pass
        if not self.data:
            # Fallback dummy data if empty
            self.data = ["The quick brown fox jumps over the lazy dog." * 10] * 100

        self.tokenizer = tokenizer
        self.max_length = max_length
        self.pad_token_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        text = self.data[idx]
        # Tokenize WITHOUT padding (collate function handles per-batch dynamic padding)
        tokens = self.tokenizer(text, truncation=True, max_length=self.max_length,
                                padding=False, return_tensors="pt")
        return {key: val.squeeze(0) for key, val in tokens.items()}



class TrainingEngine:
    """Manages the training lifecycle with batch-level pause/step control."""

    def __init__(self):
        self._state = {
            "active": False,
            "run_id": None,
            "status": "idle",
            "current_epoch": 0,
            "current_step": 0,
            "total_steps": 0,
            "current_loss": None,
            "current_grad_norm": None,
            "current_lr": None,
            "progress": 0.0,
        }
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._abort_flag = threading.Event()
        self._abort_and_save_flag = threading.Event()
        self._step_mode = False
        self._training_thread = None
        self._act_stats = {"mean": 0.0, "std": 0.0, "per_layer": []}
        self._broadcast_fn = None

    def set_broadcast_fn(self, fn):
        """Set the WebSocket broadcast function from server module."""
        self._broadcast_fn = fn

    def is_available(self) -> bool:
        return _training_available

    def get_state(self) -> dict:
        state_copy = dict(self._state)
        state_copy["act_stats"] = dict(self._act_stats)
        return state_copy

    def start_training(self, run_id: int, run_record: dict) -> bool:
        """Launch training in background thread."""
        if self._state["active"]:
            return False
        self._state["active"] = True
        self._state["run_id"] = run_id
        self._state["status"] = "running"
        self._state["progress"] = 0.0
        self._abort_flag.clear()
        self._abort_and_save_flag.clear()
        self._pause_event.set()
        self._step_mode = False
        self._training_thread = threading.Thread(
            target=self._training_loop,
            args=(run_id, run_record),
            daemon=True
        )
        self._training_thread.start()
        return True

    def pause_training(self) -> bool:
        """Pause after current batch completes."""
        if self._state["status"] != "running":
            return False
        self._pause_event.clear()
        self._state["status"] = "paused"
        return True

    def resume_training(self) -> bool:
        """Resume continuous execution."""
        if self._state["status"] != "paused":
            return False
        self._step_mode = False
        self._state["status"] = "running"
        self._pause_event.set()
        return True

    def step_training(self) -> bool:
        """Advance exactly one batch."""
        if self._state["status"] not in ("paused", "running"):
            return False
        self._step_mode = True
        self._state["status"] = "running"
        self._pause_event.set()
        return True

    def abort_training(self) -> bool:
        """Abort training after current batch (no save)."""
        if not self._state["active"]:
            return False
        self._abort_flag.set()
        self._pause_event.set()
        self._state["status"] = "aborted"
        self._state["active"] = False
        return True

    def abort_and_save_training(self) -> bool:
        """Abort training after current batch AND save the model checkpoint."""
        if not self._state["active"]:
            return False
        self._abort_and_save_flag.set()
        self._pause_event.set()
        self._state["status"] = "aborting_save"
        return True

    def get_batch_stats(self) -> dict:
        """Return activation stats for the most recent batch."""
        return dict(self._act_stats)

    def _broadcast(self, message: dict):
        if self._broadcast_fn:
            try:
                self._broadcast_fn(message)
            except Exception:
                pass

    def _build_model_from_config(self, config: dict):
        """Build a model from scratch based on model designer config.

        Uses the architecture builder registry for template architectures
        and the custom builder for component-assembled architectures.
        """
        arch = config.get("architecture", "gpt_decoder")
        mode = config.get("mode", "template")

        import math
        from core.model_architectures import get_builder

        # Normalize config keys for builder consumption
        params = dict(config)
        # Handle key name aliases
        if "num_heads" in params and "num_attention_heads" not in params:
            params["num_attention_heads"] = params["num_heads"]
        if "max_seq_len" in params and "max_seq_length" not in params:
            params["max_seq_length"] = params["max_seq_len"]

        builder = get_builder(arch)
        print(f"[TrainingEngine] Building model: arch={arch}, mode={mode}, "
              f"builder={builder.__class__.__name__}, "
              f"layers={params.get('num_layers', '?')}, "
              f"hidden={params.get('hidden_size', '?')}")
        return builder.build_model(params)

    def _training_loop(self, run_id: int, run_record: dict):
        """Custom training loop with pause/step hooks."""
        if not _training_available:
            self._state["status"] = "failed"
            self._state["active"] = False
            self._broadcast({
                "type": "training_error",
                "run_id": run_id,
                "error": "PyTorch/Transformers/PEFT not installed"
            })
            return

        try:
            params = run_record.get("training_params_json", "{}")
            if isinstance(params, str):
                import json
                params = json.loads(params)

            epochs = params.get("epochs", 3)
            batch_size = params.get("batch_size", 4)
            learning_rate = params.get("learning_rate", 2e-4)
            grad_accum = params.get("gradient_accumulation", 1)
            max_steps = params.get("max_steps", -1)
            optimizer_name = params.get("optimizer", "adamw")
            weight_decay = params.get("weight_decay", 0.01)
            warmup_steps = params.get("warmup_steps", 0)
            max_seq_len = params.get("max_seq_len", params.get("max_seq_length", 512))
            max_seq_length = max_seq_len # Alias for robustness
            val_ratio = params.get("val_ratio", 0.1)
            patience = params.get("patience", 3)

            model_config_id = run_record.get("model_config_id")
            base_model = run_record.get("base_model_id", "")
            base_source = run_record.get("base_model_source", "huggingface")
            is_scratch = bool(model_config_id and not base_model)

            print(f"[Training] Starting run {run_id}: scratch={is_scratch}, "
                  f"base={base_model or 'none'}, epochs={epochs}, batch={batch_size}")
            self._broadcast({"type": "training_progress", "run_id": run_id,
                             "epoch": 0, "step": 0, "global_step": 0,
                             "loss": 0, "grad_norm": 0, "progress": 0,
                             "status": "initializing"})

            from transformers import AutoTokenizer

            if is_scratch:
                # ── Train from scratch ──────────────────────────
                import sqlite3
                from core.paths import get_data_path
                db_path = get_data_path("chat_history.db")
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM model_configs WHERE id=?", (model_config_id,))
                config_row = cursor.fetchone()
                conn.close()

                if not config_row:
                    raise ValueError(f"模型配置 {model_config_id} 不存在")

                config_json = config_row["config_json"]
                if isinstance(config_json, str):
                    config_json = json.loads(config_json)
                config_json["architecture"] = config_row["architecture"]

                self._broadcast({"type": "training_progress", "run_id": run_id,
                                 "epoch": 0, "step": 0, "global_step": 0,
                                 "loss": 0, "grad_norm": 0, "progress": 0.05,
                                 "status": "building_model"})

                tokenizer = AutoTokenizer.from_pretrained("gpt2", trust_remote_code=True)
                if tokenizer.pad_token is None:
                    tokenizer.pad_token = tokenizer.eos_token
                
                # Sync vocab size from tokenizer
                config_json["vocab_size"] = len(tokenizer)
                model = self._build_model_from_config(config_json)
                
                if torch.cuda.is_available():
                    model = model.to("cuda")
                # Let AMP handle precision during training, keep model in float32 for better stability
            else:
                # ── Fine-tune pre-trained model ─────────────────
                # Resolve trained/run_X to actual checkpoint path
                load_path = base_model
                if base_model.startswith("trained/run_"):
                    try:
                        run_id_ref = int(base_model.replace("trained/run_", ""))
                        import sqlite3, os as _os
                        dp = get_data_path("chat_history.db")
                        _conn = sqlite3.connect(dp)
                        _conn.row_factory = sqlite3.Row
                        _cur = _conn.cursor()
                        _cur.execute("SELECT checkpoint_dir FROM training_runs WHERE id=?", (run_id_ref,))
                        _row = _cur.fetchone()
                        _conn.close()
                        if _row and _row["checkpoint_dir"] and _os.path.isdir(_row["checkpoint_dir"]):
                            load_path = _row["checkpoint_dir"]
                            self._broadcast({"type": "training_progress", "stage": "loading_model",
                                             "label": f"加载训练模型: {load_path}", "progress": 0.01, "active": True})
                    except Exception as e:
                        print(f"[Training] Failed to resolve trained model path: {e}")

                tokenizer = AutoTokenizer.from_pretrained(load_path, trust_remote_code=True)
                if tokenizer.pad_token is None:
                    tokenizer.pad_token = tokenizer.eos_token

                self._broadcast({"type": "training_progress", "run_id": run_id,
                                 "epoch": 0, "step": 0, "global_step": 0,
                                 "loss": 0, "grad_norm": 0, "progress": 0.02,
                                 "status": "loading_model"})

                from transformers import AutoModelForCausalLM
                model = AutoModelForCausalLM.from_pretrained(
                    load_path,
                    torch_dtype=torch.float32, # Keep in float32, use AMP for mixed precision
                    device_map="auto" if torch.cuda.is_available() else None,
                    trust_remote_code=True
                )

                # Apply LoRA
                lora_config = params.get("lora", {})
                if lora_config:
                    from peft import LoraConfig, get_peft_model, TaskType
                    peft_config = LoraConfig(
                        task_type=TaskType.CAUSAL_LM,
                        r=lora_config.get("rank", 8),
                        lora_alpha=lora_config.get("alpha", 16),
                        lora_dropout=lora_config.get("dropout", 0.05),
                        target_modules=lora_config.get("target_modules", ["q_proj", "v_proj"]),
                    )
                    model = get_peft_model(model, peft_config)
                    model.print_trainable_parameters()

            # Register forward hooks for activation stats
            self._register_activation_hooks(model)

            # Optimizer
            if optimizer_name == "adam":
                optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
            elif optimizer_name == "sgd":
                optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, weight_decay=weight_decay, momentum=0.9)
            else:
                optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)

            # Load Dataset
            dataset_id = run_record.get("dataset_id")
            from torch.utils.data import DataLoader, random_split
            train_loader = None
            val_loader = None
            use_token_chunks = False

            # Get model's max position limit
            max_pos = getattr(model.config, "n_positions", getattr(model.config, "max_position_embeddings", 2048))
            effective_max_len = min(max_seq_len, max_pos)

            if dataset_id:
                import sqlite3
                from core.paths import get_data_path
                db_path = get_data_path("chat_history.db")
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT storage_path FROM datasets WHERE id=?", (dataset_id,))
                ds_row = cursor.fetchone()
                conn.close()
                if ds_row and ds_row[0]:
                    ds_path = ds_row[0]
                    def _tokenize_progress(ratio, label):
                        self._broadcast({"type": "training_progress", "run_id": run_id,
                                         "stage": "loading_data",
                                         "label": label, "progress": 0.01 + ratio * 0.04,
                                         "active": True})
                    self._broadcast({"type": "training_progress", "run_id": run_id,
                                     "stage": "loading_data",
                                     "label": "正在加载并分词数据集...",
                                     "progress": 0.01, "active": True})
                    # Auto-detect: use TokenChunkDataset for plain text, JsonlDataset for instructions
                    probe_ds = TokenChunkDataset(ds_path, tokenizer, effective_max_len,
                                                 progress_cb=_tokenize_progress)
                    use_token_chunks = not probe_ds.has_instruction

                    if use_token_chunks:
                        # ── Token-based batching (standard for LM pre-training) ──
                        total_tokens = probe_ds.total_tokens
                        if val_ratio > 0 and len(probe_ds) > 10:
                            val_chunks = max(1, int(len(probe_ds) * val_ratio))
                            train_chunks = len(probe_ds) - val_chunks
                            train_dataset, val_dataset = random_split(probe_ds, [train_chunks, val_chunks])
                        else:
                            train_dataset = probe_ds
                            val_dataset = None

                        collate_fn = _collate_token_chunks
                        batch_msg = f"token-chunk 模式: {total_tokens/1e6:.1f}M tokens, {len(probe_ds)} chunks × {effective_max_len} tokens"
                    else:
                        # ── Sample-based batching (for instruction data) ──
                        full_dataset = JsonlDataset(ds_path, tokenizer, effective_max_len)
                        if val_ratio > 0 and len(full_dataset) > 10:
                            val_size = int(len(full_dataset) * val_ratio)
                            train_size = len(full_dataset) - val_size
                            train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])
                        else:
                            train_dataset = full_dataset
                            val_dataset = None
                        collate_fn = lambda b: _collate_batch(b, tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id)
                        batch_msg = f"sample 模式: {len(train_dataset)} samples"

                    self._broadcast({
                        "type": "training_progress", "run_id": run_id,
                        "stage": "preparing",
                        "label": batch_msg,
                        "progress": 0.02, "active": True
                    })
                    # Save validation set for later PPL evaluation
                    if val_dataset is not None:
                        try:
                            save_dir = os.path.join(get_data_path("models"), "trained", f"run_{run_id}")
                            os.makedirs(save_dir, exist_ok=True)
                            val_path = os.path.join(save_dir, "validation.jsonl")
                            with open(val_path, "w", encoding="utf-8") as vf:
                                for i in range(len(val_dataset)):
                                    chunk = val_dataset[i]
                                    vf.write(json.dumps({"text": chunk["input_ids"].tolist()}) + "\n")
                            self._update_run_db(run_id, checkpoint_dir=save_dir)
                        except Exception as e:
                            print(f"[Training] Failed to save validation set: {e}")

                    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True,
                                              collate_fn=collate_fn)
                    if val_dataset:
                        val_loader = DataLoader(val_dataset, batch_size=batch_size,
                                                collate_fn=collate_fn)

            if not train_loader:
                # Dummy loader
                dummy_ds = JsonlDataset("dummy", tokenizer, effective_max_len)
                train_loader = DataLoader(dummy_ds, batch_size=batch_size, shuffle=True,
                                          collate_fn=lambda b: _collate_batch(b, tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id))

            steps_per_epoch = len(train_loader)
            total_steps = epochs * steps_per_epoch
            if total_steps <= 0:
                total_steps = epochs
            if use_token_chunks:
                tokens_per_batch = batch_size * effective_max_len
                total_train_tokens = steps_per_epoch * tokens_per_batch * epochs
            else:
                tokens_per_batch = None
                total_train_tokens = None
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps)

            if warmup_steps > 0:
                from torch.optim.lr_scheduler import SequentialLR, LinearLR
                warmup_sched = LinearLR(optimizer, start_factor=0.01, total_iters=min(warmup_steps, total_steps))
                scheduler = SequentialLR(optimizer, schedulers=[warmup_sched, scheduler],
                                        milestones=[min(warmup_steps, total_steps)])

            self._broadcast({
                "type": "training_progress", "run_id": run_id,
                "stage": "preparing",
                "label": f"准备训练: {epochs} epochs × {steps_per_epoch} batches = {total_steps} steps" +
                         (f" | {total_train_tokens/1e6:.1f}M tokens" if total_train_tokens else ""),
                "progress": 0.03, "active": True
            })

            global_step = 0
            best_loss = float("inf")

            # Use modern torch.amp API
            use_amp = torch.cuda.is_available()
            # RTX 50-series supports bfloat16, which is much more stable than float16
            amp_dtype = torch.bfloat16 if (use_amp and torch.cuda.is_bf16_supported()) else torch.float16
            scaler = torch.amp.GradScaler('cuda') if (use_amp and amp_dtype == torch.float16) else None

            device = "cuda" if use_amp else "cpu"
            model.train()
            no_improve_epochs = 0
            break_save = False

            for epoch in range(epochs):
                if break_save:
                    break
                for batch_idx, batch in enumerate(train_loader):
                    self._pause_event.wait()
                    if self._abort_flag.is_set():
                        self._update_run_db(run_id, status="aborted", best_loss=best_loss)
                        self._broadcast({"type": "training_complete", "run_id": run_id, "best_loss": best_loss, "total_time": 0, "aborted": True})
                        return
                    if self._abort_and_save_flag.is_set():
                        break_save = True
                        break

                    optimizer.zero_grad()
                    input_ids = batch["input_ids"].to(device)
                    attention_mask = batch["attention_mask"].to(device)
                    labels = batch["labels"].to(device)

                    if use_amp:
                        with torch.amp.autocast(device_type="cuda", dtype=amp_dtype):
                            outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                            loss = outputs.loss
                        
                        if scaler:
                            scaler.scale(loss).backward()
                            scaler.unscale_(optimizer)
                            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                            scaler.step(optimizer)
                            scaler.update()
                        else:
                            # bfloat16 doesn't need scaling
                            loss.backward()
                            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                            optimizer.step()
                    else:
                        outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
                        loss = outputs.loss
                        loss.backward()
                        grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                        optimizer.step()

                    scheduler.step()

                    loss_val = loss.item()
                    grad_norm_val = grad_norm.item() if isinstance(grad_norm, torch.Tensor) else grad_norm
                    
                    # Update stats
                    act_mean = 0
                    act_std = 0
                    if self._act_buffer:
                        act_mean = sum(x["mean"] for x in self._act_buffer) / len(self._act_buffer)
                        act_std = sum(x["std"] for x in self._act_buffer) / len(self._act_buffer)
                        self._act_stats = {"mean": act_mean, "std": act_std, "per_layer": list(self._act_buffer)}
                        self._act_buffer.clear()

                    self._state["current_loss"] = loss_val
                    self._state["current_grad_norm"] = grad_norm_val
                    self._state["current_lr"] = scheduler.get_last_lr()[0]
                    self._state["current_epoch"] = epoch + 1  # 1-indexed
                    self._state["current_step"] = batch_idx
                    self._state["total_epochs"] = epochs
                    self._state["steps_per_epoch"] = steps_per_epoch
                    self._state["progress"] = global_step / max(total_steps, 1)

                    if loss_val < best_loss:
                        best_loss = loss_val
                        no_improve_epochs = 0
                        # Save interim best checkpoint if needed
                    
                    global_step += 1

                    # Broadcast progress
                    self._broadcast({
                        "type": "training_progress",
                        "run_id": run_id,
                        "epoch": epoch + 1,
                        "total_epochs": epochs,
                        "step": batch_idx,
                        "steps_per_epoch": steps_per_epoch,
                        "global_step": global_step,
                        "loss": round(loss_val, 6),
                        "grad_norm": round(grad_norm_val, 6),
                        "learning_rate": self._state["current_lr"],
                        "progress": min(self._state["progress"], 1.0),
                        "status": "training"
                    })

                    # Record metrics
                    if global_step % 5 == 0:
                        self._record_metrics(run_id, epoch, batch_idx, global_step,
                                             loss_val, grad_norm_val, self._state["current_lr"],
                                             act_mean, act_std)

                    # Step mode: pause after each batch
                    if self._step_mode:
                        self._pause_event.clear()
                        self._state["status"] = "paused"
                        self._broadcast({
                            "type": "training_step_paused",
                            "run_id": run_id,
                            "epoch": epoch,
                            "step": batch_idx,
                            "global_step": global_step,
                            "loss": round(loss_val, 6),
                            "grad_norm": round(grad_norm_val, 6),
                            "learning_rate": self._state["current_lr"],
                            "act_stats": self._act_stats
                        })

                    if max_steps > 0 and global_step >= max_steps:
                        break
                
                # ── End of Epoch Validation ─────────────────────
                if val_loader:
                    model.eval()
                    val_loss = 0
                    val_token_count = 0
                    with torch.no_grad():
                        for vbatch in val_loader:
                            v_input_ids = vbatch["input_ids"].to(device)
                            v_attention_mask = vbatch["attention_mask"].to(device)
                            v_labels = vbatch["labels"].to(device)
                            # Count non-masked tokens for accurate PPL
                            valid_tokens = (v_labels != -100).sum().item()
                            with torch.amp.autocast(device_type="cuda" if "cuda" in device else "cpu", enabled=use_amp):
                                v_outputs = model(input_ids=v_input_ids, attention_mask=v_attention_mask, labels=v_labels)
                                val_loss += v_outputs.loss.item() * valid_tokens
                            val_token_count += valid_tokens

                    avg_val_loss = val_loss / max(val_token_count, 1)
                    val_ppl = math.exp(avg_val_loss) if avg_val_loss < 100 else float('inf')
                    self._broadcast({
                        "type": "training_progress",
                        "run_id": run_id,
                        "status": "validating",
                        "epoch": epoch,
                        "val_loss": round(avg_val_loss, 4),
                        "val_ppl": round(val_ppl, 2),
                        "train_loss": round(loss_val, 4),
                    })

                    # Best loss tracking + early stopping
                    if avg_val_loss < best_loss:
                        best_loss = avg_val_loss
                        no_improve_epochs = 0
                    else:
                        no_improve_epochs += 1

                    model.train()

                    if no_improve_epochs >= patience:
                        self._broadcast({"type": "log", "message": f"Early stopping at epoch {epoch+1}"})
                        break
                else:
                    # If no val set, use the last training loss for progress tracking
                    if loss_val < best_loss:
                        best_loss = loss_val

            # End of training (normal completion or abort-and-save)
            from core.paths import get_data_path
            save_dir = os.path.join(get_data_path("models"), "trained", f"run_{run_id}")
            os.makedirs(save_dir, exist_ok=True)
            model.save_pretrained(save_dir)
            tokenizer.save_pretrained(save_dir)

            final_status = "aborted_saved" if break_save else "completed"
            self._state["status"] = final_status
            self._state["active"] = False
            self._update_run_db(run_id, status=final_status, best_loss=best_loss, checkpoint_dir=save_dir)
            self._broadcast({
                "type": "training_complete",
                "run_id": run_id,
                "best_loss": round(best_loss, 6),
                "total_time": 0,
                "aborted_saved": break_save,
                "checkpoint_dir": save_dir
            })

        except Exception as e:
            self._state["status"] = "failed"
            self._state["active"] = False
            self._update_run_db(run_id, status="failed", error_message=str(e))
            self._broadcast({
                "type": "training_error",
                "run_id": run_id,
                "error": str(e)
            })

    def _register_activation_hooks(self, model):
        """Register forward hooks on linear layers to capture activation stats."""
        self._hooks = []
        self._act_buffer = []

        def make_hook(name):
            def hook(module, input, output):
                if isinstance(output, torch.Tensor):
                    self._act_buffer.append({
                        "name": name,
                        "mean": output.detach().float().mean().item(),
                        "std": output.detach().float().std().item()
                    })
            return hook

        for name, module in model.named_modules():
            if isinstance(module, torch.nn.Linear):
                hook = module.register_forward_hook(make_hook(name))
                self._hooks.append(hook)

    def _update_run_db(self, run_id, **fields):
        """Update training_runs row from the training thread."""
        try:
            import sqlite3
            from core.paths import get_data_path
            db_path = get_data_path("chat_history.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            sets = ", ".join(f"{k}=?" for k in fields)
            vals = list(fields.values())
            vals.append(run_id)
            cursor.execute(f"UPDATE training_runs SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _record_metrics(self, run_id, epoch, step, global_step,
                        loss, grad_norm, lr, act_mean=0, act_std=0):
        """Write a training_metrics row to DB (called from training thread)."""
        try:
            import sqlite3
            from core.paths import get_data_path
            db_path = get_data_path("chat_history.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO training_metrics (run_id, epoch, step, global_step, loss, grad_norm, learning_rate, act_mean, act_std) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, epoch, step, global_step, loss, grad_norm, lr, act_mean, act_std)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


# Singleton
_training_engine = None


def get_training_engine() -> TrainingEngine:
    global _training_engine
    if _training_engine is None:
        _training_engine = TrainingEngine()
    return _training_engine
