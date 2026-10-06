"""The L2 stores: decisions and changes (ledger), the richness history, the source store and the import lock."""

from __future__ import annotations

import os
import unittest
from datetime import timedelta

from tests import _support
from tests.test_graph import add_import, garden_export, make_export, mk_node
from ontokit import errors, history, ledger, lockfile, records, sources, store, util


def repo_in(tmp, ns="t"):
    return store.Repo.open(_support.bare_topic(tmp, ns))


class DecisionTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = repo_in(self.tmp)

    def decide(self, **kw):
        args = dict(question="Track watering per bed or per steward?", options=["bed=Per bed", "steward=Per steward"],
                    chosen="bed", rationale="Beds change stewards", scope=["dataset:harvest-log", "garden/"])
        args.update(kw)
        return ledger.decide(self.repo, **args)

    def test_decide_writes_file_and_change(self):
        dec = self.decide(recommended="bed")
        self.assertEqual(records.check(dec, "decision"), [])
        self.assertTrue(dec["id"].startswith("dec-20260928-track-watering-per-bed-or-per-steward-"))
        on_disk = store.read_json(ledger.decision_path(self.repo, dec["id"]))
        self.assertEqual(on_disk, dec)
        changes = ledger.read_changes(self.repo)
        self.assertEqual([(c["type"], c["ids"]) for c in changes], [("decide", [dec["id"]])])

    def test_same_decision_twice_is_one(self):
        one = self.decide()
        two = self.decide()
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(len(ledger.all_decisions(self.repo)), 1)

    def test_supersede_changes_only_status_and_superseded_by(self):
        old = self.decide()
        new = self.decide(chosen="other", chosen_text="Per bed, noting the steward", supersedes=old["id"])
        after = store.read_json(ledger.decision_path(self.repo, old["id"]))
        self.assertEqual(after["status"], "superseded")
        self.assertEqual(after["superseded_by"], new["id"])
        self.assertEqual({k: v for k, v in after.items() if k not in ("status", "superseded_by")},
                         {k: v for k, v in old.items() if k not in ("status", "superseded_by")})
        self.assertEqual(new["supersedes"], old["id"])
        with self.assertRaises(errors.Refused):
            self.decide(chosen="steward", supersedes=old["id"])
        with self.assertRaises(errors.NotFound):
            self.decide(supersedes="dec-20260928-nothing-0000")
        self.assertEqual(ledger.read_changes(self.repo)[-1]["ids"], [new["id"], old["id"]])

    def test_going_back_to_an_earlier_choice_gets_a_new_id(self):
        first = self.decide()
        second = self.decide(chosen="steward", supersedes=first["id"])
        third = self.decide(supersedes=second["id"])
        self.assertNotEqual(third["id"], first["id"])
        self.assertTrue(third["id"].startswith(first["id"]))

    def test_choice_rules(self):
        with self.assertRaises(errors.UsageError):
            self.decide(chosen="plot")
        with self.assertRaises(errors.UsageError):
            self.decide(chosen="other")
        with self.assertRaises(errors.UsageError):
            self.decide(recommended="plot")
        with self.assertRaises(errors.UsageError):
            self.decide(question="  ")
        free = self.decide(options=[], chosen="weekly", scope=[])
        self.assertEqual(free["options"], [])
        self.assertEqual(free["scope"], [])

    def test_secret_in_a_decision_is_refused(self):
        with self.assertRaises(errors.Refused):
            self.decide(rationale="token " + _support.fake_secret("github"))
        self.assertEqual(ledger.all_decisions(self.repo), {})

    def test_read_decisions_filters(self):
        a = self.decide()
        b = self.decide(question="Who keeps the mint bed?", options=[], chosen="the coordinator",
                        scope=["crop:mint"])
        self.assertEqual({d["id"] for d in ledger.read_decisions(self.repo)}, {a["id"], b["id"]})
        self.assertEqual(ledger.read_decisions(self.repo, scope=[]), [])
        self.assertEqual([d["id"] for d in ledger.read_decisions(self.repo, scope=["crop:mint"])], [b["id"]])
        self.assertEqual(ledger.read_decisions(self.repo, scope=["crop:mint-2"]), [])
        self.assertEqual([d["id"] for d in ledger.read_decisions(self.repo, scope=["garden/crop:tomato"])],
                         [a["id"]])
        self.assertEqual([d["id"] for d in ledger.read_decisions(self.repo, text="MINT coordinator")], [b["id"]])
        self.assertEqual(ledger.read_decisions(self.repo, text="mint bed compost"), [])
        c = self.decide(question="Who keeps the mint bed?", options=[], chosen="a steward", scope=["crop:mint"],
                        supersedes=b["id"])
        self.assertEqual([d["id"] for d in ledger.read_decisions(self.repo, scope="crop:mint")], [c["id"]])
        both = ledger.read_decisions(self.repo, scope=["crop:mint"], active=False)
        self.assertEqual({d["id"] for d in both}, {b["id"], c["id"]})


class ScopeTest(unittest.TestCase):
    def test_scope_overlaps(self):
        yes = [("crop:mint", "crop:mint"), ("crop:mint", "crop:mint.leaf"), ("garden/", "garden/crop:mint"),
               ("garden", "garden/crop:mint"), ("crop:mint", "crop:mint@v2"), ("crop", "crop:mint"),
               ("CROP:Mint", "crop:mint"), ("./notes", "notes/a"), ("area#x", "area")]
        for a, b in yes:
            self.assertTrue(ledger.scope_overlaps(a, b), (a, b))
            self.assertTrue(ledger.scope_overlaps(b, a), (b, a))
        no = [("crop:mint", "crop:mint-2"), ("crop:mint", "crop:minty"), ("garden", "gardens/x"), ("", "crop")]
        for a, b in no:
            self.assertFalse(ledger.scope_overlaps(a, b), (a, b))
        self.assertFalse(ledger.scope_overlaps([], ["crop:mint"]))
        self.assertFalse(ledger.scope_overlaps(["crop:mint"], []))
        self.assertTrue(ledger.scope_overlaps(["a:b", "crop:mint"], ["crop:mint.leaf"]))


class ChangeTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = repo_in(self.tmp)

    def test_append_change_ids_unique_under_a_fixed_clock(self):
        one = ledger.append_change(self.repo, "apply", "user", ["role:a"], "one op")
        two = ledger.append_change(self.repo, "apply", "user", ["role:a"], "one op")
        self.assertNotEqual(one["id"], two["id"])
        self.assertTrue(two["id"].startswith(one["id"]))
        rows = ledger.read_changes(self.repo)
        self.assertEqual([r["id"] for r in rows], [one["id"], two["id"]])
        for row in rows:
            self.assertEqual(records.check(row, "change"), [])

    def test_extra_and_bad_input(self):
        row = ledger.append_change(self.repo, "import", "kit", [], "import garden", extra={"ns": "garden"})
        self.assertEqual(row["extra"], {"ns": "garden"})
        with self.assertRaises(errors.UsageError):
            ledger.append_change(self.repo, "delete", "user", [], "nope")
        with self.assertRaises(errors.UsageError):
            ledger.append_change(self.repo, "apply", "user", [], "x", colour="blue")
        self.assertEqual(ledger.append_change(self.repo, "apply", "owner", [], "x")["by"], "user")

    def test_checkpoint_and_last(self):
        ledger.append_change(self.repo, "apply", "user", ["role:a"], "one op")
        cp = ledger.checkpoint(self.repo, ["asked about data"], ["ask about owners"], ["who owns the log?"])
        self.assertNotIn("ids", cp)
        self.assertEqual(cp["open_questions"], ["who owns the log?"])
        self.assertEqual(records.check(cp, "change"), [])
        self.assertEqual(ledger.last_checkpoint(self.repo)["id"], cp["id"])
        self.assertNotEqual(ledger.last_change(self.repo), cp["id"])

    def test_read_changes_since(self):
        ledger.append_change(self.repo, "apply", "user", [], "now")
        self.assertEqual(len(ledger.read_changes(self.repo, since="7d")), 1)
        self.assertEqual(len(ledger.read_changes(self.repo, since="2026-09-29")), 0)
        self.assertEqual(len(ledger.read_changes(self.repo, since="2026-09-28T12:00:00Z")), 1)
        with self.assertRaises(errors.UsageError):
            ledger.read_changes(self.repo, since="last tuesday")


def point(at, richness, label=None, kind="apply"):
    return history.new_point(kind, {"richness": richness}, breakdowns={"n": richness}, label=label, at=at)


class HistoryTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = repo_in(self.tmp)

    def test_point_skip_and_label(self):
        self.assertTrue(history.append_point(self.repo, point("2026-09-20T00:00:00Z", 10)))
        self.assertFalse(history.append_point(self.repo, point("2026-09-21T00:00:00Z", 10)))
        self.assertTrue(history.append_point(self.repo, point("2026-09-22T00:00:00Z", 10, label="v1",
                                                              kind="release")))
        self.assertFalse(history.append_point(self.repo, point("2026-09-23T00:00:00Z", 10, label="v1",
                                                               kind="release")))
        self.assertTrue(history.append_point(self.repo, point("2026-09-24T00:00:00Z", 11)))
        self.assertEqual([p["values"]["richness"] for p in history.read(self.repo)], [10, 10, 11])
        with self.assertRaises(errors.UsageError):
            history.append_point(self.repo, {"at": "yesterday"})

    def test_baseline_nearest_half_window(self):
        points = [point("2026-09-%02dT00:00:00Z" % d, d) for d in (1, 18, 20, 24, 27)]
        base = history.baseline(points, "richness", 7)
        self.assertEqual(base["at"], "2026-09-20T00:00:00Z")  # 7 days before 09-27; 09-24 is under half the window
        self.assertEqual(history.change(points, "richness", 7), (7, "2026-09-20T00:00:00Z"))
        self.assertEqual(history.baseline(points, "richness", 30)["at"], "2026-09-01T00:00:00Z")
        self.assertIsNone(history.baseline(points[-2:], "richness", 7))
        self.assertIsNone(history.change([], "richness", 7))
        tie = [point("2026-09-19T00:00:00Z", 1), point("2026-09-21T00:00:00Z", 2), point("2026-09-27T00:00:00Z", 3)]
        self.assertEqual(history.baseline(tie, "richness", 7)["at"], "2026-09-19T00:00:00Z")  # a tie keeps the older

    def test_null_values_are_skipped(self):
        points = [point("2026-09-20T00:00:00Z", None), point("2026-09-27T00:00:00Z", 5)]
        self.assertIsNone(history.change(points, "richness", 7))
        self.assertEqual(history.series(points, "richness"), [("2026-09-27T00:00:00Z", 5)])

    def test_fmt_change(self):
        self.assertEqual(history.fmt_change(9, "2026-09-21T00:00:00Z"), "+9 since 09-21")
        self.assertEqual(history.fmt_change(-2, "2026-09-21T00:00:00Z"), "-2 since 09-21")
        self.assertEqual(history.fmt_change(0, "2026-09-21T00:00:00Z"), "+0 since 09-21")
        self.assertEqual(history.fmt_change(None), "-")

    def test_snapshot_and_restore(self):
        self.assertIsNone(history.snapshot_bytes(self.repo))
        history.append_point(self.repo, point("2026-09-20T00:00:00Z", 1))
        saved = history.snapshot_bytes(self.repo)
        history.append_point(self.repo, point("2026-09-21T00:00:00Z", 2))
        history.restore(self.repo, saved)
        self.assertEqual(len(history.read(self.repo)), 1)
        history.restore(self.repo, None)
        self.assertEqual(history.read(self.repo), [])

    def test_merge_points(self):
        a, b = point("2026-09-21T00:00:00Z", 1), point("2026-09-20T00:00:00Z", 2)
        merged = history.merge_points([a], [b, point("2026-09-21T00:00:00Z", 3)])
        self.assertEqual([p["values"]["richness"] for p in merged], [2, 3])


HANDBOOK = "Volunteer handbook\n\nStewards water the tomato beds\non Saturdays.\n\nNo pesticides.\n"


class SourcesTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = repo_in(self.tmp)

    def test_add_is_content_addressed_and_idempotent(self):
        entry, dup = sources.add(self.repo, HANDBOOK.replace("\n", "\r\n"), "note", "Volunteer handbook")
        self.assertFalse(dup)
        self.assertEqual(entry["id"], "src-" + util.sha256_text(HANDBOOK)[:12])
        self.assertEqual(records.check(entry, "source"), [])
        self.assertEqual((entry["trust"], entry["lines"], entry["bytes"]), ("untrusted", 6, len(HANDBOOK)))
        self.assertEqual(sources.read(self.repo, entry["id"]), HANDBOOK)
        again, dup = sources.add(self.repo, HANDBOOK, "note", "Another title")
        self.assertTrue(dup)
        self.assertEqual(again, entry)
        self.assertEqual(list(sources.index(self.repo)), [entry["id"]])
        interview, _ = sources.add(self.repo, "I run the garden.", "interview", "Interview answers")
        self.assertEqual(interview["trust"], "user")

    def test_new_version_supersedes(self):
        first, _ = sources.add(self.repo, "v1 text", "note", "Rota")
        second, _ = sources.add(self.repo, "v2 text", "note", "Rota")
        third, _ = sources.add(self.repo, "v3 text", "note", "Rota")
        self.assertEqual((second["supersedes"], third["supersedes"]), (first["id"], second["id"]))
        by_url, _ = sources.add(self.repo, "page one", "url", "Page", url="https://example.invalid/a")
        newer, _ = sources.add(self.repo, "page two", "url", "Renamed page", url="https://example.invalid/a")
        self.assertEqual(newer["supersedes"], by_url["id"])
        other, _ = sources.add(self.repo, "other", "note", "Different")
        self.assertIsNone(other["supersedes"])

    def test_credentials_refused_and_nothing_written(self):
        before = _support.snapshot(self.repo.root)
        with self.assertRaises(errors.Refused) as ctx:
            sources.add(self.repo, "key: " + _support.fake_secret("aws"), "note", "Leaky note")
        self.assertIn("aws", ctx.exception.to_json()["kinds"])
        self.assertEqual(_support.snapshot(self.repo.root, skip=[".onto"]), before)

    def test_custom_sanitizer_and_bad_input(self):
        def clean(text, policy):
            return text.replace("555-0100", "[phone]"), {"phone": text.count("555-0100")}

        entry, _ = sources.add(self.repo, "call 555-0100", "note", "Contacts", sanitizer=clean)
        self.assertEqual(entry["redactions"], {"phone": 1})
        self.assertEqual(sources.read(self.repo, entry["id"]), "call [phone]\n")
        for bad in (("", "note", "x"), ("text", "memo", "x"), ("text", "note", "  ")):
            with self.assertRaises(errors.UsageError):
                sources.add(self.repo, *bad)

    def test_original_is_hashed_by_the_kit(self):
        raw = os.path.join(self.repo.root, "inbox", "notes.pdf")
        os.makedirs(os.path.dirname(raw))
        with open(raw, "wb") as fh:
            fh.write(b"%PDF raw bytes")
        entry, _ = sources.add(self.repo, "converted text", "file", "Converted notes",
                               original={"path": raw, "keep": True, "converter": "agent:pdf-to-text"})
        self.assertEqual(entry["original"]["sha256"], util.sha256_hex(b"%PDF raw bytes"))
        self.assertEqual(entry["original"]["media_type"], "application/pdf")
        self.assertTrue(entry["original"]["stored"])
        self.assertTrue(os.path.isfile(self.repo.path("sources/%s.orig.pdf" % entry["id"])))

    def test_quote_found(self):
        text = "one\nStewards  water the\ntomato beds\nfour\n"
        self.assertTrue(sources.quote_found(text, "water the tomato beds", "L2-L3"))
        self.assertTrue(sources.quote_found(text, "Stewards water", "L2-L2"))
        self.assertFalse(sources.quote_found(text, "water the tomato beds", "L2-L2"))
        self.assertFalse(sources.quote_found(text, "four", "L2-L3"))
        self.assertFalse(sources.quote_found(text, "one", "L3-L9"))
        self.assertFalse(sources.quote_found(text, "stewards water", "L2-L2"))  # case matters
        self.assertTrue(sources.quote_found(text, "four", "Q:q.data.where"))
        # a T location reads only the cue lines at its stamp: plain text has none
        self.assertFalse(sources.quote_found(text, "tomato beds four", "T00:01:02"))
        cues = "T00:01:02 tomato beds four\nT00:01:09 one\n"
        self.assertTrue(sources.quote_found(cues, "tomato beds four", "T00:01:02"))
        self.assertFalse(sources.quote_found(cues, "one", "T00:01:02"))
        self.assertTrue(sources.quote_found(text, "one", "P1"))
        self.assertFalse(sources.quote_found(text, "   ", "P1"))

    def test_chunks_are_paragraph_aligned(self):
        paras = ["para %d line a\npara %d line b" % (i, i) for i in range(6)]
        text = "\n\n".join(paras) + "\n"
        entry, _ = sources.add(self.repo, text, "note", "Paragraphs")
        chunks = sources.chunk(self.repo, entry["id"], size=60)
        self.assertEqual([c["n"] for c in chunks], list(range(1, len(chunks) + 1)))
        for c in chunks:
            a, b = [int(x[1:]) for x in c["loc"].split("-")]
            self.assertTrue(c["text"].startswith("para"))
            self.assertEqual(c["text"], "\n".join(text.split("\n")[a - 1: b]))
            self.assertTrue(c["text"].endswith("line b"))
        self.assertEqual(sources.chunk(self.repo, entry["id"], n=2, size=60), [chunks[1]])
        with self.assertRaises(errors.UsageError):
            sources.chunk(self.repo, entry["id"], n=99)
        long_para = "\n".join("word %d" % i for i in range(40)) + "\n"
        pieces = sources.chunks_of(long_para, size=50)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("\n".join(p["text"] for p in pieces), long_para.rstrip("\n"))
        self.assertEqual(sources.lines(self.repo, entry["id"], 1, 2), ["para 0 line a", "para 0 line b"])

    def test_stale(self):
        entry = {"captured_at": "2026-09-01T00:00:00Z", "stale_after_days": 7}
        self.assertTrue(sources.stale(entry, util.now()))
        self.assertFalse(sources.stale(entry, util.now() - timedelta(days=21)))
        self.assertFalse(sources.stale({"captured_at": "2026-09-01T00:00:00Z", "stale_after_days": None}))


class LockfileTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = repo_in(self.tmp, "g2t")

    def codes(self):
        return [p.code for p in lockfile.verify(self.repo)]

    def test_absent_lock_reads_empty(self):
        self.assertEqual(lockfile.read(self.repo), {"format": 1, "imports": []})
        self.assertEqual(lockfile.verify(self.repo), [])
        self.assertEqual(lockfile.vendored(self.repo), {})

    def test_verified_import_and_tamper(self):
        entry = add_import(self.repo, "garden", garden_export())
        self.assertEqual(lockfile.verify(self.repo), [])
        self.assertEqual(lockfile.entry(lockfile.read(self.repo), "garden"), entry)
        self.assertEqual(set(lockfile.vendored(self.repo)["garden"]), {"meta", "nodes", "edges", "sources"})
        path = self.repo.path("imports/garden/export.json")
        with open(path, "ab") as fh:
            fh.write(b" ")
        messages = [p.message for p in lockfile.verify(self.repo)]
        self.assertTrue(any("differs from the lock" in m for m in messages))
        self.assertTrue(any("canonical" in m for m in messages))

    def test_write_sorts_entries(self):
        add_import(self.repo, "kitchen", make_export("kitchen", [mk_node("dish:salad")]))
        add_import(self.repo, "garden", garden_export())
        self.assertEqual([e["ns"] for e in lockfile.read(self.repo)["imports"]], ["garden", "kitchen"])

    def test_ns_collisions(self):
        add_import(self.repo, "g2t", make_export("g2t", [mk_node("goal:menu")]))
        self.assertIn("P15", self.codes())
        lock = lockfile.read(self.repo)
        lock["imports"] = [dict(e, ns="garden") for e in lock["imports"]] * 2
        lockfile.write(self.repo, lock)
        messages = [p.message for p in lockfile.verify(self.repo)]
        self.assertTrue(any("listed twice" in m for m in messages))

    def test_bundle_sha_and_pin_conflicts(self):
        garden = garden_export()
        garden_sha = lockfile.bundle_sha(garden)
        mid = make_export("mid", [mk_node("goal:menu")], bundled={"garden": {"sha256": garden_sha, "export": garden}})
        add_import(self.repo, "mid", mid)
        add_import(self.repo, "garden", garden, via="mid")
        self.assertEqual(lockfile.verify(self.repo), [])
        newer = garden_export()
        newer["nodes"].append(mk_node("crop:bean", "Bean"))
        entry = add_import(self.repo, "garden", newer, ref="v2")
        self.assertTrue(any("pin conflict" in p.message for p in lockfile.verify(self.repo)))
        from ontokit import ledger

        dec = ledger.decide(self.repo, "Keep garden v2?", ["v2=Keep v2"], "v2", scope=["garden/"])
        override = {"decision": dec["id"], "kept": entry["export_sha256"], "dropped": [garden_sha]}
        add_import(self.repo, "garden", newer, ref="v2", override=override)
        self.assertEqual(lockfile.verify(self.repo), [])
        bad = dict(mid, bundled={"garden": {"sha256": "0" * 64, "export": garden}})
        add_import(self.repo, "mid", bad)
        self.assertTrue(any("does not match its object" in p.message for p in lockfile.verify(self.repo)))

    def test_newer_export_format_and_stray_file(self):
        export = make_export("garden", [mk_node("crop:mint")])
        export["meta"]["format"] = 2
        add_import(self.repo, "garden", export)
        self.assertTrue(any("upgrade the kit" in p.message for p in lockfile.verify(self.repo)))
        store.write_json(self.repo.path("imports/stray/export.json"), {"meta": {}})
        self.assertTrue(any(p.file == "imports/stray/export.json" for p in lockfile.verify(self.repo)))


if __name__ == "__main__":
    unittest.main()
