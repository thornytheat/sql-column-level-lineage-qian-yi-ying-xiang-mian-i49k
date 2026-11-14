"""列级血缘：别名传播、子查询、JOIN、UNION、star 展开、函数变换链、未知表诊断。"""
from __future__ import annotations

import pytest

from app import lineage


def _edges(analysis, target_table=None, target_column=None):
    out = analysis["column_edges"]
    if target_table:
        out = [e for e in out if e["target_table"] == target_table]
    if target_column:
        out = [e for e in out if e["target_column"] == target_column]
    return out


def test_sample_counts(analysis):
    assert analysis["parsed_count"] == 12
    assert analysis["failed_count"] == 0
    assert len(analysis["table_edges"]) == 17
    assert len(analysis["column_edges"]) == 53


def test_alias_propagation(analysis):
    # 02_view_customer: v_customer.customer_id <- dwd_customer.customer_id
    edges = _edges(analysis, "v_customer", "customer_id")
    assert len(edges) == 1
    assert (edges[0]["source_table"], edges[0]["source_column"]) == ("dwd_customer", "customer_id")
    assert edges[0]["script"] == "02_view_customer.sql"
    assert edges[0]["line"] == 4


def test_function_transform_chain(analysis):
    city = _edges(analysis, "dwd_customer", "city")
    assert len(city) == 1
    assert city[0]["source_table"] == "ods_customers"
    assert city[0]["transforms"] == ["UPPER", "TRIM"]
    # CAST(o.amount AS DECIMAL(18,2)) 保身份
    amount = _edges(analysis, "dwd_order", "amount")
    assert amount[0]["source_column"] == "amount"
    assert amount[0]["transforms"] == ["CAST"]


def test_subquery_propagation(analysis):
    edges = _edges(analysis, "dwd_order_item", "amount")
    sources = sorted((e["source_table"], e["source_column"], tuple(e["transforms"])) for e in edges)
    assert sources == [
        ("ods_order_items", "qty", ("ROUND",)),
        ("ods_order_items", "unit_price", ("ROUND",)),
    ]
    # 子查询里的列名不丢源表
    assert _edges(analysis, "dwd_order_item", "item_id")[0]["source_table"] == "ods_order_items"


def test_join_sources(analysis):
    edges = _edges(analysis, "dws_customer_order")
    pairs = {(e["source_table"], e["source_column"]) for e in edges}
    assert ("dwd_customer", "customer_name") in pairs
    assert ("dwd_order", "amount") in pairs
    assert ("dwd_order", "status") in pairs


def test_union_all_branches(analysis):
    edges = _edges(analysis, "v_channel_sales", "amount")
    branches = {e["union_branch"]: e for e in edges}
    assert set(branches) == {0, 1}
    assert branches[0]["source_table"] == "ods_orders"
    assert branches[0]["transforms"] == ["CAST"]
    assert branches[1]["source_table"] == "dwd_order"
    assert branches[1]["transforms"] == ["COALESCE"]
    # channel 列两个分支都来自 ods_orders / dwd_order
    channel = _edges(analysis, "v_channel_sales", "channel")
    assert sorted(e["source_table"] for e in channel) == ["dwd_order", "ods_orders"]


def test_star_expansion_marks_edges(analysis):
    edges = _edges(analysis, "v_product_sales")
    assert len(edges) == 4
    assert all(e["star_expanded"] for e in edges)
    assert sorted(e["target_column"] for e in edges) == ["brand", "category", "product_name", "sku"]
    assert {e["source_table"] for e in edges} == {"ods_products"}

    wide = _edges(analysis, "dws_order_wide")
    star_edges = [e for e in wide if e["star_expanded"]]
    assert len(star_edges) == 6  # dwd_order 的 6 列经 o.* 展开
    assert all(e["source_table"] == "dwd_order" for e in star_edges)
    assert not any(e["star_expanded"] for e in wide if e["target_column"] in {"customer_name", "city"})


def test_target_column_list_ordering(analysis):
    script = next(s for s in analysis["scripts"] if s["filename"] == "03_dwd_order.sql")
    assert script["columns"] == ["order_id", "customer_id", "amount", "status", "channel", "created_at"]


def test_unknown_source_table_is_diagnosed(schemas):
    files = [{"filename": "bad.sql", "sql": "CREATE VIEW v_x AS\nSELECT n.id AS id\nFROM not_exist_table n;\n"}]
    result = lineage.build_lineage(files, schemas)
    assert result["parsed_count"] == 0
    assert result["failed_count"] == 1
    diag = result["diagnostics"][0]
    assert "未知来源表 not_exist_table" in diag["message"]
    assert diag["line"] == 3


def test_forward_reference_between_views(schemas):
    files = [
        {"filename": "a.sql", "sql": "CREATE VIEW v_a AS\nSELECT b.order_id AS order_id\nFROM v_b b;\n"},
        {"filename": "b.sql", "sql": "CREATE VIEW v_b AS\nSELECT o.order_id AS order_id\nFROM ods_orders o;\n"},
    ]
    result = lineage.build_lineage(files, schemas)
    assert result["parsed_count"] == 2
    edge = next(e for e in result["column_edges"] if e["target_table"] == "v_a")
    # 边保留最近一层来源（v_b），再往下由 v_b 自己的边承接
    assert (edge["source_table"], edge["source_column"]) == ("v_b", "order_id")
    inner = next(e for e in result["column_edges"] if e["target_table"] == "v_b")
    assert (inner["source_table"], inner["source_column"]) == ("ods_orders", "order_id")


def _usage(analysis, source_table, source_column, target_table, usage):
    return [
        u
        for u in analysis["usages"]
        if (u["source_table"], u["source_column"], u["target_table"], u["usage"])
        == (source_table, source_column, target_table, usage)
    ]


def test_constraint_usages_resolved(analysis):
    # 05：JOIN ON 双端 + WHERE 都解析到基表列
    assert _usage(analysis, "dwd_order", "customer_id", "dws_customer_order", "join")
    assert _usage(analysis, "dwd_customer", "customer_id", "dws_customer_order", "join")
    assert _usage(analysis, "dwd_order", "status", "dws_customer_order", "filter")
    # 04：子查询里的 WHERE 归属整条语句的目标对象
    assert _usage(analysis, "ods_order_items", "qty", "dwd_order_item", "filter")
    # 08：JOIN ON 引用 dim_customer.customer_id（该列没有被 SELECT）
    assert _usage(analysis, "dim_customer", "customer_id", "dws_order_wide", "join")
    # 09：GROUP BY 穿透子查询到 dim_product.category；JOIN 双端到两张基表
    assert _usage(analysis, "dim_product", "category", "ads_category_report", "group")
    assert _usage(analysis, "dim_product", "sku", "ads_category_report", "join")
    assert _usage(analysis, "dwd_order_item", "sku", "ads_category_report", "join")
    # 10：JOIN / GROUP BY 穿透两层子查询
    assert _usage(analysis, "dws_customer_order", "customer_id", "ads_customer_rank", "join")
    assert _usage(analysis, "v_customer", "customer_name", "ads_customer_rank", "group")


def test_constraint_usage_keeps_evidence(analysis):
    hit = _usage(analysis, "dws_customer_order", "customer_id", "ads_customer_rank", "join")
    assert hit[0]["script"] == "10_ads_customer_rank.sql"
    assert hit[0]["line"] > 0
    assert "customer_id" in hit[0]["expr"]


def test_unknown_constraint_column_is_diagnosed(schemas):
    files = [
        {
            "filename": "bad_filter.sql",
            "sql": "CREATE VIEW v_bad_filter AS\nSELECT o.order_id AS order_id\nFROM ods_orders o\nWHERE o.not_a_column = 1;\n",
        }
    ]
    result = lineage.build_lineage(files, schemas)
    assert result["failed_count"] == 1
    diag = result["diagnostics"][0]
    assert "not_a_column" in diag["message"]
    assert diag["line"] == 4
