"""Ontology, temporal accounting and optional live Neo4j integration checks."""

import json
import os
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from singhacks26.knowledge_graph import KnowledgeGraph, build_projection, identity
from singhacks26.ontology import RELATIONSHIPS

ROOT = Path(__file__).parents[1]


def test_secure_driver_uses_verified_system_trust(monkeypatch):
    import ssl
    from unittest.mock import Mock

    from singhacks26 import knowledge_graph

    factory = Mock()
    monkeypatch.setattr(knowledge_graph.GraphDatabase, "driver", factory)
    graph = KnowledgeGraph("neo4j+s://example.invalid", "neo4j", "test")
    args, kwargs = factory.call_args
    assert args == ("neo4j://example.invalid",)
    context = kwargs["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    graph.close()


def test_local_driver_keeps_original_transport(monkeypatch):
    from unittest.mock import Mock

    from singhacks26 import knowledge_graph

    factory = Mock()
    monkeypatch.setattr(knowledge_graph.GraphDatabase, "driver", factory)
    graph = KnowledgeGraph("bolt://localhost:7687", "neo4j", "test")
    args, kwargs = factory.call_args
    assert args == ("bolt://localhost:7687",)
    assert "ssl_context" not in kwargs
    graph.close()


@pytest.fixture(scope="module")
def data():
    names = [
        "clients",
        "portfolios",
        "instruments",
        "holdings",
        "mandates",
        "planned_cash_needs",
        "commitments",
        "credit_facilities",
        "transactions",
        "event_log",
        "market_context",
    ]
    result = {name: pd.read_csv(ROOT / "data" / f"{name}.csv") for name in names}
    result["rm_notes"] = json.loads((ROOT / "data/rm_notes.json").read_text(encoding="utf-8"))
    return result


@pytest.fixture(scope="module")
def projection(data):
    return build_projection(data)


def test_complete_source_coverage_and_relationship_contract(projection, data):
    assert not projection.issues
    counts = Counter(n["label"] for n in projection.nodes.values())
    assert counts["Client"] == 20
    assert counts["Portfolio"] == 24
    assert counts["Position"] == len(data["holdings"])
    assert counts["Event"] == len(data["event_log"])
    assert counts["Note"] == len(data["rm_notes"])
    for edge in projection.edges:
        assert (
            projection.nodes[edge["start"]]["label"],
            projection.nodes[edge["end"]]["label"],
        ) == RELATIONSHIPS[edge["kind"]]
        assert edge["source"]


def test_temporal_positions_match_household_totals(projection, data):
    positions = [n["properties"] for n in projection.nodes.values() if n["label"] == "Position"]
    actual = pd.DataFrame(positions).groupby(["client_id", "snapshot_date"]).market_value_usd.sum()
    expected = data["holdings"].groupby(["client_id", "snapshot_date"]).market_value_usd.sum()
    pd.testing.assert_series_equal(actual, expected)
    assert len(actual.index.get_level_values("snapshot_date").unique()) == 5
    assert any(p["valuation_date"] != p["snapshot_date"] for p in positions)


def test_custody_has_no_mandate_and_notes_are_not_verified(projection, data):
    custody = data["portfolios"].query("service_model == 'Custody'").portfolio_id
    governed = {e["start"] for e in projection.edges if e["kind"] == "GOVERNED_BY"}
    assert all(identity("Portfolio", p) not in governed for p in custody)
    assert all(
        n["properties"]["assertion_kind"] == "rm_observation"
        for n in projection.nodes.values()
        if n["label"] == "Note"
    )
    assert not any(projection.nodes[e["start"]]["label"] == "Event" for e in projection.edges)


def test_rebuild_is_deterministic_and_changed_data_changes_version(data, projection):
    assert build_projection(data).digest == projection.digest
    changed = {**data, "holdings": data["holdings"].copy()}
    changed["holdings"].loc[0, "market_value_usd"] += 1
    assert build_projection(changed).digest != projection.digest


def test_missing_references_are_reported(data):
    broken = {**data, "instruments": data["instruments"].iloc[1:].copy()}
    assert any("OF_INSTRUMENT" in issue for issue in build_projection(broken).issues)


def test_conflicting_positions_and_wrong_ownership_rejected(data):
    broken = {**data, "holdings": pd.concat([data["holdings"], data["holdings"].iloc[:1]])}
    with pytest.raises(ValueError, match="duplicate identity"):
        build_projection(broken)
    broken = {**data, "holdings": data["holdings"].copy()}
    broken["holdings"].loc[0, "client_id"] = "CL-9999"
    with pytest.raises(ValueError, match="ownership mismatch"):
        build_projection(broken)


def test_graph_page_without_server(data, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from singhacks26 import graph_ui

    monkeypatch.setattr(graph_ui, "load_dotenv", lambda: None)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from singhacks26.graph_ui import render_knowledge_graph\n"
        "render_knowledge_graph(st.session_state['data'])\n"
    )
    app.session_state["data"] = data
    app.run()
    assert not app.exception
    assert any("Connect Neo4j" in info.value for info in app.info)


def test_graph_page_exploration(data, projection, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from singhacks26 import graph_ui

    class GraphStub:
        def close(self):
            pass

        def client_positions(self, client, snapshot):
            assert snapshot == "2026-08-26"
            return [{"value_usd": 100, "graph_version": projection.digest}]

        def client_relationships(self, client, snapshot):
            return [
                {
                    "source": "Client:1",
                    "relationship": "OWNS",
                    "target": "Portfolio:1",
                    "evidence": "portfolios.csv#record=1",
                }
            ]

    monkeypatch.setattr(graph_ui, "load_dotenv", lambda: None)
    monkeypatch.setattr(graph_ui.KnowledgeGraph, "from_env", GraphStub)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from singhacks26.graph_ui import render_knowledge_graph\n"
        "render_knowledge_graph(st.session_state['data'])\n"
    )
    app.session_state["data"] = data
    app.run()
    app.button[1].click().run()
    assert not app.exception
    assert app.metric[0].value == "USD 100"


@pytest.mark.skipif(os.getenv("AURUM_NEO4J_TEST") != "1", reason="Requires dedicated Neo4j test DB")
def test_live_import_idempotency_and_temporal_query(data, projection):
    graph = KnowledgeGraph.from_env()
    try:
        graph.import_projection(projection)
        graph.import_projection(projection)
        for snapshot in sorted(data["holdings"].snapshot_date.unique()):
            actual = graph.client_positions("CL-0003", snapshot)
            expected = data["holdings"].query(
                "client_id == 'CL-0003' and snapshot_date == @snapshot"
            )
            assert len(actual) == len(expected)
            assert sum(row["value_usd"] for row in actual) == pytest.approx(
                expected.market_value_usd.sum()
            )
        with graph.driver.session(database=graph.database) as session:
            count = session.run(
                "MATCH (n:AurumEntity {version: $version}) RETURN count(n) AS n",
                version=projection.digest,
            ).single()["n"]
        assert count == len(projection.nodes)
        assert graph.client_relationships("CL-0003", "2026-08-26")
    finally:
        graph.close()
