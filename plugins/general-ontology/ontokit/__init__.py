"""ontokit: a deterministic, standard-library kit for building a topic ontology one discovery at a time.

The kit stores, checks, resolves, searches, ranks and renders; an agent asks the questions and drafts proposals;
a human reviews. Modules are layered (see the kit README): ``util``, ``errors``, ``ids``, ``schema_lite`` and
``secrets`` at the bottom, then ``store``, ``gitutil`` and ``records``, then the packs, ledger and graph layers.
"""

from __future__ import annotations

__version__ = "0.3.0"
FORMAT = 1

__all__ = ["__version__", "FORMAT"]
