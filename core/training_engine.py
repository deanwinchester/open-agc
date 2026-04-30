"""
Training Engine — orchestrates model training with pause/step/resume hooks.
Runs in a background thread and broadcasts progress via WebSocket.
"""
import threading
import time
import sys
import subprocess
import importlib


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
        return dict(self._state)

    def start_training(self, run_id: int, run_record: dict) -> bool:
        """Launch training in background thread."""
        if self._state["active"]:
            return False
        self._state["active"] = True
        self._state["run_id"] = run_id
        self._state["status"] = "running"
        self._state["progress"] = 0.0
        self._abort_flag.clear()
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
        """Abort training after current batch."""
        if not self._state["active"]:
            return False
        self._abort_flag.set()
        self._pause_event.set()
        self._state["status"] = "aborted"
        self._state["active"] = False
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

            base_model = run_record.get("base_model_id", "")
            base_source = run_record.get("base_model_source", "huggingface")

            self._broadcast({"type": "training_progress", "run_id": run_id,
                             "epoch": 0, "step": 0, "global_step": 0,
                             "loss": 0, "grad_norm": 0, "progress": 0,
                             "status": "initializing"})

            # Load tokenizer and model
            from transformers import AutoTokenizer, AutoModelForCausalLM
            tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            self._broadcast({"type": "training_progress", "run_id": run_id,
                             "epoch": 0, "step": 0, "global_step": 0,
                             "loss": 0, "grad_norm": 0, "progress": 0.02,
                             "status": "loading_model"})

            model = AutoModelForCausalLM.from_pretrained(
                base_model,
                torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
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

            # Optimizer and scheduler
            optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
            total_steps = epochs * 100  # placeholder; real value depends on dataset size
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps)

            global_step = 0
            best_loss = float("inf")
            scaler = torch.cuda.amp.GradScaler() if torch.cuda.is_available() else None

            for epoch in range(epochs):
                # Placeholder: iterate over actual dataset batches
                for batch_idx in range(100):  # simulated batches for now
                    self._pause_event.wait()
                    if self._abort_flag.is_set():
                        self._broadcast({"type": "training_complete", "run_id": run_id,
                                         "best_loss": best_loss, "total_time": 0,
                                         "aborted": True})
                        self._record_metrics(run_id, epoch, batch_idx, global_step, best_loss, 0, learning_rate)
                        return

                    # Simulate forward/backward for now
                    loss_val = 2.0 / (global_step + 1) + 0.1
                    grad_norm_val = 1.0 / (global_step + 1) ** 0.5

                    # Simulate activation stats
                    act_mean = 0.02 / (global_step + 1) ** 0.1
                    act_std = 0.15 / (global_step + 1) ** 0.1

                    self._state["current_loss"] = loss_val
                    self._state["current_grad_norm"] = grad_norm_val
                    self._state["current_lr"] = learning_rate
                    self._state["current_epoch"] = epoch
                    self._state["current_step"] = batch_idx
                    self._state["progress"] = global_step / max(total_steps, 1)
                    self._act_stats = {"mean": act_mean, "std": act_std, "per_layer": []}

                    if loss_val < best_loss:
                        best_loss = loss_val

                    global_step += 1

                    # Broadcast progress
                    self._broadcast({
                        "type": "training_progress",
                        "run_id": run_id,
                        "epoch": epoch,
                        "step": batch_idx,
                        "global_step": global_step,
                        "loss": round(loss_val, 6),
                        "grad_norm": round(grad_norm_val, 6),
                        "learning_rate": learning_rate,
                        "progress": min(self._state["progress"], 1.0),
                        "status": "training"
                    })

                    # Record metrics
                    if global_step % 5 == 0:
                        self._record_metrics(run_id, epoch, batch_idx, global_step,
                                             loss_val, grad_norm_val, learning_rate,
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
                            "learning_rate": learning_rate,
                            "act_stats": self._act_stats
                        })

                    if max_steps > 0 and global_step >= max_steps:
                        break

                    time.sleep(0.3)  # simulate compute time

            self._state["status"] = "completed"
            self._state["active"] = False
            self._broadcast({
                "type": "training_complete",
                "run_id": run_id,
                "best_loss": round(best_loss, 6),
                "total_time": 0
            })

        except Exception as e:
            self._state["status"] = "failed"
            self._state["active"] = False
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
