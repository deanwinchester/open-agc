"""
存量治理脚本：对 data/auto_tools 下所有自动工具执行 prune（可重复运行）。

将"调用次数 < min-calls 且超过 max-age-days 未使用"的工具移入各自目录的
_archive/ 子目录（不硬删，可手动恢复）。加载路径已跳过 _archive。

用法（仓库根目录）：
    python scratch/prune_auto_tools.py                     # 默认 30 天 / 1 次
    python scratch/prune_auto_tools.py --max-age-days 60 --min-calls 2
    python scratch/prune_auto_tools.py --root <其他 auto_tools 根目录>
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.paths import get_data_path
from tools.auto_tool import prune_auto_tools


def iter_target_dirs(root: str):
    """Yield root itself (loose legacy .py files) + each session subdir."""
    yield root
    for d in sorted(os.listdir(root)):
        full = os.path.join(root, d)
        if os.path.isdir(full) and not d.startswith("_") and not d.startswith("."):
            yield full


def main():
    ap = argparse.ArgumentParser(description="Archive stale, never-used auto-tools.")
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument("--min-calls", type=int, default=1)
    ap.add_argument("--root", default=get_data_path("auto_tools"),
                    help="auto_tools root (default: data/auto_tools)")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print(f"Root not found: {args.root}")
        return 1

    total_kept = total_archived = 0
    for target in iter_target_dirs(args.root):
        result = prune_auto_tools(target, max_age_days=args.max_age_days,
                                  min_calls=args.min_calls)
        kept, archived = len(result["kept"]), len(result["archived"])
        if kept or archived:
            rel = os.path.relpath(target, args.root)
            print(f"[{rel}] kept={kept} archived={archived}")
            for name in result["archived"]:
                print(f"    archived: {name}")
        total_kept += kept
        total_archived += archived

    print(f"\nTOTAL: kept={total_kept} archived={total_archived}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
