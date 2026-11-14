"""图算法：环检测、拓扑层级、影响面直接/间接、迁移清单顺序。"""
from __future__ import annotations

from app import graph, migrate


def _graph_from(analysis):
    table_adj = graph.table_adjacency(analysis["table_edges"])
    column_adj = graph.column_adjacency(analysis["column_edges"])
    levels = graph.topo_levels(table_adj)
    return table_adj, column_adj, levels


def test_table_cycle_detected(analysis):
    table_adj, _, _ = _graph_from(analysis)
    cycles = graph.find_cycles(table_adj, "table")
    assert len(cycles) == 1
    assert cycles[0]["nodes"] == ["v_loop_a", "v_loop_b"]
    assert cycles[0]["path"][0] == cycles[0]["path"][-1]
    assert set(cycles[0]["nodes"]) == {"v_loop_a", "v_loop_b"}


def test_column_cycle_detected(analysis):
    _, column_adj, _ = _graph_from(analysis)
    cycles = graph.find_cycles(column_adj, "column")
    assert cycles, "列级同样应检测到环"
    assert all(c["level"] == "column" for c in cycles)
    assert any("v_loop_a.order_id" in c["nodes"] for c in cycles)


def test_topo_levels(analysis):
    _, _, levels = _graph_from(analysis)
    assert levels["ods_orders"] == 0
    assert levels["dwd_order"] == 1
    assert levels["dws_customer_order"] == 2
    assert levels["ads_customer_rank"] == 3
    # 环内节点层级未知，不能用 0 冒充
    assert levels["v_loop_a"] is None and levels["v_loop_b"] is None


def test_impact_on_column(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"], analysis["column_edges"], levels, "ods_orders", "amount"
    )
    assert impact["found"] is True
    objects = {it["object"]: it for it in impact["downstream"]}
    assert set(objects) == {
        "dwd_order",
        "v_channel_sales",
        "dws_customer_order",
        "dws_order_wide",
        "ads_customer_rank",
    }
    assert objects["dwd_order"]["relation"] == "direct"
    assert objects["v_channel_sales"]["relation"] == "direct"
    assert objects["dws_customer_order"]["relation"] == "indirect"
    assert objects["ads_customer_rank"]["distance"] == 3
    assert objects["ads_customer_rank"]["columns"] == ["gmv"]
    # 按层级升序
    levels_seq = [it["level"] for it in impact["downstream"]]
    assert levels_seq == sorted(levels_seq)
    assert impact["max_level"] == 3


def test_impact_on_table(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(analysis["table_edges"], analysis["column_edges"], levels, "ods_orders")
    assert impact["object_count"] == 5
    assert impact["direct_count"] == 2  # dwd_order、v_channel_sales 与 ods_orders 直接相连
    assert impact["column_count"] >= 5


def test_impact_missing_column(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"], analysis["column_edges"], levels, "ods_orders", "not_a_column"
    )
    assert impact["found"] is False
    assert "没有任何下游边" in impact["message"]


def test_impact_covers_join_only_column(analysis):
    """订单号类列只出现在 JOIN 条件时，下游对象也必须进入影响面。"""
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"],
        analysis["column_edges"],
        levels,
        "dws_customer_order",
        "customer_id",
        usages=analysis["usages"],
    )
    assert impact["found"] is True
    objects = {it["object"]: it for it in impact["downstream"]}
    assert set(objects) == {"ads_customer_rank"}
    hit = objects["ads_customer_rank"]
    assert hit["relation"] == "direct"
    assert hit["usages"] == ["join"]
    assert hit["constraint_refs"][0]["script"] == "10_ads_customer_rank.sql"
    assert impact["constraint_count"] == 1


def test_impact_merges_same_object_by_usage(analysis):
    """同一对象既有输出列、又在约束里引用时，合并为一个对象、多种用途。"""
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"],
        analysis["column_edges"],
        levels,
        "dwd_order",
        "customer_id",
        usages=analysis["usages"],
    )
    objects = {it["object"]: it for it in impact["downstream"]}
    assert objects["dws_order_wide"]["usages"] == ["output", "join"]
    assert objects["dws_customer_order"]["usages"] == ["join"]


def test_impact_filter_via_derived_column(analysis):
    """WHERE 引用派生列时，沿列级血缘传导后并入影响面。"""
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"],
        analysis["column_edges"],
        levels,
        "ods_orders",
        "status",
        usages=analysis["usages"],
    )
    objects = {it["object"]: it for it in impact["downstream"]}
    assert objects["dws_customer_order"]["usages"] == ["output", "filter"]


def test_migration_covers_constraint_only_objects(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"],
        analysis["column_edges"],
        levels,
        "dws_customer_order",
        "customer_id",
        usages=analysis["usages"],
    )
    plan = migrate.plan_migration(impact, "drop", None, analysis["cycles"])
    rewrite_objects = [
        o["object"] for s in plan["steps"] if s["action"] == "rewrite" for o in s["objects"]
    ]
    assert "ads_customer_rank" in rewrite_objects
    assert plan["steps"][-1]["action"] == "ddl"
    assert any("ads_customer_rank" in w for w in plan["warnings"])


def test_migration_steps_for_drop(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"], analysis["column_edges"], levels, "ods_orders", "amount"
    )
    plan = migrate.plan_migration(impact, "drop", None, analysis["cycles"])
    assert plan["step_count"] == 6
    actions = [s["action"] for s in plan["steps"]]
    assert actions == ["freeze", "rewrite", "rewrite", "rewrite", "backfill", "ddl"]
    # 先改下游，最后才动源表
    assert actions.index("backfill") < actions.index("ddl")
    assert plan["ddl_sql"] == "ALTER TABLE ods_orders DROP COLUMN amount;"
    # 回填对象只含物理表
    backfill = next(s for s in plan["steps"] if s["action"] == "backfill")
    assert backfill["objects"] and all(o["kind"] == "table" for o in backfill["objects"])
    # 第 1 层重写只涉及直接依赖
    first_rewrite = next(s for s in plan["steps"] if s["action"] == "rewrite")
    assert [o["object"] for o in first_rewrite["objects"]] == ["dwd_order"]


def test_migration_rename(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"], analysis["column_edges"], levels, "ods_orders", "amount"
    )
    plan = migrate.plan_migration(impact, "rename", "order_amount", analysis["cycles"])
    assert plan["ddl_sql"] == "ALTER TABLE ods_orders RENAME COLUMN amount TO order_amount;"
    assert plan["warnings"], "存在环时应给出迁移风险提示"


def test_migration_without_downstream(analysis):
    _, _, levels = _graph_from(analysis)
    impact = graph.impact(
        analysis["table_edges"], analysis["column_edges"], levels, "ods_orders", "status"
    )
    plan = migrate.plan_migration(impact, "drop", None, [])
    assert plan["step_count"] >= 2
    assert plan["steps"][-1]["action"] == "ddl"
