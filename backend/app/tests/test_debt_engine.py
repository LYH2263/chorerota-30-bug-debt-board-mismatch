"""纯债引擎测试：记债、优先偿还、多做冲抵、冻结、确定性、空输入。"""

import pytest

from app.modules.debt.engine import assign_with_debt, settle

TASKS = [(1, 1), (2, 1), (3, 2)]  # 洗碗 / 倒垃圾 / 扫地
MEMBERS = [1, 2, 3]
GRID = [(d, t) for d in range(7) for (t, _) in TASKS]


def loads_of(slots, members=MEMBERS):
    load = {m: 0 for m in members}
    count = {m: 0 for m in members}
    for s in slots:
        load[s["member_id"]] += s["task_weight"]
        count[s["member_id"]] += 1
    return load, count


def test_balanced_weighted_load_and_grid_order():
    # seed 场景：3 人 × (1,1,2) × 7 天 = 总负荷 28，贪心派位逼近均分
    slots = assign_with_debt(MEMBERS, TASKS, days=7, debt_before={})
    # 返回保持网格序：day 升序、同 day 内 task_id 升序
    assert [(s["day"], s["task_id"]) for s in slots] == GRID
    load, count = loads_of(slots)
    assert count == {1: 7, 2: 7, 3: 7}
    assert load == {1: 10, 2: 9, 3: 9}  # LPT：最重的扫地多落一格给 id=1
    assert max(load.values()) - min(load.values()) <= 2  # 极差不超过单格最大权重
    # 每个格位都带权重快照
    assert all(s["task_weight"] == dict(TASKS)[s["task_id"]] for s in slots)


def test_settle_records_signed_debt():
    slots = assign_with_debt(MEMBERS, TASKS, days=7, debt_before={})
    rows = settle(MEMBERS, slots, {})
    by = {r["member_id"]: r for r in rows}
    assert by[1]["load"] == 10 and by[2]["load"] == 9 and by[3]["load"] == 9
    assert by[1]["avg_load"] == pytest.approx(28 / 3)
    assert by[1]["debt_before"] == 0 and by[2]["debt_before"] == 0
    # 多做 = 负（结余），少做 = 正（欠债）
    assert by[1]["debt_after"] == pytest.approx(-2 / 3)
    assert by[2]["debt_after"] == pytest.approx(1 / 3)
    assert by[3]["debt_after"] == pytest.approx(1 / 3)
    # 零和：每周债后余额之和为 0
    assert sum(r["debt_after"] for r in rows) == pytest.approx(0, abs=1e-9)
    assert all(r["frozen"] == 0 for r in rows)


def test_priority_repayment():
    # 第 1 周落钉后，欠债 +1/3 的成员下周优先拿重格位还债
    first = settle(MEMBERS, assign_with_debt(MEMBERS, TASKS, 7, {}), {})
    before = {r["member_id"]: r["debt_after"] for r in first}
    second = assign_with_debt(MEMBERS, TASKS, days=7, debt_before=before)
    load, _ = loads_of(second)
    # 欠债者 2 本周多扛重格位，负荷达到 10；债权人 1 降到 9
    assert load[2] == 10
    assert load[1] == 9
    rows = settle(MEMBERS, second, before)
    # 优先受偿的欠债者本周翻过均值，由欠 +1/3 转为结余 -1/3
    by = {r["member_id"]: r for r in rows}
    assert by[2]["debt_before"] == pytest.approx(1 / 3)
    assert by[2]["debt_after"] == pytest.approx(-1 / 3)
    assert sum(r["debt_after"] for r in rows) == pytest.approx(0, abs=1e-9)


def test_credit_holder_gets_fewer_heavy_slots():
    # 单方预付很多（debt_before 负得多）→ 下周明显少扛
    slots = assign_with_debt([1, 2], [(10, 1), (20, 3)], days=4,
                             debt_before={1: -6.0, 2: 6.0})
    load, _ = loads_of(slots, [1, 2])
    assert load == {1: 2, 2: 14}  # 4 个重格（3）+1 个轻格全给欠债者 2


def test_freeze_passthrough():
    slots = assign_with_debt([1], TASKS, days=7)
    rows = settle([1], slots, {1: 2.5}, frozen=True)
    assert len(rows) == 1
    r = rows[0]
    assert r["slots"] == 21 and r["load"] == 28 and r["avg_load"] == 28
    assert r["debt_before"] == 2.5 and r["debt_after"] == 2.5  # 余额原样结转
    assert r["frozen"] == 1


def test_deterministic_tie_break():
    a = assign_with_debt(MEMBERS, TASKS, 7, {})
    b = assign_with_debt(MEMBERS, TASKS, 7, {})
    assert [(s["day"], s["task_id"], s["member_id"]) for s in a] == \
           [(s["day"], s["task_id"], s["member_id"]) for s in b]
    # 完全同态时低 id 先得格
    slots = assign_with_debt([7, 8], [(1, 1)], days=2, debt_before={})
    assert [s["member_id"] for s in slots] == [7, 8]


def test_empty_inputs():
    assert assign_with_debt([], TASKS, days=7) == []
    assert assign_with_debt(MEMBERS, [], days=7) == []
    assert settle([], [], {}) == []
