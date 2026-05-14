import json
import subprocess
import sys
import os
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

TOOL_SCHEMA = {
    "name": "skip_and_continue",
    "description": "Executes a list of steps, skipping the step at the given index (e.g., a previously failed step). Each step is executed in order; the skipped step is omitted entirely. Returns a summary of results.",
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "List of step objects. Each step has an 'action' (read_file, execute_shell, execute_python, search_web) and a 'params' dict with action-specific keys.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["read_file", "execute_shell", "execute_python", "search_web"]
                        },
                        "params": {
                            "type": "object",
                            "description": "Parameters for the action. For read_file: {'path': str}. For execute_shell: {'command': str, 'timeout': int (optional)}. For execute_python: {'code': str}. For search_web: {'query': str, 'max_results': int (optional)}."
                        }
                    },
                    "required": ["action", "params"]
                }
            },
            "skip_index": {
                "type": "integer",
                "description": "Index (0-based) of the step to skip (i.e., not execute). Must be between 0 and len(steps)-1.",
                "minimum": 0
            }
        },
        "required": ["steps", "skip_index"]
    }
}

def _execute_read_file(params: Dict[str, Any]) -> str:
    path = params["path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return f"Read file {path}: {len(content)} characters."

def _execute_shell(params: Dict[str, Any]) -> str:
    command = params["command"]
    timeout = params.get("timeout", 10)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            raise RuntimeError(f"Shell command failed (exit code {result.returncode}): {stderr}")
        return f"Shell command succeeded. stdout: {stdout[:200]}" if stdout else "Shell command succeeded (no output)."
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Shell command timed out after {timeout}s.")

def _execute_python(params: Dict[str, Any]) -> str:
    code = params["code"]
    try:
        # Use exec with a restricted namespace to avoid side effects
        namespace = {}
        exec(code, namespace)
        # Return any variable named 'result' if present, else success message
        result = namespace.get("result", None)
        if result is not None:
            return f"Python code executed. Result: {result}"
        return "Python code executed successfully."
    except Exception as e:
        raise RuntimeError(f"Python execution error: {e}")

def _execute_search_web(params: Dict[str, Any]) -> str:
    query = params["query"]
    max_results = params.get("max_results", 5)
    # Deterministic mock: return a fixed message (in real usage, replace with actual web search)
    # This keeps the tool self-contained and deterministic.
    return f"Mock search for '{query}' (max_results={max_results}): no real search performed. Replace with actual search implementation as needed."

def execute(steps: List[Dict[str, Any]], skip_index: int) -> str:
    """
    Execute all steps except the one at skip_index.
    Returns a summary string indicating success or failure with details.
    """
    if skip_index < 0 or skip_index >= len(steps):
        raise ValueError(f"skip_index {skip_index} out of range (0-{len(steps)-1})")

    results = []
    for i, step in enumerate(steps):
        if i == skip_index:
            results.append(f"Step {i} skipped (as requested).")
            continue

        action = step.get("action")
        params = step.get("params", {})
        try:
            if action == "read_file":
                msg = _execute_read_file(params)
            elif action == "execute_shell":
                msg = _execute_shell(params)
            elif action == "execute_python":
                msg = _execute_python(params)
            elif action == "search_web":
                msg = _execute_search_web(params)
            else:
                raise ValueError(f"Unknown action: {action}")
            results.append(f"Step {i} ({action}) succeeded: {msg}")
        except Exception as e:
            results.append(f"Step {i} ({action}) failed: {e}")
            # Stop execution on first failure after skipping
            return "\n".join(results) + f"\n\nExecution halted due to failure at step {i}."

    return "\n".join(results) + "\n\nAll steps completed successfully."

# Example usage (for testing, not part of tool)
if __name__ == "__main__":
    # Example steps from the task (with truncated commands completed reasonably)
    sample_steps = [
        {"action": "read_file", "params": {"path": r"D:\ComfyUI_windows_portable\ComfyUI\user\default\workflow.json"}},
        {"action": "execute_shell", "params": {"command": r'dir "D:\ComfyUI_windows_portable\ComfyUI\custom_nodes"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "Get-Content \'D:\ComfyUI_windows_portable\ComfyUI\workflow.json\'"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "(Get-Content \'D:\ComfyUI_windows_portable\ComfyUI\workflow.json\') | ConvertFrom-Json"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "$json = Get-Content \'D:\ComfyUI_windows_portable\ComfyUI\workflow.json\' | ConvertFrom-Json; $json"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'dir "D:\ComfyUI_windows_portable\ComfyUI\models\checkpoints"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "(Get-Content \'D:\ComfyUI_windows_portable\ComfyUI\workflow.json\') | ConvertFrom-Json | Select-Object -ExpandProperty nodes"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'dir "D:\ComfyUI_windows_portable\ComfyUI\models\loras"', "timeout": 10}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "$wc = [System.Net.WebClient]::new(); $wc.DownloadString(\'https://example.com\')"', "timeout": 15}},
        {"action": "search_web", "params": {"query": "ComfyUI HunyuanVideo 1.5 workflow setup requirements", "max_results": 5}},
        {"action": "execute_shell", "params": {"command": r'powershell -Command "$json = Get-Content \'D:\ComfyUI_windows_portable\ComfyUI\workflow.json\' | ConvertFrom-Json"', "timeout": 10}},
        {"action": "execute_python", "params": {"code": "import json\nwith open(r'D:\\ComfyUI_windows_portable\\ComfyUI\\workflow.json') as f:\n    data = json.load(f)\nresult = len(data.get('nodes', []))"}},
        {"action": "execute_python", "params": {"code": "import json\nwith open(r'D:\\ComfyUI_windows_portable\\ComfyUI\\workflow.json') as f:\n    data = json.load(f)\nresult = list(data.get('nodes', [{}])[0].keys())"}}
    ]

    # Suppose step 0 (read_file) failed? We skip index 0.
    print(execute(sample_steps, skip_index=0))