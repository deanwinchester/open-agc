# -*- coding: utf-8 -*-
"""沙箱治理 API：顶层条目浏览、手动归类、手动删除、一键清 tmp、任务交付物。

分区约定（与 agent 系统提示「沙箱分区」段一致）：
- projects/            长期项目，永不自动清理
- outputs/<主题语义名>/ 任务交付物（语义命名，同主题任务复用同一目录；
                       归属走 deliverables/task_deliverables 登记表而非目录名，
                       task_<id>/ 仅兜底；与大任务检查点 files_dir 口径一致）
- tmp/                 一次性脚本/中间产物
- downloads/           安装包等大文件

二期新增（自动清理由 core/sandbox_janitor 承担，本模块提供联动 API）：
- entries 响应带 watermark（磁盘水位：由条目统计缓存聚合，二期 I2 统一
  统计引擎——不另起全树遍历，避免 entries 一次请求两套全量遍历 I/O 翻倍）
- tmp/ 展开一层并带 pinned 保留标记；POST /api/sandbox/pin 设置/取消
- GET /api/sandbox/janitor_log 查看清理清单（手动 clean_tmp 也写清单）
- 归档压缩、存量强制搬迁仍不做。

性能说明：size/file_count 异步计算 + 内存缓存（TTL 10 分钟）——workspace 根下
存在百万级文件的导出目录（如 cbsign_all 约 116 万文件），同步遍历会把事件循环
卡死。entries 先返回缓存或 null（前端显示「统计中…」），后台有界线程池
（_STATS_MAX_WORKERS=4 + 队列去重）遍历写缓存；单目录遍历设 60s 时间预算，
按条目数间隔检查，超时返回 partial=True 的部分统计。遍历对每个子目录做
realpath 判据剪枝——os.walk 会跟随 junction 越出遍历根（评审 I1 实测）。

安全说明：所有写操作与目录展开统一 realpath 口径——tmp/dest 分区/交付物目录
若是符号链接或 junction（os.path.islink 对 junction 返回 False，必须 realpath
比较）一律拒绝或跳过；分区目录（projects/outputs/tmp/downloads）与 .checkpoints
禁止 delete/move。
"""
import os
import re
import shutil
import threading
import time as _time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import load_config
from api.task_core import read_task_checkpoint
from api import deliverables_registry as _dr
from core import sandbox_janitor as _janitor

router = APIRouter()

_INSTALLER_EXTS = {".exe", ".msi", ".7z", ".zip", ".dmg", ".pkg"}
_STATS_CACHE_TTL = 600        # 统计缓存 TTL（秒）
_STATS_TIME_BUDGET = 60.0     # 单目录遍历时间预算（秒）
_STATS_MAX_WORKERS = 4        # 统计线程池并发上限（防首次打开页面爆发数百线程）
_BUDGET_CHECK_INTERVAL = 5000  # 遍历中按条目数间隔检查时间预算（扁平大目录兜底）
# 禁止删除/移动：检查点目录 + 四个分区目录本身（删 outputs 会连锅端全部交付物）
_FORBIDDEN_NAMES = {".checkpoints", "projects", "outputs", "tmp", "downloads"}
_DELIVERABLE_RE = re.compile(r"task_(\d+)")

# rel_path -> {"size": int, "file_count": int, "partial": bool, "ts": float}
# 有界线程池：_stats_queue 待遍历队列，_stats_queued 去重（在队/在跑），
# _stats_generation 失效代际——invalidate 后旧代 worker 写回直接丢弃（I6）。
_stats_lock = threading.Lock()
_stats_cache = {}
_stats_queue = []
_stats_queued = set()
_stats_active = 0
_stats_generation = 0
_stats_root = None  # 缓存归属的沙箱根；root 变更时整体失效（防串根显示旧值）


def _sandbox_root() -> str:
    """沙箱根目录（解析口径与 api/task_core.get_checkpoint_dir 一致）。"""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return os.path.abspath(cfg.get("sandbox_dir") or os.path.join(os.getcwd(), "workspace"))


def _fmt_mtime(ts) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


# realpath 判据（is_under_root）与 files_dir 解析的单份实现沉在
# deliverables_registry——登记表写入侧也要用同一口径，本模块由那里导入复用，
# 避免 registry → routes_sandbox 反向依赖成环。
_is_under_root = _dr.is_under_root


def _resolve_top_level(rel_path: str):
    """把用户提交的相对路径解析为 sandbox 顶层条目的 (绝对路径, 名称)。

    安全硬性要求：拒绝绝对路径与 ../ 逃逸；只允许第一层条目（拒绝嵌套路径）；
    realpath 必须严格落在 sandbox 根内（防符号链接逃逸——链接指向沙箱外或
    更深层级一律拒绝）。非法时抛 HTTPException(400/403)。
    """
    raw = (rel_path or "").strip()
    if not raw or os.path.isabs(raw):
        raise HTTPException(status_code=400, detail="非法路径：仅允许沙箱顶层条目的相对路径")
    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if len(parts) != 1 or parts[0] in (".", ".."):
        raise HTTPException(status_code=400, detail="只允许操作沙箱顶层条目")
    name = parts[0]
    root = _sandbox_root()
    joined = os.path.join(root, name)
    root_real = os.path.normcase(os.path.realpath(root))
    real = os.path.normcase(os.path.realpath(joined))
    if os.path.dirname(real) != root_real:
        raise HTTPException(status_code=403, detail="路径越出沙箱根目录")
    return joined, name


# ── size/file_count 异步统计 + 缓存（有界线程池 + 失效代际）──

def _reset_stats_locked():
    """锁内：清缓存/队列/去重标记并递增失效代际。在途 worker 写回前比对
    代次，过期即丢弃（防 clean_tmp 后 UI 在 TTL 内显示旧值）。"""
    global _stats_generation
    _stats_cache.clear()
    _stats_queue.clear()
    _stats_queued.clear()
    _stats_generation += 1


def _stats_worker(rel_path: str, abs_path: str, generation: int):
    """遍历目录聚合 size/file_count，完成后按代际写回缓存并泵起排队任务。

    时间预算双粒度检查：每换一个目录查一次 + 目录内每 _BUDGET_CHECK_INTERVAL
    个条目查一次（扁平百万级目录不会一次 yield 跑到底才看表）。

    链接剪枝（评审 I1）：os.walk 会跟随 Windows junction 递归到遍历根之外
    （followlinks 判据基于 islink，判不出 junction——评审本机实测）——对每个
    子目录做 realpath 判据，越出遍历根的从 dirnames 剪除（一个 realpath/目录
    的成本），其内容不计入统计；剪除数写入缓存 pruned_links 并记日志。"""
    global _stats_active
    size = 0
    count = 0
    pruned_links = 0
    partial = False
    deadline = _time.time() + _STATS_TIME_BUDGET
    root_real = os.path.normcase(os.path.realpath(abs_path))
    try:
        for dirpath, dirnames, filenames in os.walk(abs_path):
            kept = []
            for d in dirnames:
                try:
                    sub_real = os.path.normcase(
                        os.path.realpath(os.path.join(dirpath, d)))
                    inside = os.path.commonpath([root_real, sub_real]) == root_real
                except (ValueError, OSError):
                    inside = False
                if inside:
                    kept.append(d)
                else:
                    pruned_links += 1
            dirnames[:] = kept
            for fn in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
                count += 1
                if count % _BUDGET_CHECK_INTERVAL == 0 and _time.time() > deadline:
                    partial = True
                    break
            if partial or _time.time() > deadline:
                partial = True
                break
    except Exception:
        partial = True
    if pruned_links:
        print(f"[Sandbox] 统计遍历剪除逃逸链接目录 {pruned_links} 个: {abs_path}")
    with _stats_lock:
        _stats_active -= 1
        _stats_queued.discard(rel_path)
        if generation == _stats_generation:
            _stats_cache[rel_path] = {
                "size": size, "file_count": count, "partial": partial,
                "pruned_links": pruned_links,
                "ts": _time.time(),
            }
        _pump_stats_queue_locked()


def _pump_stats_queue_locked():
    """锁内：在并发上限内把队列中的目录派给新 worker；过期代际的排队项丢弃。"""
    global _stats_active
    while _stats_queue and _stats_active < _STATS_MAX_WORKERS:
        rel_path, abs_path, generation = _stats_queue.pop(0)
        if generation != _stats_generation:
            _stats_queued.discard(rel_path)
            continue
        _stats_active += 1
        threading.Thread(
            target=_stats_worker, args=(rel_path, abs_path, generation),
            daemon=True).start()


def _get_or_schedule_stats(root: str, rel_path: str, abs_path: str) -> Optional[dict]:
    """返回缓存统计（{'size','file_count','partial'}）；无缓存/过期时排入遍历
    队列并返回 None（前端据此显示「统计中…」并轮询）。同一路径去重。"""
    global _stats_root
    now = _time.time()
    with _stats_lock:
        # sandbox_dir 配置变更：旧根缓存整体失效，避免同 key 串根显示旧值
        if _stats_root is not None and os.path.normcase(_stats_root) != os.path.normcase(root):
            _reset_stats_locked()
        _stats_root = root
        entry = _stats_cache.get(rel_path)
        if entry and now - entry["ts"] < _STATS_CACHE_TTL:
            return entry
        if rel_path in _stats_queued:
            return None
        _stats_queued.add(rel_path)
        _stats_queue.append((rel_path, abs_path, _stats_generation))
        _pump_stats_queue_locked()
    return None


def _invalidate_stats():
    """删除/移动/清 tmp 后统计全部失效（缓存条目少，整体重建代价可忽略）。
    水位总量由同一份条目统计缓存聚合（二期 I2），此处失效即水位失效。"""
    with _stats_lock:
        _reset_stats_locked()


def _peek_cached_stats(rel_path: str) -> Optional[dict]:
    """仅查统计缓存（不触发后台遍历）；供 Janitor 清单取 size 用。"""
    with _stats_lock:
        entry = _stats_cache.get(rel_path)
        if entry and _time.time() - entry["ts"] < _STATS_CACHE_TTL:
            return entry
    return None


# ── 条目分类与组装 ──

def _classify(abs_path: str, name: str, is_dir: bool) -> str:
    """顶层条目类型：有 .git→project；tmp→temp；安装包扩展名→installer；
    其他目录→dir；散文件→file。（outputs/task_<id>→deliverable 在展开处单独判定）"""
    if is_dir:
        if os.path.isdir(os.path.join(abs_path, ".git")):
            return "project"
        if name == "tmp":
            return "temp"
        return "dir"
    if os.path.splitext(name)[1].lower() in _INSTALLER_EXTS:
        return "installer"
    return "file"


def _make_entry(root: str, rel_path: str, name: str, abs_path: str, is_dir: bool) -> dict:
    try:
        mtime = os.stat(abs_path).st_mtime
    except OSError:
        mtime = None
    entry = {
        "name": name,
        "path": rel_path,
        "is_dir": is_dir,
        "type": _classify(abs_path, name, is_dir),
        "mtime": _fmt_mtime(mtime) if mtime else "",
        "size": None,
        "file_count": None,
        "partial": False,
    }
    if is_dir:
        stats = _get_or_schedule_stats(root, rel_path, abs_path)
        if stats is not None:
            entry["size"] = stats["size"]
            entry["file_count"] = stats["file_count"]
            entry["partial"] = stats["partial"]
    else:
        # 单文件 stat 是 O(1)，同步返回，不走后台统计
        try:
            entry["size"] = os.path.getsize(abs_path)
            entry["file_count"] = 1
        except OSError:
            pass
    return entry


def _deliverable_entries(root: str, outputs_dir: str) -> list:
    """把 outputs/ 展开一层：交付物子目录作为 deliverable 条目；未登记的
    非 task_<id> 子目录（如手动归类进 outputs 的条目）作为普通 dir 条目展示，
    避免移入后不可见。子目录若是逃逸链接（junction/symlink 指向沙箱外）则
    跳过并记日志。

    登记制：已登记目录从登记表取语义名与任务关联（task_ids 可多个——同一
    主题任务复用同一目录）；task_<id> 命名的目录保留名称解析的 task_id 作
    兼容；登记表没有的老目录维持原判定（task_<id>→deliverable，其余→dir）。"""
    out = []
    try:
        children = sorted(os.listdir(outputs_dir))
    except OSError:
        return out
    reg_map = _dr.get_dirs_map([f"outputs/{c}" for c in children])
    for child in children:
        abs_child = os.path.join(outputs_dir, child)
        if not os.path.isdir(abs_child):
            continue
        if not _is_under_root(root, abs_child):
            print(f"[Sandbox] 跳过逃逸链接交付物目录: {abs_child}")
            continue
        rel = f"outputs/{child}"
        entry = _make_entry(root, rel, child, abs_child, True)
        m = _DELIVERABLE_RE.fullmatch(child)
        if m:
            entry["type"] = "deliverable"
            entry["task_id"] = int(m.group(1))
            entry["task_ids"] = [int(m.group(1))]
        info = reg_map.get(_dr.canon_dir_path(rel))
        if info:
            entry["type"] = "deliverable"
            if info.get("name"):
                entry["name"] = info["name"]  # 语义名以登记表为准
            tids = list(info.get("task_ids") or [])
            if m and int(m.group(1)) not in tids:
                tids.append(int(m.group(1)))
                tids.sort()
            entry["task_ids"] = tids
            if not m and tids:
                entry["task_id"] = tids[0]  # 兼容旧前端单任务字段
        out.append(entry)
    return out


def _tmp_child_entries(root: str, tmp_dir: str, pins: set) -> list:
    """把 tmp/ 展开一层（二期保留标记需要条目级视图）：每个子条目带
    pinned 标志。子条目若是逃逸链接（junction/symlink 指向沙箱外）则跳过
    并记日志——不 stat/不统计，与顶层条目同一判据（clean_tmp/janitor 仍会
    按链接本身删除它）。"""
    out = []
    try:
        children = sorted(os.listdir(tmp_dir))
    except OSError:
        return out
    for child in children:
        abs_child = os.path.join(tmp_dir, child)
        if not _is_under_root(root, abs_child):
            print(f"[Sandbox] 跳过逃逸链接 tmp 子条目: {abs_child}")
            continue
        entry = _make_entry(root, f"tmp/{child}", child, abs_child,
                            os.path.isdir(abs_child))
        entry["pinned"] = child in pins
        out.append(entry)
    return out


@router.get("/api/sandbox/entries")
async def list_sandbox_entries():
    """sandbox 顶层条目列表（outputs/ 展开为 task_<id> 交付物条目，tmp/ 展开
    一层带 pinned 标志）。
    指向沙箱外的链接条目跳过并记日志（不跟随 stat/walk，防外部信息泄露）。"""
    root = _sandbox_root()
    pins = _janitor.load_pins()
    entries = []
    if os.path.isdir(root):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            abs_path = os.path.join(root, name)
            is_dir = os.path.isdir(abs_path)
            if not _is_under_root(root, abs_path):
                print(f"[Sandbox] 跳过逃逸链接顶层条目: {abs_path}")
                continue
            if name == "outputs" and is_dir:
                entries.extend(_deliverable_entries(root, abs_path))
                continue
            entries.append(_make_entry(root, name, name, abs_path, is_dir))
            if name == "tmp" and is_dir:
                entries.extend(_tmp_child_entries(root, abs_path, pins))
    # total_size 口径与一期一致（全树精确汇总）：tmp 子条目不参与求和——
    # tmp 行自身统计已含其全部内容，重复计入会双算。
    known = [e["size"] for e in entries
             if e.get("size") is not None and not e["path"].startswith("tmp/")]
    total_size = sum(known) if known else None
    return {"sandbox": root, "entries": entries, "total_size": total_size,
            "watermark": _watermark_payload(root, entries),
            "janitor": _janitor.load_janitor_config()}


def _aggregate_entries_total(entries: list) -> Optional[dict]:
    """由条目列表聚合全沙箱总大小（二期 I2 统一统计引擎）：与 total_size 同一
    求和集合（顶层条目 + outputs/task_* 子项，tmp 子条目除外——其父行已覆盖），
    复用同一份条目统计，不再另起全树遍历。任一条目未就绪/部分统计 →
    partial=True；一个就绪的都没有（且存在未就绪项）→ None（未就绪）。"""
    total = 0
    partial = False
    any_ready = False
    for e in entries:
        if e["path"].startswith("tmp/"):
            continue
        if e.get("size") is None:
            partial = True
            continue
        any_ready = True
        total += e["size"]
        if e.get("partial"):
            partial = True
    if not any_ready and partial:
        return None
    return {"size": total, "partial": partial}


def _aggregate_total_size(root: str, schedule: bool = False) -> Optional[dict]:
    """全沙箱总大小聚合（二期 I2 统一统计引擎，janitor 用）：与 entries 的
    total_size 同一口径——顶层目录（含 tmp 自身）+ outputs/task_* 子项各自
    查一期统计缓存，顶层散文件同步 getsize；不另起全树遍历。
    schedule=True 时未就绪目录排入一期有界线程池（单飞/代际/队列去重语义不变）。
    返回 {"size","partial"}；一个目录都没就绪（且存在目录）→ None。"""
    total = 0
    partial = False
    any_ready = False

    def _accum(rel_path: str, abs_p: str):
        nonlocal total, partial, any_ready
        stats = _peek_cached_stats(rel_path)
        if stats is None:
            partial = True
            if schedule:
                _get_or_schedule_stats(root, rel_path, abs_p)
            return
        any_ready = True
        total += stats["size"]
        if stats["partial"]:
            partial = True

    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        abs_path = os.path.join(root, name)
        if not _is_under_root(root, abs_path):
            continue  # 逃逸链接：不统计不调度（同一期判据）
        if os.path.isdir(abs_path):
            if name == "outputs":
                # outputs 展开口径与 entries 一致：task_* 子项各自统计
                try:
                    children = sorted(os.listdir(abs_path))
                except OSError:
                    children = []
                for child in children:
                    abs_child = os.path.join(abs_path, child)
                    if not _DELIVERABLE_RE.fullmatch(child):
                        continue
                    if not os.path.isdir(abs_child) or not _is_under_root(root, abs_child):
                        continue
                    _accum(f"outputs/{child}", abs_child)
            else:
                _accum(name, abs_path)
        else:
            try:
                total += os.path.getsize(abs_path)
                any_ready = True
            except OSError:
                partial = True
    if not any_ready and partial:
        return None
    return {"size": total, "partial": partial}


def _watermark_payload(root: str, entries: list) -> dict:
    """磁盘水位（二期）：总量由条目统计聚合（二期 I2 统一统计引擎，无独立
    全树遍历）；未就绪时 total_size=None、level=ok，前端不告警；部分条目
    未统计时 partial=True（总量为已就绪下界）。soft/hard 阈值 <=0 视为关闭该级。"""
    cfg = _janitor.load_janitor_config()
    soft_bytes = max(int(cfg["soft_gb"] * 1024 ** 3), 0)
    hard_bytes = max(int(cfg["hard_gb"] * 1024 ** 3), 0)
    agg = _aggregate_entries_total(entries)
    size = agg["size"] if agg else None
    if size is None:
        level = "ok"
    elif hard_bytes and size >= hard_bytes:
        level = "hard"
    elif soft_bytes and size >= soft_bytes:
        level = "soft"
    else:
        level = "ok"
    return {
        "total_size": size,
        "soft_bytes": soft_bytes,
        "hard_bytes": hard_bytes,
        "level": level,
        "partial": bool(agg and agg["partial"]),
    }


# ── 删除 / 归类 / 清 tmp ──

class SandboxPathRequest(BaseModel):
    path: str


class SandboxMoveRequest(BaseModel):
    path: str
    dest: str


@router.post("/api/sandbox/delete")
async def delete_sandbox_entry(req: SandboxPathRequest):
    """删除指定顶层条目（仅限第一层；.checkpoints 与分区目录禁止删除）。"""
    joined, name = _resolve_top_level(req.path)
    if name in _FORBIDDEN_NAMES:
        raise HTTPException(status_code=403, detail=f"禁止删除保留目录 {name}")
    if not os.path.lexists(joined):
        raise HTTPException(status_code=404, detail="条目不存在")
    try:
        if os.path.isdir(joined) and not os.path.islink(joined):
            shutil.rmtree(joined)
        else:
            os.remove(joined)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    _invalidate_stats()
    return {"status": "success", "deleted": name}


@router.post("/api/sandbox/move")
async def move_sandbox_entry(req: SandboxMoveRequest):
    """归类移动顶层条目到 projects/ tmp/ downloads/ outputs/；目标重名返回 409。"""
    dest = (req.dest or "").strip()
    if dest not in ("projects", "tmp", "downloads", "outputs"):
        raise HTTPException(status_code=400, detail="dest 仅允许 projects/tmp/downloads/outputs")
    joined, name = _resolve_top_level(req.path)
    if name in _FORBIDDEN_NAMES:
        raise HTTPException(status_code=403, detail=f"禁止移动保留目录 {name}")
    if name == dest:
        raise HTTPException(status_code=400, detail="该条目已在目标分区")
    if not os.path.lexists(joined):
        raise HTTPException(status_code=404, detail="条目不存在")
    root = _sandbox_root()
    dest_dir = os.path.join(root, dest)
    # 目标分区若是链接（junction/symlink），makedirs(exist_ok=True) 会静默通过
    # 并把条目移出沙箱——realpath 必须等于期望落点，否则拒绝（I2）。
    root_real = os.path.normcase(os.path.realpath(root))
    expected_dest = os.path.normcase(os.path.join(root_real, dest))
    if os.path.lexists(dest_dir):
        if os.path.normcase(os.path.realpath(dest_dir)) != expected_dest:
            raise HTTPException(status_code=403, detail=f"目标分区 {dest} 是链接，拒绝移动")
    else:
        os.makedirs(dest_dir)
    target = os.path.join(dest_dir, name)
    if os.path.lexists(target):
        raise HTTPException(status_code=409, detail="目标位置已存在同名条目")
    # 目标落点 realpath 同样校验（dest 校验后此处应恒成立，兜底防御）
    if os.path.normcase(os.path.realpath(target)) != os.path.normcase(
            os.path.join(expected_dest, name)):
        raise HTTPException(status_code=403, detail="目标落点越出沙箱，拒绝移动")
    try:
        shutil.move(joined, target)
    except (OSError, shutil.Error) as e:
        raise HTTPException(status_code=500, detail=f"移动失败: {e}")
    _invalidate_stats()
    return {"status": "success", "moved": name, "dest": dest, "path": f"{dest}/{name}"}


@router.post("/api/sandbox/clean_tmp")
async def clean_tmp():
    """清空 tmp/（保留目录本身）；tmp 不存在时返回 0 删除数不报错。

    安全（C1）：tmp 本身若是链接（junction/symlink，islink 对 junction 返回
    False，必须 realpath 比较），realpath 不等于期望落点即 403 拒绝——否则
    一键清空删的是沙箱外目录。条目级同样防护：子项为链接时只删链接本身
    （symlink 用 os.remove、junction/reparse 点用 os.rmdir），绝不跟随进入
    目标递归删除；无法按链接删除的跳过并计入 skipped_links。

    二期：清理动作写 janitor 清单（reason=manual；删除/链接跳过/失败都记），
    并在清理后清掉已不存在条目的死钉。手动清空是显式用户动作，不豁免
    pinned 条目（自动清理由 janitor 负责跳过 pinned）。
    """
    root = _sandbox_root()
    tmp_dir = os.path.join(root, "tmp")
    removed = 0
    skipped_links = 0
    records = []
    if os.path.lexists(tmp_dir):
        root_real = os.path.normcase(os.path.realpath(root))
        tmp_real = os.path.normcase(os.path.realpath(tmp_dir))
        expected = os.path.normcase(os.path.join(root_real, "tmp"))
        if tmp_real != expected:
            raise HTTPException(
                status_code=403, detail="tmp 目录是链接，拒绝清空")
        if os.path.isdir(tmp_dir):
            try:
                names = os.listdir(tmp_dir)
            except OSError:
                names = []
            for name in names:
                p = os.path.join(tmp_dir, name)
                cached = _peek_cached_stats(f"tmp/{name}")
                size = cached.get("size") if cached else None
                size_partial = bool(cached and cached.get("partial"))
                try:
                    entry_real = os.path.normcase(os.path.realpath(p))
                    entry_expected = os.path.normcase(os.path.join(tmp_real, name))
                    if entry_real != entry_expected:
                        # 链接/junction：只删链接本身，不跟随目标
                        try:
                            if os.path.islink(p):
                                os.remove(p)
                            else:
                                os.rmdir(p)
                            removed += 1
                            records.append(_janitor.make_record(
                                name, "link", size, "manual", "deleted",
                                size_partial=size_partial))
                        except OSError as e:
                            skipped_links += 1
                            records.append(_janitor.make_record(
                                name, "link", size, "manual", "skipped_link", str(e),
                                size_partial=size_partial))
                        continue
                    entry_type = "dir" if os.path.isdir(p) and not os.path.islink(p) else "file"
                    if entry_type == "dir":
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                    removed += 1
                    records.append(_janitor.make_record(
                        name, entry_type, size, "manual", "deleted",
                        size_partial=size_partial))
                except OSError as e:
                    entry_type = "dir" if os.path.isdir(p) else "file"
                    records.append(_janitor.make_record(
                        name, entry_type, size, "manual", "failed", str(e),
                        size_partial=size_partial))
    _janitor.append_manifest(records)
    _janitor.prune_dead_pins(tmp_dir)
    _invalidate_stats()
    return {"status": "success", "removed": removed, "skipped_links": skipped_links}


# ── 保留标记（二期）：仅 tmp/ 顶层条目可 pin，janitor 自动清理时跳过 ──

class SandboxPinRequest(BaseModel):
    path: str
    pinned: bool


@router.post("/api/sandbox/pin")
async def pin_sandbox_entry(req: SandboxPinRequest):
    """设置/取消 tmp/ 顶层条目的保留标记（持久化 data/sandbox_pins.json）。
    仅允许 tmp/<名称> 两段路径（其他分区本就不会被自动清理，无需 pin）；
    条目须真实存在于 tmp 下——链接条目（junction/symlink，realpath 判据）
    拒绝（评审 Minor 3：entries 列表本就跳过逃逸链接不显示，口径保持一致）。"""
    raw = (req.path or "").strip()
    if not raw or os.path.isabs(raw):
        raise HTTPException(status_code=400, detail="非法路径：仅允许 tmp/<名称>")
    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if len(parts) != 2 or parts[0] != "tmp" or parts[1] in (".", ".."):
        raise HTTPException(status_code=400, detail="仅允许 pin tmp/ 下的顶层条目（tmp/<名称>）")
    name = parts[1]
    tmp_dir = os.path.join(_sandbox_root(), "tmp")
    joined = os.path.join(tmp_dir, name)
    if not os.path.lexists(joined):
        raise HTTPException(status_code=404, detail="条目不存在")
    # realpath 判据（同一期）：不等于期望落点即链接条目 → 403 拒绝
    tmp_real = os.path.normcase(os.path.realpath(tmp_dir))
    if os.path.normcase(os.path.realpath(joined)) != os.path.normcase(
            os.path.join(tmp_real, name)):
        raise HTTPException(status_code=403, detail="链接条目不支持 pin")
    pinned = _janitor.set_pinned(name, bool(req.pinned))
    return {"status": "success", "path": f"tmp/{name}", "pinned": pinned}


# ── 清理记录（二期）：janitor 与手动 clean_tmp 的清单 ──

@router.get("/api/sandbox/janitor_log")
async def get_janitor_log(limit: int = 50):
    """清理清单尾部 limit 条（新的在前）；清单文件不存在返回空列表。"""
    limit = max(1, min(limit, 500))
    return {"records": _janitor.read_manifest_tail(limit)}


# ── 任务交付物 ──

def _resolve_files_dir(root: str, files_dir: str) -> Optional[str]:
    """解析检查点 files_dir（单份实现在 deliverables_registry，此处保留原名
    以兼容 routes_tasks 等既有调用方）。"""
    return _dr.resolve_files_dir(root, files_dir)


@router.get("/api/tasks/{task_id}/artifacts")
async def get_task_artifacts(task_id: int):
    """任务交付物：登记制读取 + 未登记来源兜底合并。

    - dirs：登记表里本任务关联的目录，逐项带 dir/name/shared_with（其他关联
      任务 id 列表）/missing（目录已不存在）/files（该目录自身文件列表）；
      共享情况供删除确认框逐目录决策（独占默认删、共享默认留）。
    - files：全部来源合并的文件列表（旧契约不变）——登记目录之外，未登记的
      检查点 files_dir 与 outputs/task_<id>/ 仍按旧逻辑并入（去重口径一致）。
    """
    root = _sandbox_root()
    files = []
    seen = set()

    def _scan(dir_abs: str, source: str, bucket: list = None):
        try:
            names = sorted(os.listdir(dir_abs))
        except OSError:
            return
        for fn in names:
            fp = os.path.join(dir_abs, fn)
            if not os.path.isfile(fp):
                continue
            key = os.path.normcase(os.path.realpath(fp))
            try:
                st = os.stat(fp)
            except OSError:
                continue
            item = {
                "name": fn,
                "size": st.st_size,
                "mtime": _fmt_mtime(st.st_mtime),
                "source": source,
            }
            if bucket is not None:
                bucket.append(item)
            if key in seen:
                continue
            seen.add(key)
            files.append(item)

    # ── 登记表来源 ──
    dirs_payload = []
    covered = set()  # 已按登记扫描的目录 realpath（兜底来源去重用）
    for d in _dr.get_task_dirs(task_id):
        rel = d["dir_path"]
        abs_dir = os.path.join(root, rel)
        dir_files = []
        missing = True
        if os.path.isdir(abs_dir):
            # 与旧 outputs 来源同一 realpath 判据：逃逸链接跳过并记日志（I1）
            if _is_under_root(root, abs_dir):
                missing = False
                covered.add(os.path.normcase(os.path.realpath(abs_dir)))
                _scan(abs_dir,
                      "outputs" if rel.split("/")[0] == "outputs" else "checkpoint",
                      dir_files)
            else:
                print(f"[Sandbox] 跳过逃逸链接交付物目录: {abs_dir}")
        dirs_payload.append({
            "dir": rel,
            "name": d["name"] or rel.rsplit("/", 1)[-1],
            "shared_with": [t for t in d["task_ids"] if t != task_id],
            "missing": missing,
            "files": dir_files,
        })

    # ── 兜底：未登记的检查点 files_dir ──
    ckpt = read_task_checkpoint(task_id)
    if ckpt:
        files_dir = ckpt.get("files_dir")
        if isinstance(files_dir, str) and files_dir.strip():
            resolved = _resolve_files_dir(root, files_dir)
            if resolved and os.path.normcase(resolved) not in covered:
                covered.add(os.path.normcase(resolved))
                _scan(resolved, "checkpoint")
    # ── 兜底：未登记的 outputs/task_<id>/（realpath 判据同上，I1）──
    outputs_dir = os.path.join(root, "outputs", f"task_{task_id}")
    if os.path.isdir(outputs_dir):
        if _is_under_root(root, outputs_dir):
            if os.path.normcase(os.path.realpath(outputs_dir)) not in covered:
                _scan(outputs_dir, "outputs")
        else:
            print(f"[Sandbox] 跳过逃逸链接交付物目录: {outputs_dir}")
    return {"task_id": task_id, "files": files, "dirs": dirs_payload}
