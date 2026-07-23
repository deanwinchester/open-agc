"""阶段 7 Task 3（B3）：目标机制可靠性测试。

覆盖：
- update_goals 并发写不丢更新（两线程交替追加/递增）
- _check_goal_completeness 判 NO → 创建补救任务；超限 → stuck + reason
- failed 任务不再算"已完结"（阻止判完成）
- 巡检尊重用户中断（interruption_reason == 'user' 的目标不被复活）
- 巡检接管空 task_ids 目标并回链
- _resumable 查询尊重 resume_count < max_resume_count
- 中文重叠检测（difflib ratio ≥ 0.6）归档重复目标
- _MAX_GOALS 只统计 active（pending/doing/stuck）
- prompt 注入跳过 done 目标
"""
import threading
import types

import pytest


# ── fixtures ──

@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """临时数据目录（goals.json）+ 临时 sqlite DB。"""
    import api.db as db_mod
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


def _save_goals(items):
    from tools.task_plan import save_goals
    assert save_goals({"items": items})


def _load_items():
    from tools.task_plan import load_goals
    return load_goals().get("items", [])


def _goal(gid=1, desc="测试目标", status="doing", task_ids=None, resume_count=0):
    return {
        "id": gid,
        "desc": desc,
        "status": status,
        "updated": "2026-07-23 00:00",
        "task_ids": list(task_ids or []),
        "resume_count": resume_count,
    }


def _insert_task(db_mod, status="running", interruption_reason=None,
                 resume_count=0, max_resume_count=10, session_id=1):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, session_id, "
        "interruption_reason, resume_count, max_resume_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("测试任务", "原始查询", status, session_id,
         interruption_reason, resume_count, max_resume_count))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _task_status(db_mod, tid):
    conn = db_mod.db_connect()
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return row[0] if row else None


def _task_count(db_mod):
    conn = db_mod.db_connect()
    n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    return n


def _fake_llm(answer, counter=None):
    """构造替代 core.llm_client.LLMClient 的假类，chat 返回固定文本。"""
    class _Resp:
        def __init__(self, text):
            self.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=text))]

    class _LLM:
        def __init__(self, *a, **k):
            pass

        def chat(self, msgs, **k):
            if counter is not None:
                counter["n"] += 1
            return _Resp(answer), None

    return _LLM


# ── 1. update_goals 并发安全 ──

class TestUpdateGoalsConcurrency:
    def test_concurrent_appends_not_lost(self, tmp_env):
        from tools.task_plan import update_goals

        def _worker(tag):
            for i in range(25):
                def _mut(data, _i=i, _tag=tag):
                    data["items"].append({
                        "id": 0, "desc": f"{_tag}-{_i}", "status": "pending",
                        "updated": "", "task_ids": [], "resume_count": 0,
                    })
                    return True, None
                update_goals(_mut)

        threads = [threading.Thread(target=_worker, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        items = _load_items()
        assert len(items) == 50
        descs = sorted(i["desc"] for i in items)
        assert len(set(descs)) == 50  # 无覆盖/丢失

    def test_concurrent_increments_not_lost(self, tmp_env):
        from tools.task_plan import update_goals
        _save_goals([_goal(gid=1, resume_count=0)])

        def _worker():
            for _ in range(20):
                def _mut(data):
                    for g in data["items"]:
                        if g["id"] == 1:
                            g["resume_count"] = g.get("resume_count", 0) + 1
                    return True, None
                update_goals(_mut)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        items = _load_items()
        assert items[0]["resume_count"] == 40

    def test_no_save_flag_skips_write(self, tmp_env):
        from tools.task_plan import update_goals
        _save_goals([_goal(gid=1)])

        def _mut(data):
            data["items"][0]["status"] = "done"  # 改了但放弃保存
            return False, None

        update_goals(_mut)
        assert _load_items()[0]["status"] == "doing"


# ── 2. 判 NO 补救 / 超限 stuck ──

class TestGoalRemediation:
    def test_no_judgement_creates_remediation_task(self, tmp_env, monkeypatch):
        import api.task_core as tc
        monkeypatch.setattr("core.llm_client.LLMClient", _fake_llm("NO，只完成了一部分"))
        monkeypatch.setattr(tc, "_spawn_goal_task_run", lambda *a, **k: None)

        tid = _insert_task(tmp_env, status="completed")
        _save_goals([_goal(gid=1, desc="整理电影库", task_ids=[tid], resume_count=0)])

        assert tc._check_goal_completeness(tid) == -1

        items = _load_items()
        assert items[0]["resume_count"] == 1
        # 补救任务已创建并回链
        assert _task_count(tmp_env) == 2
        assert len(items[0]["task_ids"]) == 2
        new_tid = max(items[0]["task_ids"])
        conn = tmp_env.db_connect()
        row = conn.execute(
            "SELECT title, user_query FROM tasks WHERE id=?", (new_tid,)).fetchone()
        conn.close()
        assert "补救" in row["title"]
        assert "整理电影库" in row["user_query"]
        assert "判定未完成" in row["user_query"] or "理由" in row["user_query"]

    def test_limit_exceeded_marks_stuck(self, tmp_env, monkeypatch):
        import api.task_core as tc
        monkeypatch.setattr("core.llm_client.LLMClient", _fake_llm("NO"))
        monkeypatch.setattr(tc, "_spawn_goal_task_run", lambda *a, **k: None)

        tid = _insert_task(tmp_env, status="completed")
        _save_goals([_goal(gid=1, task_ids=[tid], resume_count=3)])

        assert tc._check_goal_completeness(tid) == -1

        items = _load_items()
        assert items[0]["status"] == "stuck"
        assert items[0].get("reason")
        assert _task_count(tmp_env) == 1  # 未创建新任务

    def test_yes_marks_done(self, tmp_env, monkeypatch):
        import api.task_core as tc
        monkeypatch.setattr("core.llm_client.LLMClient", _fake_llm("YES"))
        monkeypatch.setattr(tc, "_spawn_goal_task_run", lambda *a, **k: None)

        tid = _insert_task(tmp_env, status="completed")
        _save_goals([_goal(gid=1, task_ids=[tid])])

        assert tc._check_goal_completeness(tid) == 1
        assert _load_items()[0]["status"] == "done"

    def test_failed_task_blocks_completion(self, tmp_env, monkeypatch):
        """failed 不再算"已完结"：目标含失败任务时不进入 LLM 判定。"""
        import api.task_core as tc
        counter = {"n": 0}
        monkeypatch.setattr("core.llm_client.LLMClient", _fake_llm("YES", counter))
        monkeypatch.setattr(tc, "_spawn_goal_task_run", lambda *a, **k: None)

        t1 = _insert_task(tmp_env, status="completed")
        t2 = _insert_task(tmp_env, status="failed", interruption_reason="error")
        _save_goals([_goal(gid=1, task_ids=[t1, t2])])

        assert tc._check_goal_completeness(t1) == 0
        assert counter["n"] == 0  # LLM 未被调用
        assert _load_items()[0]["status"] == "doing"


# ── 3. 巡检：尊重用户中断 / 空 task_ids 接管 / resume 上限 ──

class TestGoalPatrol:
    def test_user_interrupted_goal_not_revived(self, tmp_env, monkeypatch):
        import api.background as bg
        resumed = []
        monkeypatch.setattr(bg, "_guardian_resume_task", lambda tid: resumed.append(tid))

        tid = _insert_task(tmp_env, status="interrupted", interruption_reason="user")
        _save_goals([_goal(gid=1, task_ids=[tid])])

        actions = bg.goal_patrol_once(spawn=False)

        assert resumed == []  # 未复活
        assert _task_count(tmp_env) == 1  # 未创建续跑任务
        assert any("user" in a for a in actions)
        assert _task_status(tmp_env, tid) == "interrupted"

    def test_non_user_interrupted_goal_resumed(self, tmp_env, monkeypatch):
        import api.background as bg
        resumed = []
        monkeypatch.setattr(bg, "_guardian_resume_task", lambda tid: resumed.append(tid))

        tid = _insert_task(tmp_env, status="interrupted",
                           interruption_reason="max_iterations",
                           resume_count=1, max_resume_count=10)
        _save_goals([_goal(gid=1, task_ids=[tid])])

        bg.goal_patrol_once(spawn=False)
        assert resumed == [tid]

    def test_resume_count_cap_blocks_resume(self, tmp_env, monkeypatch):
        """_resumable 查询尊重 resume_count < max_resume_count；fall-through 到
        remediate 会建补救任务且 goal.resume_count 0→1（评审 Minor #2 钉死语义）。"""
        import api.background as bg
        resumed = []
        monkeypatch.setattr(bg, "_guardian_resume_task", lambda tid: resumed.append(tid))

        tid = _insert_task(tmp_env, status="interrupted",
                           interruption_reason="max_iterations",
                           resume_count=10, max_resume_count=10)
        _save_goals([_goal(gid=1, task_ids=[tid], resume_count=0)])

        bg.goal_patrol_once(spawn=False)
        assert resumed == []
        # fall-through 补救：新建 1 个任务并回链，goal.resume_count 0→1
        assert _task_count(tmp_env) == 2
        items = _load_items()
        assert items[0]["resume_count"] == 1
        assert len(items[0]["task_ids"]) == 2
        assert items[0]["status"] == "doing"  # 未超限，不置 stuck

    def test_empty_task_ids_goal_adopted_and_linked(self, tmp_env):
        import api.background as bg
        _save_goals([_goal(gid=1, desc="监控下载目录", status="doing", task_ids=[])])

        actions = bg.goal_patrol_once(spawn=False)

        assert _task_count(tmp_env) == 1
        items = _load_items()
        assert len(items[0]["task_ids"]) == 1
        new_tid = items[0]["task_ids"][0]
        conn = tmp_env.db_connect()
        row = conn.execute("SELECT title FROM tasks WHERE id=?", (new_tid,)).fetchone()
        conn.close()
        assert row is not None
        assert any("first task" in a for a in actions)

    def test_done_goal_not_touched(self, tmp_env, monkeypatch):
        import api.background as bg
        resumed = []
        monkeypatch.setattr(bg, "_guardian_resume_task", lambda tid: resumed.append(tid))

        tid = _insert_task(tmp_env, status="interrupted", interruption_reason="max_iterations")
        _save_goals([_goal(gid=1, status="done", task_ids=[tid])])

        bg.goal_patrol_once(spawn=False)
        assert resumed == []
        assert _task_count(tmp_env) == 1


# ── 4. 中文重叠检测 ──

class TestOverlapDetection:
    def test_chinese_overlap_archived(self):
        from tools.task_plan import archive_overlapping_goals
        items = [_goal(gid=1, desc="整理电影库并生成清单", status="pending")]
        archived = archive_overlapping_goals("整理电影库生成清单", items)
        assert archived == [1]
        assert items[0]["status"] == "archived"

    def test_dissimilar_not_archived(self):
        from tools.task_plan import archive_overlapping_goals
        items = [_goal(gid=1, desc="整理电影库并生成清单", status="pending")]
        archived = archive_overlapping_goals("配置邮件监听服务", items)
        assert archived == []
        assert items[0]["status"] == "pending"

    def test_done_goal_not_archived(self):
        from tools.task_plan import archive_overlapping_goals
        items = [_goal(gid=1, desc="整理电影库生成清单", status="done")]
        archived = archive_overlapping_goals("整理电影库生成清单", items)
        assert archived == []


# ── 5. active 上限统计 / prompt 注入跳过 done ──

class TestActiveLimitAndPrompt:
    def test_max_goals_counts_only_active(self, tmp_env):
        import secrets
        from tools.task_plan import TaskPlanTool, _MAX_GOALS
        tool = TaskPlanTool()
        # 填满 active 名额（desc 用随机后缀避免互相重叠归档）
        for i in range(_MAX_GOALS):
            r = tool.execute(action="goal_add", desc=f"目标{secrets.token_hex(8)}")
            assert "已添加" in r
        # done / archived 不占名额
        items = _load_items()
        items[0]["status"] = "done"
        items[1]["status"] = "archived"
        from tools.task_plan import save_goals
        save_goals({"items": items})
        r = tool.execute(action="goal_add", desc=f"新目标{secrets.token_hex(8)}")
        assert "已添加" in r
        # 其余 8 个仍 active，再加 1 个到上限
        items = _load_items()
        active = [i for i in items if i.get("status") in ("pending", "doing", "stuck")]
        assert len(active) == _MAX_GOALS - 1
        r = tool.execute(action="goal_add", desc=f"另目标{secrets.token_hex(8)}")
        assert "已添加" in r
        # 现在满了
        r = tool.execute(action="goal_add", desc=f"溢出目标{secrets.token_hex(8)}")
        assert "上限" in r

    def test_stuck_counts_as_active(self, tmp_env):
        from tools.task_plan import TaskPlanTool, _MAX_GOALS, save_goals
        items = [_goal(gid=i + 1, desc=f"目标{i:02d}unique{i}pad{i}", status="stuck")
                 for i in range(_MAX_GOALS)]
        save_goals({"items": items})
        r = TaskPlanTool().execute(action="goal_add", desc="新目标n1n2n3")
        assert "上限" in r

    def test_prompt_skips_done(self, tmp_env):
        from tools.task_plan import format_goal_list_for_prompt
        items = [
            _goal(gid=1, desc="进行中的目标AAA", status="doing"),
            _goal(gid=2, desc="已完成的目标BBB", status="done"),
            _goal(gid=3, desc="归档的目标CCC", status="archived"),
        ]
        text = format_goal_list_for_prompt({"items": items})
        assert "进行中的目标AAA" in text
        assert "已完成的目标BBB" not in text
        assert "归档的目标CCC" not in text


# ── 6. WS 回链（_link_resolved_goal）──

class TestWsGoalLinkBack:
    """ws.py 在 agent 跑完后经 _link_resolved_goal 把 ws_task_id 回链到 _resolved_goal。"""

    def test_calls_link_with_correct_ids(self, monkeypatch):
        import api.ws as ws_mod
        calls = []
        monkeypatch.setattr("api.task_core._link_task_to_goal",
                            lambda gid, tid: calls.append((gid, tid)) or True)
        assert ws_mod._link_resolved_goal(3, 42) is True
        assert calls == [(3, 42)]

    def test_zero_goal_not_linked(self, monkeypatch):
        import api.ws as ws_mod
        calls = []
        monkeypatch.setattr("api.task_core._link_task_to_goal",
                            lambda gid, tid: calls.append((gid, tid)) or True)
        assert ws_mod._link_resolved_goal(0, 42) is False
        assert calls == []

    def test_empty_task_id_not_linked(self, monkeypatch):
        import api.ws as ws_mod
        calls = []
        monkeypatch.setattr("api.task_core._link_task_to_goal",
                            lambda gid, tid: calls.append((gid, tid)) or True)
        assert ws_mod._link_resolved_goal(3, None) is False
        assert ws_mod._link_resolved_goal(3, 0) is False
        assert calls == []

    def test_link_exception_swallowed(self, monkeypatch):
        import api.ws as ws_mod

        def _boom(gid, tid):
            raise RuntimeError("goals.json corrupted")

        monkeypatch.setattr("api.task_core._link_task_to_goal", _boom)
        assert ws_mod._link_resolved_goal(3, 42) is False

    def test_real_link_appends_and_dedupes(self, tmp_env):
        """走真实 _link_task_to_goal：回链追加且重复调用不重复追加。"""
        import api.ws as ws_mod
        _save_goals([_goal(gid=1, task_ids=[5])])

        assert ws_mod._link_resolved_goal(1, 9) is True
        assert _load_items()[0]["task_ids"] == [5, 9]
        assert ws_mod._link_resolved_goal(1, 9) is True  # 幂等
        assert _load_items()[0]["task_ids"] == [5, 9]
        # 目标不存在 → 不回链
        assert ws_mod._link_resolved_goal(99, 10) is False
