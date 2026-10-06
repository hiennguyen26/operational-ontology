"""The viewer follows the export's rules for local records: a decision about a shared node that names a local-only
person shows ``[local record]`` in place of the person's id, name and aliases (in its question and chosen text, and
in its id when the id was made from those words); a decision whose scope names only left-out records is not shown;
the needs of a shared node never name a left-out record; and a source title never names one either."""

from __future__ import annotations

import json
import os
import re
import unittest

from tests import test_build_release as viewer_tests
from tests import test_field_learnings as learnings
from tests.test_field_learnings import edge, node

from ontokit import build, graph, ledger, mutate, sources, store

LEFT_OUT = ("pat green", "pat-green", "person:pat-green", "patty", "role:seller.helper", "stall helper",
            "pat  green")


def inferred_edge(src, rel, dst):
    return {"op": "add_edge", "edge": {"src": src, "rel": rel, "dst": dst}, "basis": "inferred", "prov": []}


def clear():
    graph.clear_cache()
    store.clear_cache()


class LocalTopicCase(learnings.TopicCase):
    """A garden topic with a local-only person (Pat Green, alias Patty G) and a local-only role under a shared
    role's id (role:seller.helper), a draft link from the shared role to the person, and three decisions: one about
    the shared role that names the person, one scoped to the local role only, and one that names nobody."""

    def setUp(self):
        super().setUp()
        person = node("person:pat-green", "person", "Pat Green", "Pat Green helps at the stall")
        person["node"]["aliases"] = ["Patty G"]
        helper = node("role:seller.helper", "role", "Stall helper", "a stall helper sets up the tables",
                      "Sets up the tables on market days.")
        helper["node"]["visibility"] = "local"
        helper["ref"] = "$helper"
        self.answer("q.people.key", "Pat Green helps at the stall. The seller role sells the honey at the market. "
                    "A stall helper sets up the tables.", [
                        person,
                        node("role:seller", "role", "Seller", "sells the honey at the market",
                             "Sells the honey at the market."),
                        helper,
                        edge("$seller", "part_of", "topic:garden", "sells the honey at the market"),
                    ])
        self.answer("q.deepen.more", "The seller probably works with the stall volunteer.",
                    [inferred_edge("role:seller", "supports", "person:pat-green")])
        onto = self.onto()
        self.assertEqual(onto.node("person:pat-green").get("visibility"), "local")
        self.assertEqual(onto.node("role:seller.helper").get("visibility"), "local")
        self.assertEqual(onto.node("role:seller").get("visibility"), "shared")
        self.named = ledger.decide(
            self.repo, "Should Pat Green (person:pat-green) run the seller stall on weekends?",
            ["yes=Yes, Patty G runs it", "no=No, the seller does"], "yes", rationale="Pat  Green knows the buyers",
            scope=["role:seller", "person:pat-green"])["id"]
        self.local_only = ledger.decide(self.repo, "Does the stall helper get a key?", [], "yes, the helper keeps it",
                                        scope=["role:seller.helper"])["id"]
        self.plain = ledger.decide(self.repo, "Open the honey stall at eight?", [], "yes, at eight",
                                   scope=["role:seller"])["id"]
        clear()

    def built_html(self):
        code, out, err = self.cli("build", "--html")
        self.assertEqual(code, 0, out + err)
        with open(os.path.join(self.root, "build", "index.html"), encoding="utf-8") as fh:
            return fh.read()


class ViewerLeavesOutLocalTextTest(LocalTopicCase):
    def test_the_built_page_never_names_the_local_person(self):
        self.assertIn("pat-green", self.named)  # the id is made from the question, so it names the person too
        html = self.built_html()
        low = html.lower()
        for text in LEFT_OUT + (self.named, self.local_only):
            self.assertNotIn(text.lower(), low, text)
        with open(os.path.join(self.root, "build", "export.json"), encoding="utf-8") as fh:
            exported = fh.read().lower()
        for text in LEFT_OUT:
            self.assertNotIn(text, exported, text)

    def test_the_decision_is_shown_with_the_person_redacted(self):
        decisions = build.extract_data(self.built_html())["decisions"]
        listed = decisions["nodes"]["role:seller"]
        self.assertEqual(len(listed), 2, listed)
        self.assertIn(self.plain, listed)  # a decision that names nobody keeps its id
        stand_in = [d for d in listed if d != self.plain][0]
        self.assertRegex(stand_in, r"^dec-[0-9]{8}-local-record-[0-9a-f]{4,8}\Z")
        self.assertEqual(stand_in[-4:], self.named[-4:])  # the date and the hash stay, so onto decisions finds it
        self.assertEqual(stand_in[:13], self.named[:13])
        item = decisions["items"][stand_in]
        self.assertEqual(item["question"], "Should [local record] ([local record]) run the seller stall on weekends?")
        self.assertEqual(item["chosen"], "Yes, [local record] runs it")
        self.assertEqual(decisions["items"][self.plain]["question"], "Open the honey stall at eight?")
        self.assertNotIn(self.local_only, decisions["items"])
        self.assertEqual(sorted(decisions["items"]), sorted({d for ds in decisions["nodes"].values() for d in ds}))

    def test_a_decision_scoped_only_to_a_left_out_record_is_not_shown(self):
        # without the redactor the decision about the local role reaches the shared role by its id prefix
        clear()
        raw = build._node_decisions(self.repo, ["role:seller"])
        self.assertIn(self.local_only, raw["nodes"]["role:seller"])
        clear()
        red = build.Redactor(self.onto())
        kept = build._node_decisions(self.repo, ["role:seller"], red)
        self.assertNotIn(self.local_only, kept["items"])
        self.assertTrue(red.scope_left_out(["role:seller.helper"]))
        self.assertTrue(red.scope_left_out(["person:pat-green", "./Person:Pat-Green/notes"]))
        self.assertFalse(red.scope_left_out(["role:seller", "person:pat-green"]))
        self.assertFalse(red.scope_left_out(["role"]))  # a kind-wide scope names no record
        self.assertFalse(red.scope_left_out([]))

    def test_a_narrowing_link_never_names_a_left_out_decision(self):
        narrower = ledger.decide(self.repo, "Does the helper key open the shed too?", [], "no",
                                 scope=["role:seller.helper"], narrows=self.local_only)["id"]
        child = ledger.decide(self.repo, "Open at eight in winter too?", [], "no, at nine",
                              scope=["role:seller"], narrows=self.plain)["id"]
        under_named = ledger.decide(self.repo, "And on holidays?", [], "no", scope=["role:seller"],
                                    narrows=self.named)["id"]
        clear()
        payload = build.extract_data(self.built_html())
        items = payload["decisions"]["items"]
        self.assertNotIn(narrower, items)
        self.assertEqual(items[child]["narrows"], self.plain)
        self.assertEqual(items[self.plain]["narrowed_by"], [child])
        stand_in = [d for d in items if d.startswith(self.named[:13] + "local-record-")]
        self.assertEqual(len(stand_in), 1, sorted(items))
        self.assertEqual(items[under_named]["narrows"], stand_in[0])  # the link uses the same stand-in id
        self.assertEqual(items[stand_in[0]]["narrowed_by"], [under_named])

    def test_the_needs_of_a_shared_node_never_name_the_local_person(self):
        clear()
        onto = self.onto()
        raw = build._node_needs(onto, ["role:seller"])
        self.assertTrue(any("Pat Green" in g["text"] for g in raw.get("role:seller", [])), raw)  # the draft link
        needs_ = build.extract_data(self.built_html())["needs"]["role:seller"]
        texts = [g["text"] for g in needs_ if g["type"] == "draft_link"]
        self.assertTrue(texts and all("[local record]" in t for t in texts), needs_)
        self.assertNotIn("pat green", json.dumps(needs_).lower())

    def test_the_build_is_still_deterministic(self):
        code, out, err = self.cli("build", "--html", "--check")
        self.assertEqual(code, 0, out + err)
        self.assertIn("same bytes twice", out)


class RedactorTest(LocalTopicCase):
    def red(self):
        clear()
        return build.Redactor(self.onto())

    def test_names_ids_and_aliases_are_replaced_whole(self):
        red = self.red()
        self.assertEqual(red.text("PAT GREEN and pat\n green, Patty G, self/person:pat-green."),
                         "[local record] and [local record], [local record], [local record].")
        self.assertEqual(red.text("Patterson sells at person:pat-greenhouse"), "Patterson sells at "
                         "person:pat-greenhouse")
        self.assertEqual(red.text("the role:seller.helper sets up"), "the [local record] sets up")
        self.assertEqual(red.text("the Seller sells"), "the Seller sells")
        self.assertFalse(red.changed("Open at eight"))
        self.assertTrue(red.changed("ask Pat Green"))

    def test_a_name_a_shared_record_also_has_is_kept(self):
        node_ = node("person:seller", "person", "Seller", "a person called Seller")
        self.answer("q.people.key", "A person called Seller.", [node_])
        red = self.red()
        self.assertEqual(self.onto().node("person:seller").get("visibility"), "local")
        # the shared role is named Seller too, so the word is in the export already; the id is still replaced
        self.assertEqual(red.text("the Seller and person:seller"), "the Seller and [local record]")

    def test_source_titles_drop_names_as_well_as_ids(self):
        self.assertEqual(build._scrub_title("Call notes with Pat Green on person:pat-green", {"person:pat-green"},
                                            ["Pat Green"]),
                         "Call notes with a local record on a local record")
        self.assertEqual(build._scrub_title("Answer to q.gap.x on person:dave (ans-1)", {"person:dave"}),
                         "Answer to q.gap.x on a local record (ans-1)")
        red = self.red()
        self.assertIn("Pat Green", red.names)
        self.assertIn("Patty G", red.names)
        self.assertNotIn("Seller", red.names)

    def test_a_stand_in_id_keeps_the_date_and_hash(self):
        red = self.red()
        rec = {"id": self.named, "question": "Should Pat Green run it?", "scope": ["role:seller"]}
        self.assertEqual(red.decision_id(rec), self.named[:13] + "local-record-" + self.named.rsplit("-", 1)[1])
        plain = {"id": self.plain, "question": "Open the honey stall at eight?"}
        self.assertEqual(red.decision_id(plain), self.plain)
        erased = {"id": "dec-20261001-ask-sam-about-it-ab12", "question": "Ask [erased] about it?"}
        self.assertEqual(red.decision_id(erased), "dec-20261001-local-record-ab12")
        self.assertRegex(red.decision_id({"id": "dec-x", "question": "Ask Pat Green"}),
                         r"^dec-local-record-[0-9a-f]{8}\Z")


class RedactorFormsTest(LocalTopicCase):
    """Review round 7: the forms of a left-out id or name that free text may use."""

    def red(self):
        clear()
        return build.Redactor(self.onto())

    @staticmethod
    def stand_ins(items, dec_id):
        return [d for d in items if d == dec_id[:13] + "local-record-" + dec_id.rsplit("-", 1)[1]]

    def shown(self, question, chosen):
        dec_id = ledger.decide(self.repo, question, [], chosen, scope=["role:seller"])["id"]
        clear()
        html = self.built_html()
        items = build.extract_data(html)["decisions"]["items"]
        return dec_id, items, html

    def test_an_id_after_at_or_a_path_segment_is_replaced(self):
        dec_id, items, html = self.shown("Ping @person:pat-green for the keys?", "ok")
        stand_in = self.stand_ins(items, dec_id)
        self.assertEqual(len(stand_in), 1, sorted(items))
        self.assertEqual(items[stand_in[0]]["question"], "Ping @[local record] for the keys?")
        self.assertNotIn("person:pat-green", html.lower())
        red = self.red()
        self.assertEqual(red.text("See notes/person:pat-green"), "See notes/[local record]")
        self.assertEqual(red.text("x/self/person:pat-green"), "x/[local record]")
        self.assertEqual(red.text("(self/person:pat-green)"), "([local record])")
        self.assertEqual(red.text("mail pat@person:pat-green"), "mail pat@[local record]")

    def test_an_id_under_an_import_namespace_stays(self):
        red = self.red()
        red._imports = {"market"}  # an import holds its own record under the same local id
        self.assertEqual(red.text("see market/person:pat-green"), "see market/person:pat-green")
        self.assertEqual(red.text("market/person:pat-green, not Pat Green"), "market/person:pat-green, not "
                         "[local record]")
        self.assertEqual(red.text("see notes/person:pat-green"), "see notes/[local record]")

    def test_a_name_joined_by_a_hyphen_underscore_or_nothing_is_replaced(self):
        dec_id, items, html = self.shown("Ask pat-green about the stall?", "yes, Pat-Green agreed")
        self.assertNotIn(dec_id, items)  # its id was made from the words, so it gets a stand-in
        stand_in = self.stand_ins(items, dec_id)
        self.assertEqual(len(stand_in), 1, sorted(items))
        self.assertEqual(items[stand_in[0]]["question"], "Ask [local record] about the stall?")
        self.assertEqual(items[stand_in[0]]["chosen"], "yes, [local record] agreed")
        low = html.lower()
        for text in LEFT_OUT:
            self.assertNotIn(text, low, text)
        red = self.red()
        for form in ("Pat_Green", "PatGreen", "Pat​Green", "PAT - GREEN", "Patty_G", "seller.helper"):
            self.assertEqual(red.text("ask %s now" % form), "ask [local record] now", form)
        self.assertIn("pat-green", red.names)  # the bare slug of the hidden id
        self.assertEqual(red.text("Patterson and the greenhouse"), "Patterson and the greenhouse")

    def test_a_decision_whose_words_still_name_one_after_redaction_is_not_shown(self):
        dec_id, items, html = self.shown("Ask Pat.Green about the stall?", "yes")
        self.assertNotIn(dec_id, items)
        self.assertFalse(self.stand_ins(items, dec_id), sorted(items))
        self.assertNotIn("pat.green", html.lower())
        red = self.red()
        self.assertTrue(red.withheld({"id": dec_id, "question": "Ask Pat.Green about it?", "scope": ["role:seller"]}))
        self.assertFalse(red.withheld({"id": self.named, "question": "Should Pat Green run it?",
                                       "scope": ["role:seller"]}))  # redacted in full, so it is shown

    def test_a_one_word_name_is_matched_only_as_written(self):
        self.answer("q.people.key", "Rose waters the beds.", [node("person:rose", "person", "Rose",
                                                                    "Rose waters the beds")])
        self.assertEqual(self.onto().node("person:rose").get("visibility"), "local")
        plain_id, items, html = self.shown("Plant rose bushes by the seller stall?", "yes")
        self.assertEqual(items[plain_id]["question"], "Plant rose bushes by the seller stall?")
        red = self.red()
        self.assertIn("Rose", red.names)
        self.assertEqual(red.text("Ask Rose about the rose bushes"), "Ask [local record] about the rose bushes")
        self.assertFalse(red.decision_named({"id": plain_id, "question": "Plant rose bushes by the seller stall?"}))
        self.assertEqual(build._scrub_title("A guide to rose pruning for the seller", set(), ["Rose"]),
                         "A guide to rose pruning for the seller")
        self.assertEqual(build._scrub_title("Call with Rose", set(), ["Rose"]), "Call with a local record")


@unittest.skipIf(viewer_tests.NODE is None, "node is not installed; the viewer DOM checks need it")
class ViewerLocalDomTest(LocalTopicCase):
    page = viewer_tests.ViewerDomTest.page  # the DOM harness, without running that class's own tests again

    def test_the_panel_and_the_page_show_the_stand_in(self):
        html = self.built_html()
        stand_in = [d for d in build.extract_data(html)["decisions"]["items"] if "local-record" in d][0]
        _load, peeked = self.page(self.root, "#kind/role", [{"peek": "#node/role:seller"}], html=html)
        panel = " | ".join(peeked["peek"])
        self.assertIn("Should [local record] ([local record]) run the seller stall on weekends?Chosen: Yes, "
                      "[local record] runs it. " + stand_in, panel)
        self.assertIn("Open the honey stall at eight?", panel)
        full = " | ".join(self.page(self.root, "#node/role:seller", html=html)[0]["lines"])
        self.assertIn(stand_in, full)
        for text in (panel, full):
            low = text.lower()
            for word in LEFT_OUT:
                self.assertNotIn(word, low, word)
            self.assertIsNone(re.search(r"does the stall helper", low))


ROSE_NOTE = ("Call notes. Cut each rose back in March. Rose waters the north beds. Ask @person:rose first, or "
             "read notes/person:rose.txt.\n")
# the forms of the left-out id person:rose (and of the one-word name Rose) that no published file may hold
ROSE_FORMS = ("person:rose", "@rose", "person-rose", "person_rose", "personrose")


class PublishedFilesLeaveOutLocalIdsTest(learnings.TopicCase):
    """Review round 8: a local-only person whose name is a common word (Rose). Her id never reaches a published
    file (the export, the cards, the viewer, MANIFEST.json, VERSIONS.md) in any form: after "@", inside a path,
    under the topic's namespace, in another case, or joined to its kind. The ordinary word "rose" in a shared
    record's prose stays."""

    def setUp(self):
        super().setUp()
        entry, _dup = sources.add(self.repo, ROSE_NOTE, "note", "Call notes with @person:rose")
        self.sid = entry["id"]
        clear()

        def prov(quote):
            return [{"src": self.sid, "loc": "L1-L1", "quote": quote, "by": "user"}]

        def add(n, nid, kind, name, summary, quote, **extra):
            body = dict({"id": nid, "kind": kind, "name": name, "summary": summary}, **extra)
            return {"n": n, "op": "add_node", "node": body, "status": "confirmed", "trust": "user", "conf": 0.9,
                    "prov": prov(quote)}

        mutate.apply_ops(self.repo, [
            add(1, "person:rose", "person", "Rose", "Waters the north beds.", "Rose waters the north beds"),
            add(2, "term:pruning", "term", "Pruning", "Cut each rose back in March.", "Cut each rose back in March"),
            add(3, "term:north-beds", "term", "North beds",
                "Ask @Rose, see notes/person:rose.txt, garden/Person:Rose or the person-rose rota first.",
                "Ask @person:rose first", attrs={"abbreviation": "@person:rose"}),
        ], by="user", change_type="apply", summary="add the rota")
        clear()
        self.dec = ledger.decide(self.repo, "Ask @rose before pruning the north beds?", [], "yes, ask @Rose",
                                 scope=["term:north-beds"])["id"]
        clear()

    def published(self):
        code, out, err = self.cli("release", "--write", "--notes", "first beds, thanks @person:rose")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("build", "--html")
        self.assertEqual(code, 0, out + err)
        out = {}
        for rel in ("build/export.json", "build/cards.json", "build/index.html", "MANIFEST.json", "VERSIONS.md"):
            with open(os.path.join(self.root, *rel.split("/")), encoding="utf-8") as fh:
                out[rel] = fh.read()
        return out

    def test_the_id_never_reaches_a_published_file(self):
        self.assertEqual(self.onto().node("person:rose").get("visibility"), "local")
        files = self.published()
        for rel, text in files.items():
            low = text.lower()
            for form in ROSE_FORMS:
                self.assertFalse(form in low, "%s holds %s" % (rel, form))
        # the ordinary word in a shared record's prose stays, in the export and in the viewer
        for rel in ("build/export.json", "build/index.html"):
            self.assertIn("Cut each rose back in March.", files[rel], rel)

    def test_the_source_title_drops_an_id_after_at(self):
        exported = json.loads(self.published()["build/export.json"])
        titles = [s["title"] for s in exported["sources"] if s["id"] == self.sid]
        self.assertEqual(titles, ["Call notes with @a local record"])

    def test_the_shared_records_text_is_redacted_and_the_card_naming_her_is_dropped(self):
        files = self.published()
        nodes = {n["id"]: n for n in json.loads(files["build/export.json"])["nodes"]}
        self.assertNotIn("person:rose", nodes)
        beds = nodes["term:north-beds"]
        self.assertEqual(beds["summary"], "Ask @[local record], see notes/[local record].txt, [local record] or "
                         "the [local record] rota first.")  # the topic's own namespace goes, as self/ does
        self.assertEqual(beds["attrs"], {"abbreviation": "@[local record]"})
        self.assertEqual([p.get("quote") for p in beds["prov"]], ["Ask @[local record] first"])
        self.assertEqual(nodes["term:pruning"]["summary"], "Cut each rose back in March.")
        cards = {c["id"] for c in json.loads(files["build/cards.json"])["cards"]}
        self.assertIn("term:pruning", cards)
        self.assertNotIn("term:north-beds", cards)
        payload = build.extract_data(files["build/index.html"])
        items = payload["decisions"]["items"]
        self.assertNotIn(self.dec, items)  # its id was made from "@rose", so it gets a stand-in
        stand_in = [d for d in items if d == self.dec[:13] + "local-record-" + self.dec.rsplit("-", 1)[1]]
        self.assertEqual(len(stand_in), 1, sorted(items))
        self.assertEqual(items[stand_in[0]]["question"], "Ask @[local record] before pruning the north beds?")
        self.assertIn("first beds, thanks @[local record]", files["VERSIONS.md"])

    def test_an_edge_key_a_gap_field_and_joined_ids_are_redacted(self):
        # review round 9: an edge's key and a gap's field went out as written, and an id followed by "-", "_", "@"
        # or a digit was kept whole
        mutate.apply_ops(self.repo, [
            {"n": 1, "op": "add_edge", "edge": {"src": "term:pruning", "rel": "related_to", "dst": "term:north-beds",
                                                "key": "person:rose"},
             "status": "confirmed", "trust": "user", "conf": 0.9,
             "prov": [{"src": self.sid, "loc": "L1-L1", "quote": "Cut each rose back in March", "by": "user"}]},
            {"n": 2, "op": "add_gap", "id": "term:pruning", "gap": {"field": "ask @person:rose", "note": "plain"}},
            {"n": 3, "op": "update_node", "id": "term:pruning",
             "set": {"summary": "Cut each rose back in March. See notes/person:rose-2026.txt and person:rose_notes."},
             "prov": [{"src": self.sid, "loc": "L1-L1", "quote": "Cut each rose back in March", "by": "user"}]},
        ], by="user", change_type="apply", summary="key the link")
        clear()
        files = self.published()
        for rel, text in files.items():
            low = text.lower()
            for form in ROSE_FORMS:
                self.assertFalse(form in low, "%s holds %s" % (rel, form))
        exported = json.loads(files["build/export.json"])
        edges = [e for e in exported["edges"] if {e["src"], e["dst"]} == {"term:pruning", "term:north-beds"}]
        self.assertEqual([e["key"] for e in edges], ["[local record]"])
        pruning = {n["id"]: n for n in exported["nodes"]}["term:pruning"]
        self.assertEqual([(g["field"], g["note"]) for g in pruning["gaps"]], [("ask @[local record]", "plain")])
        self.assertEqual(pruning["summary"], "Cut each rose back in March. See notes/[local record]-2026.txt and "
                         "[local record]_notes.")
        # the card is made from the record as stored, so it names her and is dropped
        self.assertNotIn("term:pruning", {c["id"] for c in json.loads(files["build/cards.json"])["cards"]})

    def test_one_scrub_covers_every_form(self):
        clear()
        red = build.Redactor(self.onto())
        cases = {
            "ask @person:rose now": "ask @[local record] now",
            "ask @Person:ROSE now": "ask @[local record] now",
            "ask @rose now": "ask @[local record] now",
            "ask @ROSE now": "ask @[local record] now",
            "see notes/person:rose.txt": "see notes/[local record].txt",
            "see notes/person:rose/rota.md": "see notes/[local record]/rota.md",
            "see garden/person:rose": "see [local record]",
            "see self/Person:Rose": "see [local record]",
            "the person-rose, Person_Rose and personrose rotas": "the [local record], [local record] and "
                                                                  "[local record] rotas",
            "Rose waters the rose bushes": "[local record] waters the rose bushes",
            "the person rose early and pruned the rose": "the person rose early and pruned the rose",
            # a letter just after the id makes another word; other text joined to it (no record has that longer
            # id) leaves the id part replaced (review round 9: these kept the literal id)
            "see person:rosewood and person:rose-hip": "see person:rosewood and [local record]-hip",
            "notes/person:rose-2026.txt": "notes/[local record]-2026.txt",
            "person:rose_notes, person:rose2, person:rose@home and person:rose-notes":
                "[local record]_notes, [local record]2, [local record]@home and [local record]-notes",
            # glued after a character no kind holds, or after a kind no record has (review round 9)
            "re:person:rose, garden:person:rose, my_person:rose, file.person:rose and x-person:rose":
                "re:[local record], garden:[local record], my_[local record], file.[local record] and "
                "x-[local record]",
            "the person--rose and Person__Rose rotas": "the [local record] and [local record] rotas",
        }
        for given, wanted in cases.items():
            self.assertEqual(red.scrub(given), wanted, given)
            self.assertEqual(red.text(given), wanted, given)
        self.assertEqual(red.scrub("Call with @person:rose", build.TITLE_MARK), "Call with @a local record")
        self.assertEqual(build._scrub_title("Call with @person:rose and Rose", redactor=red),
                         "Call with @a local record and a local record")
        # a longer id that a kept record has names that record, before or after the left-out id
        red._known.update({"person:rose-hip", "person:rose.jr", "x-person:rose"})
        self.assertEqual(red.scrub("see person:rose-hip, person:rose.jr.txt, x-person:rose and person:rose-hips"),
                         "see person:rose-hip, person:rose.jr.txt, x-person:rose and [local record]-hips")
        # the left-out id itself is no reason to keep a longer text
        self.assertEqual(red.scrub("term:person:rose-x"), "term:[local record]-x")
        # the slug of another record's id stays, even when it joins to the left-out id
        red._known.add("term:person-rose")
        self.assertEqual(red.scrub("see term:person-rose and person-rose"), "see term:person-rose and [local record]")
        # a record's own name stays in its own text, the left-out one's does not
        self.assertEqual(red.scrub("Rose bed: Rose weeds it", keep=["Rose bed"]), "Rose bed: [local record] weeds it")


if __name__ == "__main__":
    unittest.main()
