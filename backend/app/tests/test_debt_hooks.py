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


def test_generate_pins_ledger():
    c = connect()
    res = pin_generation(c, 1)
    c.commit()
    assert res["count"] == 21
    assert res["frozen"] == 0

    rows = entries(c, 1)
    assert [r["member_id"] for r in rows] == [1, 2, 3]
    assert [r["slots"] for r in rows] == [7, 7, 7]
    assert [(r["load"], r["debt_before"]) for r in rows] == [(10, 0.0), (9, 0.0), (9, 0.0)]
    assert rows[0]["debt_after"] == pytest.approx(-2 / 3)
    assert rows[1]["debt_after"] == rows[2]["debt_after"] == pytest.approx(1 / 3)

    # assignments 带权重快照：扫地 3 格位=2，其余=1
    weights = [(r["task_id"], r["task_weight"]) for r in c.execute(
        "SELECT DISTINCT task_id, task_weight FROM assignments WHERE week_id=1 ORDER BY task_id")]
    assert weights == [(1, 1), (2, 1), (3, 2)]
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
    snap = c.execute(
        "SELECT task_weight FROM assignments WHERE week_id=1 AND task_id=3").fetchall()
    assert {r["task_weight"] for r in snap} == {2}

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
    assert [(r["load"], r["debt_after"]) for r in led["rows"]] == \
           [(10, before[0]["debt_after"]), (9, before[1]["debt_after"]),
            (9, before[2]["debt_after"])]
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
    frows = entries(c, 2)
    assert [r["member_id"] for r in frows] == [1]
    assert frows[0]["debt_after"] == frows[0]["debt_before"] == w1_after[1]
    # 冻结周同样锁定，不可重新生成
    with pytest.raises(WeekSettledError):
        pin_generation(c, 2)

    # 第 3 周全员回归：debt_before 必须越过冻结周，等于第 1 周 after
    c.execute("UPDATE members SET active=1 WHERE id IN (2,3)")
    c.execute("INSERT INTO weeks(label,status) VALUES ('第14周','draft')")
    w3 = pin_generation(c, 3); c.commit()
    w3_before = {d["member_id"]: d["debt_before"] for d in w3["debts"]}
    assert w3_before == w1_after
    c.close()


def test_freeze_zero_members():
    c = connect()
    c.execute("UPDATE members SET active=0 WHERE active=1")
    res = pin_generation(c, 1); c.commit()
    assert res["count"] == 0
    assert res["frozen"] == 1
    assert res["debts"] == []
    week = c.execute("SELECT status,frozen FROM weeks WHERE id=1").fetchone()
    assert week["status"] == "ready" and week["frozen"] == 1
    led = week_ledger(c, 1)
    assert led["pinned"] is False and led["frozen"] == 1
    c.close()


def test_frozen_zero_member_week_is_locked_and_not_rebuilt():
    # 全员 skip：无台账行也必须按 weeks.status 锁定，重复生成不得重铺占格
    c = connect()
    c.execute("UPDATE members SET active=0 WHERE active=1")
    pin_generation(c, 1); c.commit()
    with pytest.raises(WeekSettledError):
        pin_generation(c, 1)
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
    assert by[2]["debt_before"] == after1
    new_id = c.execute("SELECT id FROM members WHERE name='新人'").fetchone()["id"]
    assert by[new_id]["debt_before"] == 0.0
    c.close()


def test_response_board_and_review_share_one_pin():
    # 本 bug 核心回归：生成回包 / 看板落库占格 / 周回看 / 成员页 四路同钉
    c = connect()
    w1 = pin_generation(c, 1); c.commit()
    c.execute("INSERT INTO weeks(label,status) VALUES ('第13周','draft')")
    w2 = pin_generation(c, 2); c.commit()

    # 回包占格 == 实际落库 assignments（含成员与权重快照）
    db_slots = [(r["day"], r["task_id"], r["member_id"], r["task_weight"]) for r in c.execute(
        "SELECT day,task_id,member_id,task_weight FROM assignments "
        "WHERE week_id=2 ORDER BY day,task_id")]
    resp_slots = [(s["day"], s["task_id"], s["member_id"], s["task_weight"]) for s in w2["slots"]]
    assert db_slots == resp_slots

    # 回包债行 == 周回看钉账（含真实 debt_after，不是 debt_before 的镜像）
    led = week_ledger(c, 2)
    assert led["pinned"] is True
    assert [(r["member_id"], r["slots"], r["load"], r["avg_load"],
             r["debt_before"], r["debt_after"]) for r in led["rows"]] == \
           [(d["member_id"], d["slots"], d["load"], d["avg_load"],
             d["debt_before"], d["debt_after"]) for d in w2["debts"]]
    assert any(abs(r["debt_after"] - r["debt_before"]) > 1e-9 for r in led["rows"])

    # 成员页逐周台账与当前余额同源同值
    view = members_debt_view(c)
    w2_entries = {(e["member_id"]): e for e in view["entries"] if e["week_id"] == 2}
    for r in led["rows"]:
        assert w2_entries[r["member_id"]]["debt_after"] == r["debt_after"]
    cur = {m["id"]: m["current_balance"] for m in view["members"]}
    for r in led["rows"]:
        assert cur[r["member_id"]] == r["debt_after"]
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
    assert 4 in ids
    assert ids[4]["current_balance"] == 0.0 and ids[4]["last_week_id"] is None
    # 在岗成员当前余额 == 第 1 周钉账 debt_after 真值
    w1 = {e["member_id"]: e["debt_after"] for e in view["entries"] if e["week_id"] == 1}
    for mid in (1, 2, 3):
        assert ids[mid]["current_balance"] == w1[mid]
    assert w1[1] < 0 and w1[2] > 0 and w1[3] > 0
    c.close()


def test_bad_days():
    c = connect()
    with pytest.raises(BadDaysError):
        pin_generation(c, 1, days=0)
    with pytest.raises(BadDaysError):
        pin_generation(c, 1, days=32)
    # 拒绝时不得落钉
    assert c.execute("SELECT COUNT(*) n FROM debt_entries WHERE week_id=1").fetchone()["n"] == 0
    c.close()
