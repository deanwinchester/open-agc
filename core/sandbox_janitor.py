# -*- coding: utf-8 -*-
"""沙箱 Janitor（沙箱治理二期）：tmp/ TTL 定时清理 + 磁盘硬水位强制清空。

- 后台线程（api/server.py 启动钩子拉起，周期可配置，默认 1 小时）扫描
  <sandbox>/tmp/ 顶层条目：mtime 超过 tmp_ttl_days（默认 7 天）的删除。
  mtime 取条目自身（lstat 不跟随链接；目录即其自身 st_mtime，不递归聚合
  ——递归太贵）。
- 磁盘水位（评审 I2 统一统计引擎）：每轮先查总大小聚合——复用一期条目
  统计缓存求和（api/routes/routes_sandbox._aggregate_total_size，未就绪
  目录排入一期有界线程池暖缓存），不再独立全树遍历。无缓存本轮跳过；
  部分条目未就绪时聚合为已就绪下界（partial），硬水位按低估值判定只会
  延迟不误触。超过 hard_gb（默认 50）时无视 TTL 清空 tmp/（保留目录本身
  与保留标记条目）。soft_gb（默认 20）仅供 entries API 的水位告警展示，
  不触发清理。
- 每次清理动作写清单到 data/sandbox_janitor.log（JSONL/UTF-8，超 5MB
  轮转截断保留尾部）：删除、跳过（pinned/链接无法按链接删除）、失败都
  记录；未到期条目不记（不是「跳过」决策，纯噪声）；tmp 不存在时按要求
  静默跳过（也不记）。size 取自一期统计缓存，partial 统计带 size_partial。
- 删除安全判据与一期 clean_tmp 完全一致（api/routes/routes_sandbox.py）：
  tmp 本身为链接（junction/symlink，realpath 比较）整轮拒绝；条目为链接
  只删链接本身（symlink 用 os.remove、junction/reparse 点用 os.rmdir），
  绝不跟随进入目标递归删除。
- 保留标记（pins）持久化在 data/sandbox_pins.json：仅 tmp/ 顶层条目可
  pin（其他分区本就不会被自动清理）；每轮清理前自动清死钉（条目已不
  存在的 pin 移除）。

配置（config.json 的 sandbox_janitor 节，全部有缺省，缺节/坏值也能跑）：
  {"sandbox_janitor": {"enabled": true, "tmp_ttl_days": 7,
                       "interval_hours": 1, "soft_gb": 20, "hard_gb": 50}}
"""
import json
import os
import shutil
import threading
import time as _time
from datetime import datetime

from api.config import load_config
from core.paths import get_data_path

_GB = 1024 ** 3

_DEFAULTS = {
    "enabled": True,
    "tmp_ttl_days": 7,
    "interval_hours": 1.0,
    "soft_gb": 20,
    "hard_gb": 50,
}

_LOG_TAIL_BYTES = 512 * 1024   # janitor_log 端点只读清单文件尾部
_LOG_MAX_BYTES = 5 * 1024 * 1024   # 清单日志轮转阈值（超 5MB 截断保留尾部）
_LOG_KEEP_BYTES = 1 * 1024 * 1024  # 轮转后保留的尾部大小


def load_janitor_config() -> dict:
    """读 config.json 的 sandbox_janitor 节合并缺省值；坏值逐项回落缺省。
    tmp_ttl_days 负值钳制为 0（评审 Minor 5：负 TTL 会让所有条目「已过期」）。"""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    sec = cfg.get("sandbox_janitor")
    if not isinstance(sec, dict):
        sec = {}
    out = dict(_DEFAULTS)
    for key in ("tmp_ttl_days", "interval_hours", "soft_gb", "hard_gb"):
        try:
            if key in sec:
                out[key] = float(sec[key])
        except (TypeError, ValueError):
            pass
    out["tmp_ttl_days"] = max(out["tmp_ttl_days"], 0.0)
    out["enabled"] = bool(sec.get("enabled", out["enabled"]))
    return out


# ── 保留标记（pins）：data/sandbox_pins.json ──

_pins_lock = threading.Lock()


def _pins_path() -> str:
    return get_data_path("sandbox_pins.json")


def _read_pins_locked() -> set:
    """读 pins 文件为名称集合；文件缺失/损坏返回空集。兼容裸 list 与 {"pinned": []}。"""
    try:
        with open(_pins_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    if isinstance(data, dict):
        data = data.get("pinned", [])
    if not isinstance(data, list):
        return set()
    return {str(n) for n in data}


def _write_pins_locked(pins: set):
    path = _pins_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pinned": sorted(pins)}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_pins() -> set:
    """当前保留的 tmp 顶层条目名称集合。"""
    with _pins_lock:
        return _read_pins_locked()


def set_pinned(name: str, pinned: bool) -> bool:
    """设置/清除某 tmp 顶层条目的保留标记；返回设置后的状态。"""
    with _pins_lock:
        pins = _read_pins_locked()
        if pinned:
            pins.add(name)
        else:
            pins.discard(name)
        _write_pins_locked(pins)
    return pinned


def prune_dead_pins(tmp_dir: str) -> set:
    """清死钉：条目已不存在的 pin 移除并持久化；返回存活 pins。"""
    with _pins_lock:
        pins = _read_pins_locked()
        alive = {n for n in pins if os.path.lexists(os.path.join(tmp_dir, n))}
        if alive != pins:
            try:
                _write_pins_locked(alive)
            except OSError:
                pass
        return alive


# ── 清理清单：data/sandbox_janitor.log（JSONL/UTF-8，尾部追加）──

_log_lock = threading.Lock()


def _log_path() -> str:
    return get_data_path("sandbox_janitor.log")


def make_record(entry, entry_type, size, reason, result, error=None,
                size_partial=False) -> dict:
    """清单记录：{时间, 条目名, 类型, size(缓存有值才非空), 原因, 结果[, error]}。
    reason: ttl / hard_watermark / manual；result: deleted / failed /
    skipped_pinned / skipped_link。size 来自一期 partial 统计时带
    size_partial=True（评审 Minor 1：部分值不当精确值展示）。"""
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry": entry,
        "type": entry_type,
        "size": size,
        "reason": reason,
        "result": result,
    }
    if size_partial:
        rec["size_partial"] = True
    if error:
        rec["error"] = str(error)
    return rec


def append_manifest(records) -> None:
    """把本轮清理记录追加到清单日志；空列表不写文件。写入失败不阻断清理。
    文件超 _LOG_MAX_BYTES（5MB）时先轮转：截断仅保留尾部 _LOG_KEEP_BYTES
    （对齐行边界，评审 Minor 4）。"""
    if not records:
        return
    with _log_lock:
        try:
            path = _log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isfile(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
                with open(path, "rb") as f:
                    f.seek(-_LOG_KEEP_BYTES, os.SEEK_END)
                    tail = f.read()
                nl = tail.find(b"\n")
                if nl != -1:
                    tail = tail[nl + 1:]  # 对齐到行边界，避免半行 JSON
                with open(path, "wb") as f:
                    f.write(tail)
            with open(path, "a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[SandboxJanitor] 清单写入失败: {e}")


def read_manifest_tail(limit: int = 50) -> list:
    """读清单尾部 limit 条（新的在前）；文件不存在返回空列表。"""
    path = _log_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    records = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records[-limit:][::-1]


# ── 与 routes_sandbox 的协作（延迟导入防循环引用）──

def _aggregate_total_size(root: str, schedule: bool = False):
    """全沙箱总大小聚合（评审 I2 统一统计引擎）：转发 routes_sandbox 同名函数，
    复用一期条目统计缓存求和——本模块不再有独立全树遍历（旧 _total_worker
    会跟随 junction 越出沙箱高估水位，评审 I1，且与条目统计重复 I/O）。"""
    try:
        from api.routes import routes_sandbox as _rs
        return _rs._aggregate_total_size(root, schedule=schedule)
    except Exception:
        return None


def _peek_entry_stats(rel_path: str):
    """从一期条目统计缓存取统计项（仅查缓存不触发遍历）；取不到返回 None。"""
    try:
        from api.routes import routes_sandbox as _rs
        return _rs._peek_cached_stats(rel_path)
    except Exception:
        return None


def _invalidate_sandbox_stats():
    """清理删除后让一期条目统计失效（水位总量由同一份缓存聚合，随之失效）。"""
    try:
        from api.routes import routes_sandbox as _rs
        _rs._invalidate_stats()
    except Exception:
        pass


# ── 清理主逻辑 ──

def run_janitor_once(root: str, cfg: dict = None, now: float = None) -> dict:
    """执行一轮 tmp/ 清理（测试直调入口，不需要真等 interval）。

    流程：tmp 链接判据（同一期 clean_tmp）→ 清死钉 → 查总大小聚合（一期
    条目统计缓存求和；一个都没就绪本轮跳过，未就绪目录顺带排入线程池暖
    缓存）→ 超硬水位则无视 TTL 全清，否则按 TTL 判定 → 逐条目删除/跳过
    并写清单。返回本轮摘要 dict。"""
    cfg = cfg or load_janitor_config()
    now = _time.time() if now is None else now
    root = os.path.abspath(root)
    tmp_dir = os.path.join(root, "tmp")
    summary = {"round": "ok", "mode": "ttl", "deleted": 0, "failed": 0,
               "skipped_pinned": 0, "skipped_links": 0}
    # tmp 不存在：按要求静默跳过（不记清单）
    if not os.path.lexists(tmp_dir) or not os.path.isdir(tmp_dir):
        summary["round"] = "skipped_tmp_missing"
        return summary
    # tmp 本身若是链接（junction/symlink，islink 对 junction 返回 False，必须
    # realpath 比较），整轮拒绝——同一期 clean_tmp 判据（C1）。
    root_real = os.path.normcase(os.path.realpath(root))
    tmp_real = os.path.normcase(os.path.realpath(tmp_dir))
    if tmp_real != os.path.normcase(os.path.join(root_real, "tmp")):
        print(f"[SandboxJanitor] tmp 目录是链接，本轮拒绝清理: {tmp_dir}")
        summary["round"] = "skipped_tmp_link"
        return summary
    pins = prune_dead_pins(tmp_dir)
    # 磁盘硬水位（评审 I2）：总大小由一期条目统计聚合；无就绪缓存本轮跳过
    # （schedule=True 顺带把未就绪目录排入一期线程池，为下一轮暖缓存）。
    # 聚合为部分值（partial）时是已就绪下界——按低估值判硬水位只会延迟不误触。
    total = _aggregate_total_size(root, schedule=True)
    if total is None:
        summary["round"] = "skipped_no_total"
        return summary
    hard_bytes = cfg["hard_gb"] * _GB
    if hard_bytes > 0 and total["size"] >= hard_bytes:
        summary["mode"] = "hard_watermark"
        print(f"[SandboxJanitor] 总大小 {total['size']} 超硬阈值 "
              f"{int(hard_bytes)}，无视 TTL 清空 tmp/")
    hard_mode = summary["mode"] == "hard_watermark"
    ttl_seconds = cfg["tmp_ttl_days"] * 86400
    records = []
    try:
        names = sorted(os.listdir(tmp_dir))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(tmp_dir, name)
        # 条目级链接判据（同一期）：realpath 不等于期望落点即链接
        entry_real = os.path.normcase(os.path.realpath(p))
        is_link = entry_real != os.path.normcase(os.path.join(tmp_real, name))
        if is_link:
            entry_type = "link"
        elif os.path.isdir(p) and not os.path.islink(p):
            entry_type = "dir"
        else:
            entry_type = "file"
        if not hard_mode:
            # TTL 判定：条目自身 mtime（lstat 不跟随链接；目录取其自身
            # st_mtime，不递归聚合——递归太贵）
            try:
                mtime = os.lstat(p).st_mtime
            except OSError:
                continue
            if now - mtime <= ttl_seconds:
                continue  # 未到期：不算「跳过」，不记清单
        cached = _peek_entry_stats(f"tmp/{name}")
        size = cached.get("size") if cached else None
        size_partial = bool(cached and cached.get("partial"))
        if name in pins:
            summary["skipped_pinned"] += 1
            records.append(make_record(name, entry_type, size,
                                       summary["mode"], "skipped_pinned",
                                       size_partial=size_partial))
            continue
        # 删除（一期判据：链接只删链接本身不跟随；目录 rmtree；其余 remove）
        try:
            if is_link:
                if os.path.islink(p):
                    os.remove(p)
                else:
                    os.rmdir(p)  # junction/reparse 点
            elif entry_type == "dir":
                shutil.rmtree(p)
            else:
                os.remove(p)
            summary["deleted"] += 1
            records.append(make_record(name, entry_type, size,
                                       summary["mode"], "deleted",
                                       size_partial=size_partial))
        except OSError as e:
            if is_link:
                summary["skipped_links"] += 1
                records.append(make_record(name, entry_type, size,
                                           summary["mode"], "skipped_link", str(e),
                                           size_partial=size_partial))
            else:
                summary["failed"] += 1
                records.append(make_record(name, entry_type, size,
                                           summary["mode"], "failed", str(e),
                                           size_partial=size_partial))
    append_manifest(records)
    if summary["deleted"]:
        _invalidate_sandbox_stats()
    return summary


# ── 后台线程（api/server.py 启动钩子拉起）──

_start_lock = threading.Lock()
_started = False


def _janitor_loop():
    print("[SandboxJanitor] Started")
    while True:
        interval_s = 3600.0
        try:
            cfg = load_janitor_config()
            interval_s = max(cfg["interval_hours"], 0.01) * 3600
            if cfg["enabled"]:
                # 延迟导入防循环引用（routes_sandbox 导入本模块）
                from api.routes.routes_sandbox import _sandbox_root
                summary = run_janitor_once(_sandbox_root(), cfg)
                if summary["deleted"] or summary["failed"]:
                    print(f"[SandboxJanitor] 本轮清理: {summary}")
        except Exception as e:
            print(f"[SandboxJanitor] Error: {e}")
        _time.sleep(interval_s)


def start_sandbox_janitor():
    """启动 janitor 后台线程（幂等；daemon）。enabled=false 时线程空转不清理。"""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_janitor_loop, daemon=True).start()
