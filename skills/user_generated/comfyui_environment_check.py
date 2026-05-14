import os
import subprocess
import sys
import platform
from typing import Optional

TOOL_SCHEMA = {
    "name": "comfyui_environment_check",
    "description": "Inspect a ComfyUI installation to gather information about models, custom nodes, GPU, and Python/torch environment. Useful for preparing a video generation workflow setup.",
    "parameters": {
        "type": "object",
        "properties": {
            "comfyui_dir": {
                "type": "string",
                "description": "Path to the ComfyUI root directory (e.g., D:\\ComfyUI_windows_portable)",
                "default": "D:\\ComfyUI_windows_portable"
            }
        },
        "required": []
    }
}

def execute(comfyui_dir: str = "D:\\ComfyUI_windows_portable") -> str:
    """
    Perform a comprehensive check of the ComfyUI environment:
    - List available model directories and files
    - Check custom nodes folder
    - Get GPU information (via nvidia-smi or wmic on Windows)
    - Report Python version and torch availability
    Returns a formatted string with all findings.
    """
    try:
        lines = []
        lines.append(f"ComfyUI environment check for: {comfyui_dir}")
        lines.append("=" * 60)

        # 1. Check directory exists
        if not os.path.isdir(comfyui_dir):
            return f"Error: ComfyUI directory not found: {comfyui_dir}"

        # 2. Explore models directory
        models_dir = os.path.join(comfyui_dir, "ComfyUI", "models")
        if os.path.isdir(models_dir):
            lines.append("\n[MODELS DIRECTORY]")
            for item in os.listdir(models_dir):
                item_path = os.path.join(models_dir, item)
                if os.path.isdir(item_path):
                    # Count files inside (excluding subdirs for brevity)
                    file_count = len([f for f in os.listdir(item_path) if os.path.isfile(os.path.join(item_path, f))])
                    lines.append(f"  {item}/  ({file_count} files)")
                else:
                    lines.append(f"  {item} (file)")
        else:
            lines.append(f"\nModels directory not found: {models_dir}")

        # 3. Custom nodes
        custom_nodes_dir = os.path.join(comfyui_dir, "ComfyUI", "custom_nodes")
        if os.path.isdir(custom_nodes_dir):
            lines.append("\n[CUSTOM NODES]")
            nodes = [d for d in os.listdir(custom_nodes_dir) if os.path.isdir(os.path.join(custom_nodes_dir, d))]
            for node in nodes:
                lines.append(f"  {node}")
            if not nodes:
                lines.append("  (no custom nodes found)")
        else:
            lines.append(f"\nCustom nodes directory not found: {custom_nodes_dir}")

        # 4. GPU information (cross-platform attempt)
        lines.append("\n[GPU INFORMATION]")
        try:
            if platform.system() == "Windows":
                # Try nvidia-smi first, then fallback to wmic
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        lines.append(f"  {line.strip()}")
                else:
                    # Fallback wmic
                    result = subprocess.run(
                        ["wmic", "path", "Win32_VideoController", "get", "name,AdapterRAM"],
                        capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        lines.append(result.stdout.strip())
                    else:
                        lines.append("  No GPU info available via wmic")
            else:
                # Linux / macOS: try nvidia-smi
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        lines.append(f"  {line.strip()}")
                else:
                    lines.append("  No GPU info available (nvidia-smi not found)")
        except subprocess.TimeoutExpired:
            lines.append("  GPU info command timed out")
        except FileNotFoundError:
            lines.append("  nvidia-smi not found, no GPU info collected")

        # 5. Python and torch
        lines.append("\n[PYTHON & TORCH]")
        lines.append(f"  Python version: {sys.version}")
        try:
            import torch
            lines.append(f"  torch version: {torch.__version__}")
            lines.append(f"  CUDA available: {torch.cuda.is_available()}")
            if torch.cuda.is_available():
                lines.append(f"  CUDA device count: {torch.cuda.device_count()}")
                for i in range(torch.cuda.device_count()):
                    lines.append(f"    Device {i}: {torch.cuda.get_device_name(i)}")
        except ImportError:
            lines.append("  torch is not installed")
        except Exception as e:
            lines.append(f"  Error checking torch: {e}")

        return "\n".join(lines)

    except Exception as e:
        return f"Unexpected error during environment check: {str(e)}"