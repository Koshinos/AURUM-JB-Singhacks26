"""Optional Neo4j explorer; no connection or import happens on app startup."""

import json
from collections import Counter

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from neo4j.exceptions import ConfigurationError, Neo4jError, ServiceUnavailable, SessionExpired

from singhacks26.knowledge_graph import KnowledgeGraph, build_projection
from singhacks26.ontology import CLASSES, RELATIONSHIPS, VERSION


def render_knowledge_graph(data):
    st.title("Knowledge graph")
    st.caption("Explore dated holdings and the source relationships behind each client.")
    load_dotenv()
    with st.expander("Ontology and source coverage"):
        st.caption(f"Ontology {VERSION} · local CSV and RM-note projection")
        st.dataframe(
            pd.DataFrame(
                [{"Class": name, "Meaning": meaning} for name, meaning in CLASSES.items()]
            ),
            hide_index=True,
            width="stretch",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"From": domain, "Relationship": kind, "To": target}
                    for kind, (domain, target) in RELATIONSHIPS.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.info(
            "Events retain the controlled event log. No causal event-to-client links are "
            "invented. Notes remain RM observations, and themes are curated mappings."
        )

    try:
        projection = build_projection(data)
    except ValueError as exc:
        st.error(f"Source validation failed: {exc}")
        return
    counts = Counter(node["label"] for node in projection.nodes.values())
    st.caption(
        f"Source projection: {len(projection.nodes):,} entities · "
        f"{len(projection.edges):,} relationships · "
        f"{counts['Position']:,} dated positions"
    )
    if projection.issues:
        st.error("Unresolved source references block import.")
        st.write(projection.issues)
        return
    try:
        graph = KnowledgeGraph.from_env()
    except (ValueError, ConfigurationError):
        st.info(
            "Connect Neo4j by setting NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD "
            "in your local .env. See docs/KNOWLEDGE_GRAPH.md for setup."
        )
        return
    try:
        if st.button("Import current dataset", type="primary"):
            with st.spinner("Validating and importing the source graph…"):
                version = graph.import_projection(projection)
            st.success(f"Dataset imported. Version {version[:12]}.")
        client_id = st.selectbox("Client", data["clients"].client_id.tolist(), key="kg_client")
        snapshot = st.selectbox(
            "Holdings snapshot",
            sorted(
                data["holdings"].snapshot_date.unique(),
                reverse=True,
            ),
            key="kg_snapshot",
        )
        if st.button("Explore client"):
            positions = graph.client_positions(client_id, snapshot)
            relationships = graph.client_relationships(client_id, snapshot)
            if not positions:
                st.info(
                    "No positions found. Import the dataset first, then select a client and date."
                )
                return
            if positions[0]["graph_version"] != projection.digest:
                st.warning(
                    "The imported graph differs from the current source files. "
                    "Import the current dataset to refresh it. Showing the imported version."
                )
            st.metric(
                "Household holdings value", f"USD {sum(p['value_usd'] for p in positions):,.0f}"
            )
            st.dataframe(pd.DataFrame(positions), hide_index=True, width="stretch")
            st.caption(
                "Relationships are capped at 500. Positions use the selected date; "
                "notes and obligations retain their original dates."
            )
            # JSON quoting safely escapes source-controlled strings for DOT labels.
            dot = ["digraph Aurum {", 'rankdir="LR";', 'node [shape="box"];']
            for edge in relationships:
                dot.append(
                    f"{json.dumps(edge['source'])} -> {json.dumps(edge['target'])} "
                    f"[label={json.dumps(edge['relationship'])}];"
                )
            dot.append("}")
            st.graphviz_chart("\n".join(dot), width="stretch")
            with st.expander("Relationship evidence"):
                st.dataframe(pd.DataFrame(relationships), hide_index=True, width="stretch")
    except (Neo4jError, ServiceUnavailable, SessionExpired, OSError):
        st.error(
            "Neo4j could not complete the request. Check the server, credentials, database "
            "and write permissions. The existing workbench remains available."
        )
    finally:
        graph.close()
