"""DB 挂钩/投影测试：落钉、锁定、权重不回刷、结转链、冻结、成员变动、投影。"""

import pytest

from app.db import connect
from app.modules.debt import (
    pin_generation, week_ledger, members_debt_view,
    WeekSettledError, BadDaysError,
)


def entries(c, week_id):
    return [dict(r) for r in c.execute(
        "SELECT * FROM debt_entries WHERE week_id=? ORDER BY member_id", (week_id,))]


def assignment_stats(c, week_id):
    stats = {}
    for r in c.execute(
            "SELECT member_id, COUNT(*) n, SUM(task_weight) load "
            "FROM assignments WHERE week_id=? GROUP BY member_id", (week_id,)):
        stats[r["member_id"]] = (r["n"], r["load"])
    return stats


def test_generate_pins_ledger():
    c = connect()
    res = pin_generation(c, 1)
    c.commit()
    assert res["count"] == 21 and res["frozen"] == 0
    # 回包即台账：debt_before 全 0，债后零和，load 10/9/9
    assert [(d["member_id"], d["slots"], d["load"]) for d in res["debts"]] == \
           [(1, 7, 10), (2, 7, 9), (3, 7, 9)]
    assert sum(d["debt_after"] for d in res["debts"]) == pytest.approx(0, abs=1e-9)

    rows = entries(c, 1)
    assert len(rows) == 3
    assert rows[0]["debt_before"] == 0
    assert rows[0]["debt_after"] == pytest.approx(-2 / 3)

    # assignments 带权重快照：扫地 3 格位=2，其余=1
    weights = {(r["task_id"], r["task_weight"]) for r in c.execute(
        "SELECT DISTINCT task_id, task_weight FROM assignments WHERE week_id=1")}
    assert weights == {(1, 1), (2, 1), (3, 2)}
    week = c.execute("SELECT status,frozen FROM weeks WHERE id=1").fetchone()
    assert week["status"] == "ready" and week["frozen"] == 0
    c.close()


def test_pin_lock_blocks_regenerate():
    c = connect()
    pin_generation(c, 1); c.commit()
    with pytest.raises(WeekSettledError):
        pin_generation(c, 1)
    # 台账行数与格位数不变
    assert len(entries(c, 1)) == 3
    assert c.execute("SELECT COUNT(*) n FROM assignments WHERE week_id=1").fetchone()["n"] == 21
    c.close()


def test_weight_change_does_not_rebrush_old_week():
    c = connect()
    pin_generation(c, 1); c.commit()
    before = entries(c, 1)
    snap = {r["task_weight"] for r in c.execute(
        "SELECT task_weight FROM assignments WHERE week_id=1 AND task_id=3").fetchall()}
    assert snap == {2}

    # 事后把扫地改成 99：旧周钉账与格位快照都不得回刷
    c.execute("UPDATE tasks SET weight=99 WHERE id=3"); c.commit()
    after = entries(c, 1)
    assert [(r["slots"], r["load"], r["avg_load"], r["debt_before"], r["debt_after"])
            for r in after] == \
           [(r["slots"], r["load"], r["avg_load"], r["debt_before"], r["debt_after"])
            for r in before]
    assert {r["task_weight"] for r in c.execute(
        "SELECT task_weight FROM assignments WHERE week_id=1 AND task_id=3")} == {2}
    led = week_ledger(c, 1)
    assert led["pinned"] is True
    assert [(r["slots"], r["load"]) for r in led["rows"]] == \
           [(7, 10), (7, 9), (7, 9)]
    c.close()


def test_three_way_pins_agree_across_weeks():
    """看板占格增量、成员页债表债后、周回看债后 = 回包债后优先口径。"""
    c = connect()
    w1 = pin_generation(c, 1); c.commit()
    w1_after = {d["member_id"]: d["debt_after"] for d in w1["debts"]}

    c.execute("INSERT INTO weeks(label,status) VALUES ('第13周','draft')")
    w2 = pin_generation(c, 2); c.commit()

    # 1) 落库占格 == 回包 slots（同一次带债演算，不允许无债落库）
    db_slots = sorted((r["day"], r["task_id"], r["member_id"], r["task_weight"])
                      for r in c.execute(
                          "SELECT day,task_id,member_id,task_weight "
                          "FROM assignments WHERE week_id=2"))
    assert db_slots == sorted((s["day"], s["task_id"], s["member_id"], s["task_weight"])
                              for s in w2["slots"])

    # 2) 看板占格按债后优先增多：欠债成员 2 由首周 load 9 增到 10
    stats = assignment_stats(c, 2)
    assert stats[2] == (7, 10) and stats[1] == (7, 9)

    # 3) 台账统计 == 占格统计；debt_before 承接首周 after
    rows = {r["member_id"]: r for r in entries(c, 2)}
    for mid, (n, load) in stats.items():
        assert rows[mid]["slots"] == n and rows[mid]["load"] == load
        assert rows[mid]["debt_before"] == pytest.approx(w1_after[mid])
    # 成员 2 受偿后翻正为结余
    assert rows[2]["debt_after"] == pytest.approx(-1 / 3)

    # 4) 周回看投影读到真实债后（不是债前的镜像）
    led = {r["member_id"]: r for r in week_ledger(c, 2)["rows"]}
    assert led[2]["debt_before"] == pytest.approx(1 / 3)
    assert led[2]["debt_after"] == pytest.approx(-1 / 3)

    # 5) 成员页逐周台账与当前余额同源同值
    view = members_debt_view(c)
    e2 = {e["member_id"]: e for e in view["entries"] if e["week_id"] == 2}
    assert e2[2]["debt_after"] == pytest.approx(rows[2]["debt_after"])
    cur = {m["id"]: m["current_balance"] for m in view["members"]}
    for mid in (1, 2, 3):
        assert cur[mid] == pytest.approx(rows[mid]["debt_after"])
    c.close()


def test_carryover_chain_through_frozen_week():
    c = connect()
    w1 = pin_generation(c, 1); c.commit()
    w1_after = {d["member_id"]: d["debt_after"] for d in w1["debts"]}

    # 第 2 周只留 1 个活跃成员 → 冻结：余额原样结转
    c.execute("UPDATE members SET active=0 WHERE id IN (2,3)")
    c.execute("INSERT INTO weeks(label,status) VALUES ('第13周','draft')")
    frozen = pin_generation(c, 2); c.commit()
    assert frozen["frozen"] == 1
    fr = {d["member_id"]: d for d in frozen["debts"]}
    assert fr[1]["debt_before"] == pytest.approx(w1_after[1])
    assert fr[1]["debt_after"] == pytest.approx(w1_after[1])
    # 冻结周仍照常生成占格（唯一活跃成员全包），只是不结算
    assert assignment_stats(c, 2) == {1: (21, 28)}

    # 第 3 周全员回归：debt_before 必须越过冻结周，等于第 1 周 after
    c.execute("UPDATE members SET active=1 WHERE id IN (2,3)")
    c.execute("INSERT INTO weeks(label,status) VALUES ('第14周','draft')")
    w3 = pin_generation(c, 3); c.commit()
    w3_before = {d["member_id"]: d["debt_before"] for d in w3["debts"]}
    assert w3_before == pytest.approx(w1_after)
    c.close()


def test_freeze_zero_members():
    c = connect()
    c.execute("UPDATE members SET active=0 WHERE active=1")
    res = pin_generation(c, 1); c.commit()
    assert res["frozen"] == 1 and res["count"] == 0
    assert res["slots"] == [] and res["debts"] == []
    week = c.execute("SELECT status,frozen FROM weeks WHERE id=1").fetchone()
    assert week["status"] == "ready" and week["frozen"] == 1
    led = week_ledger(c, 1)
    assert led["pinned"] is False and led["frozen"] == 1
    assert c.execute("SELECT COUNT(*) n FROM assignments WHERE week_id=1").fetchone()["n"] == 0
    c.close()


def test_returning_member_keeps_balance_new_member_starts_zero():
    c = connect()
    pin_generation(c, 1); c.commit()
    after1 = entries(c, 1)[1]["debt_after"]  # 小雨（id=2）

    # 小雨缺席第 2 周
    c.execute("UPDATE members SET active=0 WHERE id=2")
    c.execute("INSERT INTO weeks(label,status) VALUES ('第13周','draft')")
    pin_generation(c, 2); c.commit()

    # 第 3 周回归 + 新增成员：回归者延续旧余额，新人从 0 起
    c.execute("UPDATE members SET active=1 WHERE id=2")
    c.execute("INSERT INTO members(name,active,data_quality) VALUES ('新人',1,'clean')")
    c.execute("INSERT INTO weeks(label,status) VALUES ('第14周','draft')")
    res = pin_generation(c, 3); c.commit()
    by = {d["member_id"]: d for d in res["debts"]}
    assert by[2]["debt_before"] == pytest.approx(after1)
    new_id = c.execute("SELECT id FROM members WHERE name='新人'").fetchone()["id"]
    assert by[new_id]["debt_before"] == 0
    c.close()


def test_projections_legacy_and_members_view():
    c = connect()
    # 旧版周：直接插 assignments（无 task_weight、无台账）
    c.execute("INSERT INTO assignments(week_id,day,task_id,member_id) VALUES (0,1,1,1)")
    c.commit()
    led = week_ledger(c, 0)
    assert led["pinned"] is False and led["rows"] == []

    pin_generation(c, 1); c.commit()
    view = members_debt_view(c)
    ids = {m["id"]: m for m in view["members"]}
    # 停用/脏成员同样出现，无历史则余额 0
    assert 4 in ids and ids[4]["current_balance"] == 0
    assert ids[4]["last_week_id"] is None
    # 有台账成员的当前余额 = 其最后一周钉账 debt_after
    e1 = {e["member_id"]: e for e in view["entries"] if e["week_id"] == 1}
    for mid in (1, 2, 3):
        assert ids[mid]["current_balance"] == pytest.approx(e1[mid]["debt_after"])
    assert ids[1]["last_week_id"] == 1
    c.close()


def test_bad_days():
    c = connect()
    with pytest.raises(BadDaysError):
        pin_generation(c, 1, days=0)
    with pytest.raises(BadDaysError):
        pin_generation(c, 1, days=32)
    # 拒绝时不得落钉
    assert entries(c, 1) == []
    assert c.execute("SELECT COUNT(*) n FROM assignments WHERE week_id=1").fetchone()["n"] == 0
    c.close()
