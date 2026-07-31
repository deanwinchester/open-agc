# -*- coding: utf-8 -*-
"""交付物登记制：目录 ↔ 任务的多对多登记（deliverables / task_deliverables 两表）。

背景：交付物归属过去靠 outputs/task_<id>/ 目录名隐含（一对一）；同一逻辑任务
跑多次会复用首次任务的目录，归属实际是多对多——删后面的任务目录不跟随，
删首次任务把后续产出连坐。登记制把归属显式化：

- deliverables：一行一个交付物目录（dir_path 为沙箱相对 POSIX 路径，如
  "outputs/唐嫣照片"，UNIQUE；name 语义名；created_by_task 只首次写入）；
- task_deliverables：(task_id, deliverable_id) 关联，UNIQUE 幂等。

写入点：任务完成钩子（api/task_core.handle_task_completion →
register_task_deliverables）与服务启动回填（backfill_deliverables，幂等）。
目录被删除/不存在时不删登记（保留历史），查询方按 os.path.isdir 标 missing。

本模块不 import api.routes.routes_sandbox（反向依赖会成环）——realpath 判据
（is_under_root / resolve_files_dir）实现于此，routes_sandbox 由这里导入复用，
保证删除/列举/登记全链路同一口径。表结构建表在 api/db.py init_db。
"""
import json
import os
import re

from api.db import db_connect
from api.config import load_config

_TASK_DIR_RE = re.compile(r"task_(\d+)")
_CKPT_FILE_RE = re.compile(r"task_(\d+)\.json")


def canon_dir_path(p) -> str:
    """登记表 dir_path 的统一口径：POSIX 分隔符 + 平台大小写折叠。

    不能只用 os.path.normcase——Windows 上它除了小写化还会把 / 翻成 \\，
    存进表就成了反斜杠形态，与查询/文件系统口径全错开（评审修复轮实测）。
    先归一分隔符、normcase 折叠大小写（POSIX 上为恒等，大小写敏感语义不变）、
    再翻回 /；登记/查询/删除三处同口径。"""
    rel = str(p or "").strip().replace("\\", "/").strip("/")
    return os.path.normcase(rel).replace("\\", "/")


_PROTECTED_REL = {"", ".", "outputs", "projects", "tmp", "downloads", ".checkpoints"}
# 分区目录本身不是交付物；但其子目录（含 outputs/<名>）是
_PARTITION_ROOTS = {"projects", "tmp", "downloads", ".checkpoints"}


def is_valid_deliverable_rel(rel: str) -> bool:
    """交付物目录的合法形态：`outputs/<名称>[/...]`（新约定）或沙箱根下
    单层/多层真实目录（如 `cbsign_all`、`exports`——检查点 files_dir 的
    历史形态）；各段非空非点，分区目录本身（含其作为首段）不算。

    根目录/分区目录本身永远不是交付物——生产实证：agent 把检查点
    files_dir 写成沙箱根（D:\\...\\workspace），回填登记出 dir_path='.'
    的脏行；此类行流入删除流程有 rmtree 整个 workspace 的风险（执行层
    forbidden_reals 是最后防线，登记/查询层必须在源头挡住）。"""
    rel = canon_dir_path(rel)
    if not rel or rel in _PROTECTED_REL:
        return False
    parts = rel.split("/")
    if any(x in ("", ".", "..") for x in parts):
        return False
    return parts[0] not in _PARTITION_ROOTS


def sandbox_root() -> str:
    """沙箱根目录（解析口径与 routes_sandbox._sandbox_root 一致）。"""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return os.path.abspath(cfg.get("sandbox_dir") or os.path.join(os.getcwd(), "workspace"))


def is_under_root(root: str, path: str) -> bool:
    """realpath 口径判断 path 是否严格位于 sandbox 根内（含根本身）。"""
    try:
        root_real = os.path.normcase(os.path.realpath(root))
        real = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath([root_real, real]) == root_real
    except (ValueError, OSError):
        return False


def resolve_files_dir(root: str, files_dir: str):
    """解析检查点 files_dir 为沙箱内真实目录；越出沙箱或不存在时返回 None。

    兼容两种口径：沙箱内相对路径（outputs/task_1、mongo_export）与仓库根
    相对路径（workspace/mongo_export，检查点历史写法）。"""
    p = files_dir.strip()
    candidates = [p] if os.path.isabs(p) else [
        os.path.join(root, p),
        os.path.join(os.getcwd(), p),
    ]
    for c in candidates:
        if is_under_root(root, c) and os.path.isdir(c):
            return os.path.realpath(c)
    return None


def _rel_of(abs_path: str, root: str) -> str:
    """沙箱内绝对路径 → 登记表用的相对 POSIX 路径。"""
    return os.path.relpath(abs_path, root).replace("\\", "/")


def outputs_dir_of(path, root: str):
    """把 generated_files 条目路径规范为 'outputs/<名>'；不在 outputs 下或含
    .. 逃逸段时返回 None。

    兼容绝对路径（须在沙箱内）、沙箱相对（outputs/x/...）与仓库根相对
    （workspace/outputs/x/...）三种写法。"""
    p = str(path or "").strip()
    if not p:
        return None
    if os.path.isabs(p):
        try:
            rel = os.path.relpath(p, root)
        except (ValueError, OSError):
            return None
        rel = rel.replace("\\", "/")
        if rel.startswith(".."):
            return None
    else:
        rel = p.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if rel.startswith("workspace/"):  # 仓库根相对历史写法
            rel = rel[len("workspace/"):]
    parts = [x for x in rel.split("/") if x and x != "."]
    if any(x == ".." for x in parts):
        return None  # 显式拒绝逃逸段（旧 lstrip("./") 会把 ../outputs/x 误吞成沙箱内）
    if len(parts) >= 2 and parts[0] == "outputs":
        return f"outputs/{parts[1]}"
    return None


def _read_checkpoint(task_id: int, root: str):
    """读取 <root>/.checkpoints/task_<id>.json；缺失/损坏返回 None（不依赖
    api.task_core，避免 task_core → registry → task_core 循环 import）。"""
    try:
        path = os.path.join(root, ".checkpoints", f"task_{task_id}.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── 登记写入 ──

def register_deliverable(dir_path: str, task_id=None, name: str = None,
                         touch: bool = True):
    """登记目录并关联任务（幂等）。dir_path 为沙箱相对 POSIX 路径（统一
    normcase 存储——登记/查询/删除三处同口径，Windows 大小写不撞重复行）。

    - 原子 upsert（I4）：INSERT OR IGNORE 不抛 UNIQUE 冲突，随后回读 id——
      并发首登同一目录时输家也能拿到 id 续走关联，不再整体返回 None 丢关联；
    - 已存在：touch=True 时刷新 updated_at；created_by_task 用守卫式 UPDATE
      仅在为空时补写（首次写入者保留，并发后到者不覆盖）；
    - task_id 给定时关联 (task_id, deliverable_id)，UNIQUE 去重幂等。
    返回 deliverable_id；失败返回 None（登记故障不阻断主流程）。
    """
    dir_path = canon_dir_path(dir_path)
    if not dir_path or not is_valid_deliverable_rel(dir_path):
        return None  # 非法形态（'.'/根/分区目录本身）从源头拒登记
    try:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO deliverables (dir_path, name, created_by_task) "
                "VALUES (?,?,?)",
                (dir_path, name or dir_path.rsplit("/", 1)[-1], task_id))
            row = conn.execute(
                "SELECT id, created_by_task FROM deliverables WHERE dir_path=?",
                (dir_path,)).fetchone()
            did = row["id"]
            if touch:
                conn.execute(
                    "UPDATE deliverables SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (did,))
            if task_id is not None and row["created_by_task"] is None:
                conn.execute(
                    "UPDATE deliverables SET created_by_task=? "
                    "WHERE id=? AND created_by_task IS NULL",
                    (task_id, did))
            if task_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO task_deliverables (task_id, deliverable_id) "
                    "VALUES (?,?)", (task_id, did))
            conn.commit()
            return did
        finally:
            conn.close()
    except Exception as e:
        print(f"[Deliverables] register error: {e}")
        return None


def register_task_deliverables(task_id: int, root: str = None):
    """任务完成钩子的登记入口：收集本任务的交付物目录并登记/关联。

    来源（与 artifacts 端点口径一致）：检查点 files_dir（沙箱内真实目录）+
    task_steps.generated_files 里 outputs/ 下的目录。每次完成都刷新
    updated_at，关联幂等。返回登记的目录列表。"""
    root = root or sandbox_root()
    dirs = []
    ckpt = _read_checkpoint(task_id, root)
    if ckpt:
        files_dir = ckpt.get("files_dir")
        if isinstance(files_dir, str) and files_dir.strip():
            resolved = resolve_files_dir(root, files_dir)
            if resolved:
                dirs.append(_rel_of(resolved, root))
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT generated_files FROM task_steps WHERE task_id=? "
            "AND generated_files IS NOT NULL AND generated_files != ''",
            (task_id,)).fetchall()
        conn.close()
        for r in rows:
            try:
                parsed = json.loads(r[0])
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for f in parsed:
                path = f.get("path", "") if isinstance(f, dict) else f
                od = outputs_dir_of(path, root)
                if od:
                    dirs.append(od)
    except Exception as e:
        print(f"[Deliverables] collect generated_files error: {e}")
    registered = []
    for rel in dict.fromkeys(dirs):  # 去重保序
        if register_deliverable(rel, task_id=task_id, touch=True) is not None:
            registered.append(rel)
    return registered


def backfill_deliverables(root: str = None):
    """启动回填（幂等）：存量交付物目录登记进表。只补缺失行/关联，不刷新
    updated_at（避免每次启动都覆盖真实更新时间）。

    - outputs/task_<id>/ → 登记并关联 task <id>；
    - .checkpoints/task_<id>.json 的 files_dir（沙箱内）→ 登记并关联对应任务；
    - task_steps.generated_files 里 outputs/ 下的目录 → 登记并关联所属任务。

    只关联 tasks 表里存活的任务（I3）：已删任务的残留目录/检查点/步骤不再
    登记——否则死关联会把目录误判"共享"，删除时永远跳过、UI 显示"还被任务
    #X（已删）使用"。
    """
    root = root or sandbox_root()
    try:
        conn = db_connect()
        living = {r[0] for r in conn.execute("SELECT id FROM tasks").fetchall()}
        conn.close()
    except Exception as e:
        print(f"[Deliverables] backfill tasks scan error: {e}")
        return
    outputs_dir = os.path.join(root, "outputs")
    try:
        children = sorted(os.listdir(outputs_dir)) if os.path.isdir(outputs_dir) else []
    except OSError:
        children = []
    for child in children:
        abs_child = os.path.join(outputs_dir, child)
        if not os.path.isdir(abs_child) or not is_under_root(root, abs_child):
            continue
        m = _TASK_DIR_RE.fullmatch(child)
        if m and int(m.group(1)) in living:
            register_deliverable(f"outputs/{child}", task_id=int(m.group(1)),
                                 touch=False)
    ckpt_dir = os.path.join(root, ".checkpoints")
    try:
        ckpt_files = sorted(os.listdir(ckpt_dir)) if os.path.isdir(ckpt_dir) else []
    except OSError:
        ckpt_files = []
    for fn in ckpt_files:
        m = _CKPT_FILE_RE.fullmatch(fn)
        if not m:
            continue
        tid = int(m.group(1))
        if tid not in living:
            continue
        data = _read_checkpoint(tid, root)
        files_dir = data.get("files_dir") if data else None
        if isinstance(files_dir, str) and files_dir.strip():
            resolved = resolve_files_dir(root, files_dir)
            if resolved:
                register_deliverable(_rel_of(resolved, root), task_id=tid,
                                     touch=False)
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT task_id, generated_files FROM task_steps "
            "WHERE generated_files IS NOT NULL AND generated_files != ''").fetchall()
        conn.close()
        pairs = set()  # (task_id, outputs_dir) 去重后再登记（同任务多步骤同目录只写一次）
        for r in rows:
            if r[0] not in living:
                continue
            try:
                parsed = json.loads(r[1])
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for f in parsed:
                path = f.get("path", "") if isinstance(f, dict) else f
                od = outputs_dir_of(path, root)
                if od:
                    pairs.add((r[0], od))
        for tid, od in sorted(pairs):
            register_deliverable(od, task_id=tid, touch=False)
    except Exception as e:
        print(f"[Deliverables] backfill generated_files error: {e}")


# ── 查询 ──

def get_task_dirs(task_id: int) -> list:
    """任务关联的交付物目录：[{id, dir_path, name, task_ids}]（task_ids 升序，
    只含 tasks 表存活任务——I3：已删任务的残留关联不构成"共享"；被查询任务
    本身不过滤，删除流程中任务行已删也能读到自己的目录）。查询故障返回空
    列表（调用方走未登记兜底逻辑）。"""
    try:
        conn = db_connect()
        rows = conn.execute(
            "SELECT d.id, d.dir_path, d.name FROM deliverables d "
            "JOIN task_deliverables td ON td.deliverable_id = d.id "
            "WHERE td.task_id=? ORDER BY d.id", (task_id,)).fetchall()
        out = []
        for r in rows:
            if not is_valid_deliverable_rel(r["dir_path"]):
                continue  # 历史脏行（如 dir_path='.'）不展示不流转
            tids = [x[0] for x in conn.execute(
                "SELECT td.task_id FROM task_deliverables td "
                "JOIN tasks t ON t.id = td.task_id "
                "WHERE td.deliverable_id=? ORDER BY td.task_id",
                (r["id"],)).fetchall()]
            out.append({"id": r["id"], "dir_path": r["dir_path"],
                        "name": r["name"], "task_ids": tids})
        conn.close()
        return out
    except Exception as e:
        print(f"[Deliverables] get_task_dirs error: {e}")
        return []


def get_dirs_map(dir_paths: list) -> dict:
    """批量查目录登记：dir_path -> {id, name, task_ids}（沙箱页展开与删除兜底
    共享判定用）。入参统一 normcase（与登记存储同口径）；task_ids 只含存活
    任务（I3）。查询故障返回空字典（条目展示退回目录名判定）。"""
    if not dir_paths:
        return {}
    try:
        norm_paths = sorted({canon_dir_path(p) for p in dir_paths})
        conn = db_connect()
        ph = ",".join("?" for _ in norm_paths)
        rows = conn.execute(
            f"SELECT id, dir_path, name FROM deliverables WHERE dir_path IN ({ph})",
            norm_paths).fetchall()
        rows = [r for r in rows if is_valid_deliverable_rel(r["dir_path"])]
        ids = [r["id"] for r in rows]
        links = {}
        if ids:
            ph2 = ",".join("?" for _ in ids)
            for lr in conn.execute(
                    f"SELECT td.deliverable_id, td.task_id FROM task_deliverables td "
                    f"JOIN tasks t ON t.id = td.task_id "
                    f"WHERE td.deliverable_id IN ({ph2}) ORDER BY td.task_id",
                    ids).fetchall():
                links.setdefault(lr["deliverable_id"], []).append(lr["task_id"])
        conn.close()
        return {r["dir_path"]: {"id": r["id"], "name": r["name"],
                                "task_ids": links.get(r["id"], [])}
                for r in rows}
    except Exception as e:
        print(f"[Deliverables] get_dirs_map error: {e}")
        return {}


def remove_task_links(task_id: int):
    """清掉某任务的全部交付物关联（任务删除时调用）。deliverables 行保留作
    历史——目录是否还在由查询方按文件系统标 missing。"""
    try:
        conn = db_connect()
        conn.execute("DELETE FROM task_deliverables WHERE task_id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Deliverables] remove_task_links error: {e}")
