"""Validated, temporal Neo4j projection of the authoritative local dataset."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import truststore
from neo4j import GraphDatabase

from singhacks26.ontology import CLASSES, RELATIONSHIPS, VERSION
from singhacks26.workbench import CURATED_THEMES


def identity(label: str, *parts: Any) -> str:
    """Unambiguous stable identity, including dates for temporal observations."""
    return label + ":" + json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)


@dataclass
class Projection:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def add_node(self, label, parts, properties):
        if label not in CLASSES:
            raise ValueError(f"Unknown ontology class: {label}")
        key = identity(label, *parts)
        props = {k: v for k, v in properties.items() if pd.notna(v)}
        node = {"key": key, "label": label, "properties": props}
        if key in self.nodes and self.nodes[key] != node:
            raise ValueError(f"Conflicting duplicate identity: {key}")
        self.nodes[key] = node
        return key

    def link(self, kind, start, end, source):
        domain, target = RELATIONSHIPS[kind]
        if start not in self.nodes or end not in self.nodes:
            self.issues.append(f"Unresolved {kind}: {start} -> {end} ({source})")
            return
        if (self.nodes[start]["label"], self.nodes[end]["label"]) != (domain, target):
            raise ValueError(f"Invalid domain/range for {kind}")
        self.edges.append({"kind": kind, "start": start, "end": end, "source": source})

    @property
    def digest(self):
        payload = json.dumps(
            {"version": VERSION, "nodes": self.nodes, "edges": self.edges},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def build_projection(data: dict[str, Any]) -> Projection:
    """Preserve source values; do not infer causes or parse prose into facts.

    Row numbers denote data records (header excluded), not physical lines when
    CSV fields contain embedded newlines. Missing foreign keys become issues.
    """
    graph = Projection()
    tables = {
        "clients": ("Client", ["client_id"]),
        "portfolios": ("Portfolio", ["portfolio_id"]),
        "instruments": ("Instrument", ["instrument_id"]),
        "holdings": ("Position", ["portfolio_id", "instrument_id", "snapshot_date"]),
        "mandates": ("AllocationBand", ["mandate_code", "asset_class"]),
        "planned_cash_needs": ("CashNeed", ["need_id"]),
        "commitments": ("Commitment", ["commitment_id"]),
        "credit_facilities": ("CreditFacility", ["facility_id"]),
        "transactions": ("Transaction", ["transaction_id"]),
        "event_log": ("Event", ["event_date", "event_type", "description"]),
        "market_context": ("MarketObservation", ["series_id", "snapshot_date"]),
    }
    records = []
    for table, (label, keys) in tables.items():
        for number, row in enumerate(data[table].to_dict("records"), start=1):
            if any(pd.isna(row.get(key)) for key in keys):
                raise ValueError(f"Missing identity in {table} record {number}")
            source = f"{table}.csv#record={number}"
            key = graph.add_node(
                label,
                [row[k] for k in keys],
                {
                    **row,
                    "source": source,
                    "source_file": f"{table}.csv",
                    "source_record": number,
                    "assertion_kind": "source_record",
                },
            )
            records.append((label, key, row, source))
            if label == "AllocationBand":
                graph.add_node(
                    "Mandate",
                    [row["mandate_code"]],
                    {
                        "mandate_code": row["mandate_code"],
                        "name": row["mandate_name"],
                        "source_file": "mandates.csv",
                    },
                )

    for label, key, row, source in records:

        def link(kind, other_label, *parts, reverse=False, key=key, source=source):
            other = identity(other_label, *parts)
            graph.link(kind, other if reverse else key, key if reverse else other, source)

        if label == "Portfolio":
            link("OWNS", "Client", row["client_id"], reverse=True)
            if row["service_model"] != "Custody":
                link("GOVERNED_BY", "Mandate", row["mandate_code"])
        elif label == "Position":
            link("HAS_POSITION", "Portfolio", row["portfolio_id"], reverse=True)
            link("OF_INSTRUMENT", "Instrument", row["instrument_id"])
            portfolio = graph.nodes.get(identity("Portfolio", row["portfolio_id"]))
            if portfolio and portfolio["properties"]["client_id"] != row["client_id"]:
                raise ValueError(f"Position/client ownership mismatch: {source}")
        elif label == "AllocationBand":
            link("HAS_BAND", "Mandate", row["mandate_code"], reverse=True)
        elif label == "CashNeed":
            link("HAS_CASH_NEED", "Client", row["client_id"], reverse=True)
        elif label == "Commitment":
            link("HAS_COMMITMENT", "Client", row["client_id"], reverse=True)
            link("COMMITTED_IN", "Portfolio", row["portfolio_id"])
        elif label == "CreditFacility":
            link("HAS_FACILITY", "Client", row["client_id"], reverse=True)
            link("SECURED_BY", "Portfolio", row["collateral_portfolio_id"])
        elif label == "Transaction":
            link("HAS_TRANSACTION", "Portfolio", row["portfolio_id"], reverse=True)
            if pd.notna(row["instrument_id"]):
                link("TRANSACTS_IN", "Instrument", row["instrument_id"])

    for row in data["rm_notes"]:
        source = f"rm_notes.json#note_id={row['note_id']}"
        key = graph.add_node(
            "Note",
            [row["note_id"]],
            {
                **row,
                "source": source,
                "assertion_kind": "rm_observation",
            },
        )
        graph.link("HAS_NOTE", identity("Client", row["client_id"]), key, source)
    for theme, instruments in sorted(CURATED_THEMES.items()):
        source = "src/singhacks26/workbench.py:CURATED_THEMES"
        key = graph.add_node(
            "Theme",
            [theme],
            {
                "name": theme,
                "source": source,
                "assertion_kind": "curated_mapping",
            },
        )
        for instrument in sorted(instruments):
            graph.link("MAPPED_TO", identity("Instrument", instrument), key, source)
    return graph


class KnowledgeGraph:
    """Neo4j adapter. All writes are scoped to a versioned Aurum projection.

    Imports use a single transaction and immutable content-addressed versions.
    The active pointer changes only after a complete import; old versions remain.
    """

    def __init__(self, uri, username, password, database="neo4j"):
        tls_options = {}
        # Neo4j's +s shorthand disallows a custom SSL context. Preserve verified
        # TLS explicitly while using the native OS certificate trust store.
        if uri.startswith(("neo4j+s://", "bolt+s://")):
            uri = uri.replace("+s://", "://", 1)
            tls_options["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.driver = GraphDatabase.driver(
            uri,
            auth=(username, password),
            connection_timeout=5,
            **tls_options,
        )
        self.database = database

    @classmethod
    def from_env(cls):
        uri, password = os.getenv("NEO4J_URI"), os.getenv("NEO4J_PASSWORD")
        if not uri or not password:
            raise ValueError("Set NEO4J_URI and NEO4J_PASSWORD to connect.")
        return cls(
            uri,
            os.getenv("NEO4J_USERNAME", "neo4j"),
            password,
            os.getenv("NEO4J_DATABASE", "neo4j"),
        )

    def close(self):
        self.driver.close()

    def import_projection(self, graph: Projection):
        if graph.issues:
            raise ValueError("Resolve missing references before import: " + "; ".join(graph.issues))
        groups, edges = defaultdict(list), defaultdict(list)
        version = graph.digest
        for node in graph.nodes.values():
            groups[node["label"]].append(
                {
                    "uid": version + "/" + node["key"],
                    "properties": {**node["properties"], "key": node["key"], "version": version},
                }
            )
        for edge in graph.edges:
            edges[edge["kind"]].append(
                {
                    "start": version + "/" + edge["start"],
                    "end": version + "/" + edge["end"],
                    "source": edge["source"],
                }
            )
        with self.driver.session(database=self.database) as session:
            session.run(
                "CREATE CONSTRAINT aurum_uid IF NOT EXISTS "
                "FOR (n:AurumEntity) REQUIRE n.uid IS UNIQUE"
            ).consume()
            session.run(
                "CREATE CONSTRAINT aurum_catalog IF NOT EXISTS "
                "FOR (n:AurumCatalog) REQUIRE n.name IS UNIQUE"
            ).consume()

            def write(tx):
                for label, rows in groups.items():
                    if label not in CLASSES:
                        raise ValueError("Unknown class")
                    tx.run(
                        f"UNWIND $rows AS row MERGE (n:AurumEntity:{label} {{uid: row.uid}}) "
                        "SET n += row.properties",
                        rows=rows,
                    ).consume()
                for kind, rows in edges.items():
                    if kind not in RELATIONSHIPS:
                        raise ValueError("Unknown relationship")
                    tx.run(
                        "UNWIND $rows AS row MATCH (a:AurumEntity {uid: row.start}), "
                        "(b:AurumEntity {uid: row.end}) "
                        f"MERGE (a)-[r:{kind}]->(b) SET r.source = row.source",
                        rows=rows,
                    ).consume()
                tx.run(
                    "MERGE (c:AurumCatalog {name: 'aurum'}) "
                    "SET c.active_version = $version, c.ontology_version = $ontology",
                    version=version,
                    ontology=VERSION,
                ).consume()

            session.execute_write(write)
        return version

    def client_positions(self, client_id, snapshot):
        """Explicit date and household traversal prevent cross-snapshot double counting."""
        query = """
        MATCH (catalog:AurumCatalog {name: 'aurum'})
        MATCH (c:AurumEntity:Client {client_id: $client})-[:OWNS]->(p:Portfolio)
              -[:HAS_POSITION]->(h:Position)-[:OF_INSTRUMENT]->(i:Instrument)
        WHERE c.version = catalog.active_version AND h.snapshot_date = $snapshot
        RETURN p.portfolio_id AS portfolio, i.instrument_id AS instrument,
               i.instrument_name AS name, h.market_value_usd AS value_usd,
               h.liquidity_tier AS liquidity, h.valuation_date AS valuation_date,
               i.underlying_reference AS underlying_reference, h.source AS source,
               c.version AS graph_version
        ORDER BY value_usd DESC
        """
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: tx.run(
                    query,
                    client=client_id,
                    snapshot=snapshot,
                ).data()
            )

    def client_relationships(self, client_id, snapshot):
        query = """
        MATCH (catalog:AurumCatalog {name: 'aurum'})
        MATCH path=(c:AurumEntity:Client {client_id: $client})-[*1..4]->(n:AurumEntity)
        WHERE c.version = catalog.active_version
          AND all(node IN nodes(path) WHERE NOT node:Position OR node.snapshot_date = $snapshot)
        UNWIND relationships(path) AS r
        WITH DISTINCT r
        RETURN startNode(r).key AS source, type(r) AS relationship,
               endNode(r).key AS target, r.source AS evidence
        ORDER BY source, relationship, target LIMIT 500
        """
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: tx.run(
                    query,
                    client=client_id,
                    snapshot=snapshot,
                ).data()
            )
