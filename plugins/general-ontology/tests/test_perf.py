"""Size targets: 5,000 nodes and 15,000 edges load in under 2 s, and applying 10 ops takes under 2 s.

Skip with ``ONTO_SKIP_PERF=1`` (read before the harness clears the ``ONTO_*`` variables)."""

from __future__ import annotations

import os
import time
import unittest

SKIP = bool(os.environ.get("ONTO_SKIP_PERF"))

from tests import _support  # noqa: E402
from tests.test_graph import SRC, mk_edge, mk_node, write_topic  # noqa: E402
from ontokit import graph, pipeline, store, validate  # noqa: E402

N_NODES = 5000
N_EDGES = 15000
KINDS = ("role", "process", "dataset", "term", "goal", "tool", "constraint", "deliverable")
RELS = ("related_to", "supports", "part_of", "about", "derived_from")


def build(tmp):
    nodes = [mk_node("%s:item-%05d" % (KINDS[i % len(KINDS)], i), "Item %05d" % i) for i in range(N_NODES)]
    ids = [n["id"] for n in nodes]
    edges = []
    for i in range(N_EDGES):
        rnd = i // N_NODES
        a = i % N_NODES
        b = (a + 1 + rnd * 101) % N_NODES
        rel = RELS[(i + rnd) % len(RELS)]
        edges.append(mk_edge(ids[a], rel, ids[b], symmetric=rel == "related_to"))
    return write_topic(tmp, nodes, edges, ns="big")


@unittest.skipIf(SKIP, "ONTO_SKIP_PERF is set")
class PerfTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        graph.clear_cache()
        store.clear_cache()
        self.repo = build(self.tmp)

    def test_load_under_2s(self):
        started = time.perf_counter()
        onto = graph.Ontology.load(self.repo)
        elapsed = time.perf_counter() - started
        self.assertEqual((onto.stats["nodes"], onto.stats["edges"]), (N_NODES, N_EDGES))
        self.assertLess(elapsed, 2.0)

    def test_apply_10_ops_under_2s(self):
        graph.Ontology.load(self.repo)
        ops = []
        for i in range(5):
            ops.append({"op": "add_node", "ref": "$n%d" % i, "node": {"kind": "term", "name": "New term %d" % i},
                        "prov": [{"src": SRC, "loc": "L1-L1", "quote": "line one", "by": "agent"}]})
            edge = {"src": "$n%d" % i, "rel": "about", "dst": "role:item-%05d" % (i * 8)}
            ops.append({"op": "add_edge", "edge": edge, "prov": [{"src": SRC, "loc": "L1-L1", "by": "agent"}]})
        prop = pipeline.prepare(self.repo, {"source": SRC, "ops": ops})
        pipeline.review(self.repo, prop["id"], {str(n): "accept" for n in range(1, 11)})
        started = time.perf_counter()
        applied = pipeline.commit(self.repo, prop["id"])
        elapsed = time.perf_counter() - started
        self.assertEqual(len(applied["results"]), 10)
        self.assertLess(elapsed, 2.0)
        graph.clear_cache()
        onto = graph.Ontology.load(self.repo)
        self.assertEqual(onto.stats["nodes"], N_NODES + 5)
        self.assertEqual(validate.check_graph(onto, applied["ids"]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
