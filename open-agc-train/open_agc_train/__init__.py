"""
open-agc-train — Model training, fine-tuning, evaluation, and benchmark plugin.

Usage:
    from open_agc_train import init_plugin
    plugin = init_plugin(db_path="path/to/training.db",
                         broadcast_fn=my_broadcast_function)
    app.include_router(plugin.router, prefix="/api/training")
"""
import os
import sys

# Re-export public API
from .engine import TrainingEngine, get_training_engine, _training_available
from .eval import compute_ppl, compute_generation_metrics
from .codegen import generate_model_code, generate_hf_config, generate_tokenizer_config
from .architectures import get_builder, BUILDERS

_plugin_instance = None


def init_plugin(db_path: str = "", broadcast_fn=None, data_dir: str = "",
                server_config: dict = None):
    """Initialize the training plugin.

    Args:
        db_path: Path to training.db (default: <data_dir>/training.db)
        broadcast_fn: WebSocket broadcast function for progress updates
        data_dir: Data directory for models, datasets, benchmarks
        server_config: Server config dict (for proxy, API keys etc.)

    Returns:
        Plugin instance with .router (FastAPI APIRouter), .engine, .state
    """
    global _plugin_instance

    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    else:
        data_dir = os.getcwd()

    if not db_path:
        db_path = os.path.join(data_dir, "training.db")

    from .db import init_db
    init_db(db_path)

    from .routes import create_router
    engine = get_training_engine(data_dir=data_dir, db_path=db_path)
    engine.set_broadcast_fn(broadcast_fn)

    router = create_router(db_path, engine, broadcast_fn, server_config or {})

    _plugin_instance = {
        "router": router,
        "engine": engine,
        "state": {"db_path": db_path, "data_dir": data_dir, "active": True},
    }
    return _plugin_instance


def get_plugin():
    """Return the active plugin instance or None."""
    return _plugin_instance
