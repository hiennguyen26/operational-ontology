"""sanitize: every secret kind has a redactor sample, hits after escapes, look-alikes kept, the personal policy (keep,
redact, refuse), JSON read as JSON, redact_file, and the one-mode rule: anything validate accepts, export accepts."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from tests import _support
from ontokit import graph, secrets, sanitize, sources, store, validate
from ontokit.errors import Refused, UsageError

KEEP_ALL = {"personal": {kind: "keep" for kind in sanitize.PERSONAL_KINDS}}
REDACT_ALL = {"personal": {kind: "redact" for kind in sanitize.PERSONAL_KINDS}}
REFUSE_ALL = {"personal": {kind: "refuse" for kind in sanitize.PERSONAL_KINDS}}

# one sample per personal kind (synthetic: a reserved test domain, a fictional 555-01xx number, the networks'
# published test card number and an SSN from the advertising-sample range)
PERSONAL = {
    "email": "steward@garden.test",
    "phone": "(212) 555-0142",
    "address": "12 Elm Street",
    "name": "/Users/plotkeeper/notes.md",
    "payment_card": "4111 1111 1111 1111",
    "government_id": "123-45-6789",
}

LOOK_ALIKES = [
    "task-list-abcdefghijklmnopqrstuvwxyz0123 is the name of a board",  # "sk-" inside a word
    "the logo is image@2x.png and the icon is icon@1.5x.svg",
    "write to noreply@example.com for the newsletter",
    "docker run -u 1000:1000 garden-app",
    "SORT_KEY=created_at2",
    '{"max_tokens": 4096, "input_tokens": 12}',
    '{"private_key": 1, "credential": 2}',
    "API_KEY=$API_KEY and SECRET_TOKEN=${SECRET_TOKEN}",
    "password: <your password here>",
    "Authorization: the process of approving a new plot holder",
    "Order 4 bags of mulch on 2026-09-28 at 09:30",
    "Bed 12 is next to the shed; plant 12 tomato seedlings per row",
    "Beds 3 and 4 get water every 2 days",
    "The bearer of this card may enter the garden",
    "order number 2077245139 is an id, not a phone",
    "logs sit in /home/runner/work and /Users/shared/tmp",
    "version 1.2.3 of the rota and host 10.0.0.1",
    "the seed token is a wooden disc",
    # prose that once refused as a credential, or lost words to an address
    "Cookie: butter=200g, sugar=150g, bake 12 minutes",
    "Basic Kitchen101 course for new cooks",
    "See https://garden.test/wiki?key=north-bed for the plan",
    "Authorization: 123 forms signed by the coordinator",
    "Plant 4 Tomato Row by the fence",
    "DB_PASSWORD=process.env.DB_PASSWORD",
    # numbers that are not cards or government ids, and prose about passwords that holds none
    "ms 1727600000000 is a timestamp and 4111111111111112 fails the check digit",
    "ISBN 978-0-306-40615-7 and part 123-45-6789-01 sit on the shelf",
    "the dates 2026 0929 1234 5678 and 4111 1111 1111 1111 1111 1111 are lists, not cards",
    "SSN 000-12-3456 is never issued, and passport photos are 2x2",
    "the password is required and the password is case-sensitive",
    "the PIN is 4-digit and the password was 2FA-protected",
    "Pin 13 is ground; the pin is 5V tolerant",
    "Password: see the binder by the shed door",
    "sshpass -f pw.txt ssh -p 2222 shed.test",
    "hf_hub_download fetches a model file",
    # numbers that are not phones: dates, zero-padded lists, ISBNs, counts, and phone words before other numbers
    "meet on 01.02.2026 at 10.30, or call me on 12.10.2026 and text me on 12 10 2026",
    "plots 010 020 030 040 and 0100 0200 0300, rows 01 02 03 04 05",
    "ISBN 0-306-40615-2, tag 04213, and bed 0412 has 345 678 seeds",
    "phone 3 times a day, call 911, and dial 2 for the shed",
    "+2 cups of flour, a +1 2026 budget, and score +3 in round 12",
    "Tel Aviv 2026 and mobile app version 2.3.4",
    "Contact: 0412 345 678 901 234 are plot tags",
    "HOTEL_ID=0123456789, telescope=12345678, cellar: 12345678, phone_count=12, order_number=2077245139",
    '{"hotel": 12345678, "cell_size": 0.5, "phone": "ask at the desk", "fax": 12}',
]

# a redaction opens a token boundary for a rule that already ran (the second item needs a second pass)
ADJACENT = [
    ("Deliveries: 12 Elm Street.(212) 555-0142\n", {"address": 1, "phone": 1}),
    ("Seeds to PO Box 4471+44 20 7946 0958\n", {"address": 1, "phone": 1}),
    ("steward@garden.test_steward@garden.test\n", {"email": 2}),
    ("<joined>12 Elm Street.(212) 555-0142</joined>\n", {"address": 1, "phone": 1}),
    ("Seeds to PO Box 4471 07700 900123\n", {"address": 1, "phone": 1}),  # a number after a number, then not
]

# phone numbers outside North America, one per line: (text, the number). Synthetic: the UK drama ranges (07700
# 900xxx, 020 7946 0xxx), the Australian fiction range (0491 570 xxx) and made-up numbers elsewhere.
PHONES = [
    ("UK mobile +44 7700 900123", "+44 7700 900123"),
    ("UK mobile +447700 900123", "+447700 900123"),
    ("UK mobile +44 7700900123", "+44 7700900123"),
    ("UK line +44 (0)20 7946 0958", "+44 (0)20 7946 0958"),
    ("UK mobile 07700 900123", "07700 900123"),
    ("UK line 020 7946 0958", "020 7946 0958"),
    ("UK line (020) 7946 0958", "(020) 7946 0958"),
    ("DE line +49 30 1234567", "+49 30 1234567"),
    ("DE mobile +49 151 23456789", "+49 151 23456789"),
    ("DE line +49 (30) 1234567", "+49 (30) 1234567"),
    ("DE mobile 0151 23456789", "0151 23456789"),
    ("DE line 030/1234567", "030/1234567"),
    ("FR mobile 06 12 34 56 78", "06 12 34 56 78"),
    ("FR mobile 06.12.34.56.78", "06.12.34.56.78"),
    ("FR mobile +33 6 12 34 56 78", "+33 6 12 34 56 78"),
    ("IN mobile +91 98765 43210", "+91 98765 43210"),
    ("AU mobile 0491 570 156", "0491 570 156"),
    ("AU mobile +61 491 570 156", "+61 491 570 156"),
    ("AU line (02) 9876 5432", "(02) 9876 5432"),
    ("JP mobile 090-1234-5678", "090-1234-5678"),
    ("CH line 044 668 18 00", "044 668 18 00"),
]
# a phone word before a number the other rules leave: contiguous, no trunk 0, a 00 prefix
CUED_PHONES = [
    ("my number is 07700900123", "my number is [redacted:phone]"),
    ("Mobile: 98765 43210", "Mobile: [redacted:phone]"),
    ("Tel. 0044 20 7946 0958", "Tel. [redacted:phone]"),
    ("phone number: 0491570156", "phone number: [redacted:phone]"),
    ("call me on 612 34 56 78", "call me on [redacted:phone]"),
    ("reach Lee on 0412345678", "reach Lee on [redacted:phone]"),
    ("ring 2125550142 after nine", "ring [redacted:phone] after nine"),
    ("WhatsApp 9876543210", "WhatsApp [redacted:phone]"),
    ("Phone:\n07700900123", "Phone:\n[redacted:phone]"),
    ("text me at 555 0142", "text me at [redacted:phone]"),
    # a field name is a cue too: a table row, a settings line, JSON written as text
    ("row 1: name=Lee | phone_number=07700900123", "row 1: name=Lee | phone_number=[redacted:phone]"),
    ("contactPhone: 9876543210", "contactPhone: [redacted:phone]"),
    ('  "WORK_MOBILE": "98765 43210",', '  "WORK_MOBILE": "[redacted:phone]",'),
]


def _secret_line(kind: str) -> str:
    return "the shed camera uploads with %s every night\n" % _support.fake_secret(kind)


class SecretKindsTest(unittest.TestCase):
    def test_every_secret_kind_has_a_redactor_sample(self):
        # a pattern added to secrets fails here until _support.fake_secret has a sample for it
        self.assertTrue(set(secrets.KINDS) <= sanitize.RULE_KINDS)
        for kind in secrets.KINDS:
            sample = _support.fake_secret(kind).strip()
            text = _secret_line(kind)
            with self.subTest(kind=kind):
                redactor = sanitize.Redactor()
                out = redactor.text(text)
                self.assertGreater(redactor.counts[kind], 0, out)
                self.assertIn("[redacted:%s]" % kind, out)
                self.assertNotIn(sample, out)
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text, None)
                self.assertIn(kind, ctx.exception.extra["kinds"])
                self.assertIn(kind, ctx.exception.message)
                self.assertNotIn(sample[6:], ctx.exception.message)  # never echoes the secret
                self.assertIn(kind, sanitize.check_text(text, None))

    def test_token_formats_the_release_scan_does_not_list_yet(self):
        samples = {
            "huggingface": "hf" + "_" + _support._alnum(34),
            "digitalocean": "dop" + "_v1_" + ("0123456789abcdef" * 4),
        }
        self.assertEqual(sorted(kind for kind, _src, _hints in sanitize.EXTRA_TOKENS), sorted(samples))
        for kind, sample in sorted(samples.items()):
            text = "the shed camera uploads with %s every night\n" % sample
            with self.subTest(kind=kind):
                self.assertIn(kind, sanitize.RULE_KINDS)
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text, None)
                self.assertEqual(ctx.exception.extra["kinds"], [kind])
                self.assertNotIn(sample[6:], ctx.exception.message)
                self.assertEqual(sanitize.check_text(text, None), [kind])
                # after an escape, as in JSON inside a JSON string
                with self.assertRaises(Refused):
                    sanitize.sanitize_text('{"log": "line one\\n%s"}\n' % sample, None)
        # each token kind has one rule, also once the release scan lists it
        kinds = [rule.kind for rule in sanitize.SECRET_RULES]
        self.assertEqual(sorted(set(kinds)), sorted(kinds))

    def test_hits_after_escape_sequences(self):
        token = _support.fake_secret("github")
        for text in ('{"log": "line one\\n%s"}' % token, "q=1%%3D%s" % token, "a\\t%s" % token,
                     '"\\u0022%s"' % token):
            with self.subTest(text=text[:20]):
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text, None)
                self.assertEqual(ctx.exception.extra["kinds"], ["github"])
        # JSON inside a JSON string, and a password under a quoted name
        with self.assertRaises(Refused) as ctx:
            sanitize.sanitize_text('{"body": "{\\"password\\": \\"gardenshed42\\"}"}\n', None)
        self.assertEqual(ctx.exception.extra["kinds"], ["credential"])
        # an address right after an escape keeps the escape, and the JSON line stays valid
        clean, red = sanitize.sanitize_text('{"note": "call\\nsteward@garden.test"}\n', None)
        self.assertEqual(json.loads(clean), {"note": "call\n[redacted:email]"})
        self.assertEqual(red, {"email": 1})
        # a URL-encoded phone number goes whole
        clean, red = sanitize.sanitize_text("tel=%2B1%20212%20555%200142 for the shed\n", None)
        self.assertEqual((clean, red), ("tel=[redacted:phone] for the shed\n", {"phone": 1}))

    def test_look_alikes_are_kept(self):
        for text in LOOK_ALIKES:
            with self.subTest(text=text):
                self.assertEqual(sanitize.sanitize_text(text, REDACT_ALL), (text, {}))
                self.assertEqual(sanitize.check_text(text, REDACT_ALL), [])

    def test_credential_rules(self):
        cases = {
            "password: gardenshed42": "credential",
            'DB_PASSWORD="hunter22"': "credential",
            "JWT_KEY=super_secret1": "credential",
            "Authorization: Bearer abcdefghijkl0123456789": "auth_header",
            "curl -u keeper:shed4tools https://garden.test/api": "credential",
            "https://keeper:shed4tools@garden.test/rota": "url_credentials",
            "https://garden.test/rota?access_token=abc123def456": "url_token",
            "Cookie: session=abc123def456": "cookie",
            "machine garden.test login keeper password shed4tools": "credential",
            "mysql -u root -pShed4tools rota": "credential",
            "DB_PASSWORD=letmein": "credential",
            "export SHED_PWD=trowels": "credential",
            "JWT_SECRET=gardenshed": "credential",
            "Cookie: session=abc123def456; theme=dark": "cookie",
            "Basic a2VlcGVyOnNoZWQ0dG9vbHM=": "basic",
            "Authorization: 12345678": "auth_header",
            "https://garden.test/wiki?key=north-bed-plan-2026": "url_token",
            # secrets said in prose, sshpass, and passcode fields
            "The shed wifi password is Garden2026!Shed": "credential",
            "the password for the shed is Garden2026!": "credential",
            "The password was changed to Hunter2024 last week": "credential",
            'the passphrase is "Tulip-Bed-42"': "credential",
            "the gate code is 4821.": "credential",
            "PIN: 4821": "credential",
            "sshpass -p Garden2026Shed ssh keeper@shed.test": "credential",
            "sshpass -pGarden2026Shed ssh shed.test": "credential",
            "SSHPASS=Garden2026Shed sshpass -e ssh shed.test": "credential",
            '{"passcode": "Tulip42"}': "credential",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text + "\n", None)
                self.assertIn(kind, ctx.exception.extra["kinds"])

    def test_private_key_blocks(self):
        body = "\n".join((_support._alnum(64)) for _ in range(3))
        block = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + body + "\n-----END " + "RSA PRIVATE KEY-----\n"
        with self.assertRaises(Refused) as ctx:
            sanitize.sanitize_text("key below\n" + block + "after\n", None)
        self.assertEqual(ctx.exception.extra["kinds"], ["private_key"])
        with self.assertRaises(Refused) as ctx:
            sanitize.sanitize_text("pasted without its header:\n" + body + "\nAb3d\n", None)
        self.assertEqual(ctx.exception.extra["kinds"], ["key_block"])
        # a column of hex digests is not a key block
        digests = "\n".join(("0123456789abcdef" * 4) for _ in range(3)) + "\n"
        self.assertEqual(sanitize.sanitize_text(digests, None), (digests, {}))

    def test_one_mode_only(self):
        with self.assertRaises(UsageError):
            sanitize.Redactor(record=False)
        self.assertTrue(sanitize.Redactor().record)


class PolicyTest(unittest.TestCase):
    def test_keep_redact_refuse_per_kind(self):
        for kind, sample in sorted(PERSONAL.items()):
            text = "Contact: %s today\n" % sample
            with self.subTest(kind=kind, action="keep"):
                self.assertEqual(sanitize.sanitize_text(text, {"personal": {kind: "keep"}}), (text, {}))
                self.assertEqual(sanitize.check_text(text, {"personal": {kind: "keep"}}), [])
            with self.subTest(kind=kind, action="redact"):
                clean, red = sanitize.sanitize_text(text, {"personal": {kind: "redact"}})
                self.assertEqual(red, {kind: 1})
                self.assertIn("[redacted:%s]" % kind, clean)
                self.assertNotIn(sample.split("/")[2] if kind == "name" else sample, clean)
                self.assertEqual(sanitize.check_text(text, {"personal": {kind: "redact"}}), [kind])
            with self.subTest(kind=kind, action="refuse"):
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text, {"personal": {kind: "refuse"}})
                self.assertEqual(ctx.exception.extra["kinds"], [kind])
                self.assertIn("personal data the policy refuses", ctx.exception.message)

    def test_default_policy(self):
        text = "Mail %s or call %s; deliveries to %s; files in %s\n" % (
            PERSONAL["email"], PERSONAL["phone"], PERSONAL["address"], PERSONAL["name"])
        clean, red = sanitize.sanitize_text(text, None)
        self.assertEqual(red, {"address": 1, "email": 1, "phone": 1})
        self.assertIn("/Users/plotkeeper/", clean)  # names are kept by default
        # kinds that store.DEFAULT_POLICY does not name are redacted
        expected = dict(store.DEFAULT_POLICY["personal"])
        for kind in ("payment_card", "government_id"):
            expected.setdefault(kind, "redact")
        self.assertEqual(sanitize.personal_policy(None), expected)
        self.assertEqual(set(sanitize.personal_policy(None)), set(sanitize.PERSONAL_KINDS))
        # an unknown action refuses (the careful reading), and a partial policy keeps the other defaults
        with self.assertRaises(Refused):
            sanitize.sanitize_text(text, {"personal": {"email": "maybe"}})
        clean, red = sanitize.sanitize_text(text, {"personal": {"email": "keep"}})
        self.assertEqual(red, {"address": 1, "phone": 1})

    def test_payment_cards_and_government_ids(self):
        cases = [
            ("Treasurer card 4111 1111 1111 1111 exp 12/29", {"payment_card": 1}),
            ("card 4111 1111 1111 1111 12/29 cvv 123", {"payment_card": 1}),
            ("exp 12/29 4111111111111111", {"payment_card": 1}),
            ("SSN 123456789 born 1990", {"government_id": 1}),
            ("card 4111111111111111 on file", {"payment_card": 1}),
            ("Mastercard 5555-5555-5555-4444 for seeds", {"payment_card": 1}),
            ("Amex 3782 822463 10005 for tools", {"payment_card": 1}),
            ("paid with 6011111111111117.", {"payment_card": 1}),
            ("SSN 123-45-6789 on the form", {"government_id": 1}),
            ("ITIN 912-70-1234 on the form", {"government_id": 1}),
            ("SSN: 123456789 on the form", {"government_id": 1}),
            ("social security no. 123 45 6789", {"government_id": 1}),
            ("passport number is X12345678", {"government_id": 1}),
            ("driver's license # D1234567", {"government_id": 1}),
            ("NI number: AB 12 34 56 C", {"government_id": 1}),
            ("\\n4111111111111111 after an escape", {"payment_card": 1}),
            ("pay=4111%201111%201111%201111 encoded", {"payment_card": 1}),
        ]
        for text, counts in cases:
            with self.subTest(text=text):
                clean, red = sanitize.sanitize_text(text + "\n", None)
                self.assertEqual(red, counts, clean)
                self.assertNotIn("1111", clean)
                self.assertNotIn("6789", clean)
                self.assertEqual(sanitize.check_text(clean, None), [])
        # a topic whose policy was written before these kinds existed still redacts them; its own choice wins
        old = {"personal": {"email": "redact", "phone": "redact", "name": "keep", "address": "redact"}}
        text = "card %s and SSN %s\n" % (PERSONAL["payment_card"], PERSONAL["government_id"])
        self.assertEqual(sanitize.sanitize_text(text, old)[1], {"government_id": 1, "payment_card": 1})
        with self.assertRaises(Refused) as ctx:
            sanitize.sanitize_text(text, {"personal": {"payment_card": "refuse"}})
        self.assertEqual(ctx.exception.extra["kinds"], ["payment_card"])
        self.assertNotIn("1111", ctx.exception.message)
        self.assertEqual(sanitize.sanitize_text(text, KEEP_ALL), (text, {}))

    def test_street_addresses_and_boxes(self):
        for text in ("Deliveries go to 221B Garden Lane on Fridays", "send seeds to PO Box 4471",
                     "meet at 7 N. Main St. by the gate"):
            with self.subTest(text=text):
                clean, red = sanitize.sanitize_text(text, None)
                self.assertEqual(red, {"address": 1}, clean)


class PhoneTest(unittest.TestCase):
    def test_international_and_national_formats(self):
        for line, number in PHONES:
            text = "Bed rota: %s, weekdays\n" % line
            with self.subTest(text=line):
                clean, red = sanitize.sanitize_text(text, None)
                self.assertEqual(red, {"phone": 1}, clean)
                self.assertEqual(clean, text.replace(number, "[redacted:phone]"))
                self.assertEqual(sanitize.check_text(text, None), ["phone"])
                self.assertEqual(sanitize.check_text(clean, None), [])
                self.assertEqual(sanitize.sanitize_text(text, KEEP_ALL), (text, {}))

    def test_a_phone_word_before_any_number(self):
        for text, expected in CUED_PHONES:
            with self.subTest(text=text):
                self.assertEqual(sanitize.sanitize_text(text + "\n", None), (expected + "\n", {"phone": 1}))
        # a US SSN a policy keeps is not a phone number after a phone word
        keep_ids = {"personal": {"government_id": "keep"}}
        self.assertEqual(sanitize.sanitize_text("call me at 123-45-6789\n", keep_ids),
                         ("call me at 123-45-6789\n", {}))

    def test_every_number_in_a_mixed_text_is_counted(self):
        # a US and a UK number: both go, and the count says 2 (a count of 1 read as handled while one leaked)
        clean, red = sanitize.sanitize_text("reach Lee on +44 7700 900123 or (212) 555-0199.\n", None)
        self.assertEqual((clean, red), ("reach Lee on [redacted:phone] or [redacted:phone].\n", {"phone": 2}))
        transcript = ("00:04:15 Sam: my number is 07700 900123\n"
                      "00:04:20 Kim: mine is +49 151 23456789 and office +91 98765 43210\n")
        clean, red = sanitize.sanitize_text(transcript, None)
        self.assertEqual(red, {"phone": 3}, clean)
        self.assertEqual(clean, "00:04:15 Sam: my number is [redacted:phone]\n"
                                "00:04:20 Kim: mine is [redacted:phone] and office [redacted:phone]\n")
        # the same numbers inside JSON and URL-encoded
        clean, red = sanitize.sanitize_text('{"note": "call\\n07700 900123"}\n', None)
        self.assertEqual((json.loads(clean), red), ({"note": "call\n[redacted:phone]"}, {"phone": 1}))
        clean, red = sanitize.sanitize_text("tel=%2B44%207700%20900123 for the shed\n", None)
        self.assertEqual((clean, red), ("tel=[redacted:phone] for the shed\n", {"phone": 1}))

    def test_a_phone_field_in_json(self):
        # a contact list: the field name is the cue for a value no other rule knows (contiguous, a JSON number)
        doc = ('[\n {\n  "name": "Lee",\n  "phone": "07700900123",\n  "mobile": 2125550142,\n'
               '  "contactPhone": "+44 7700 900123",\n  "beds": 12345678\n }\n]\n')
        clean, red = sanitize.sanitize_text(doc, None)
        self.assertEqual(red, {"phone": 3}, clean)
        self.assertEqual(json.loads(clean), [{"name": "Lee", "phone": "[redacted:phone]",
                                              "mobile": "[redacted:phone]", "contactPhone": "[redacted:phone]",
                                              "beds": 12345678}])
        self.assertEqual(sanitize.check_text(clean, None), [])
        line = '{"who": "Lee", "cell": "0491570156"}\n'
        self.assertEqual(sanitize.check_text(line, None), ["phone"])
        self.assertEqual(sanitize.sanitize_text(line, KEEP_ALL), (line, {}))
        with self.assertRaises(Refused) as ctx:
            sanitize.sanitize_text(line, REFUSE_ALL)
        self.assertEqual(ctx.exception.extra["kinds"], ["phone"])
        self.assertNotIn("0491570156", ctx.exception.message)


class JsonTest(unittest.TestCase):
    def test_key_names_decide(self):
        for text in ('{"apiKey": "abcdefghijkl"}\n', '{"db_password": 84736251}\n',
                     '{\n "settings": {\n  "refresh_token": "plainwords"\n }\n}\n'):
            with self.subTest(text=text):
                with self.assertRaises(Refused) as ctx:
                    sanitize.sanitize_text(text, None)
                self.assertEqual(ctx.exception.extra["kinds"], ["credential"])

    def test_exact_bytes_kept_unless_redacted(self):
        line = '{"a":1,   "b" : "north bed"}\n'
        self.assertEqual(sanitize.sanitize_text(line, None), (line, {}))
        doc = '{\n "beds": [1, 2],\n "beds": "a repeated key is kept",\n "size": 4.50\n}\n'
        self.assertEqual(sanitize.sanitize_text(doc, None), (doc, {}))
        clean, red = sanitize.sanitize_text('{"who": "steward@garden.test", "who": "coordinator@garden.test"}\n',
                                            None)
        self.assertEqual(red, {"email": 2})
        self.assertEqual(json.loads(clean), {"who": "[redacted:email]", "who (dup)": "[redacted:email]"})

    def test_every_copy_of_a_repeated_key_is_read(self):
        with self.assertRaises(Refused):
            sanitize.sanitize_text('{"token": "abc123xyz789", "token": "placeholder"}\n', None)

    def test_a_rewritten_line_or_document_keeps_number_spellings(self):
        line = '{"who": "steward@garden.test", "price": 2.50, "big": 1e400, "area": 12345678901234567.25, "n": -0}\n'
        clean, red = sanitize.sanitize_text(line, None)
        self.assertEqual(red, {"email": 1})
        self.assertEqual(clean, '{"who": "[redacted:email]", "price": 2.50, "big": 1e400, '
                                '"area": 12345678901234567.25, "n": -0}\n')
        doc = '{\n "ratio": 1.10,\n "who": ["steward@garden.test", NaN],\n "empty": {},\n "none": null\n}\n'
        clean, red = sanitize.sanitize_text(doc, None)
        self.assertEqual(clean, '{\n "ratio": 1.10,\n "who": [\n  "[redacted:email]",\n  NaN\n ],\n "empty": {},\n'
                                ' "none": null\n}\n')
        self.assertEqual(sanitize.check_text(clean, None), [])
        # a number under a password name is still a credential
        with self.assertRaises(Refused):
            sanitize.sanitize_text('{"db_password": 84736251.0}\n', None)

    def test_a_lone_surrogate_stays_escaped(self):
        clean, red = sanitize.sanitize_text('{"who": "steward@garden.test", "note": "half \\ud83d"}\n', None)
        self.assertEqual(red, {"email": 1})
        self.assertIn("\\ud83d", clean)
        clean.encode("utf-8")

    def test_json_nested_too_deeply_is_read_as_text(self):
        for text in ("[" * 500 + "]" * 500 + "\n", "[" * 5000 + "\n", '{"a":' * 3000 + "1" + "}" * 3000 + "\n"):
            with self.subTest(text=text[:8] + str(len(text))):
                self.assertEqual(sanitize.sanitize_text(text, None), (text, {}))
                self.assertEqual(sanitize.check_text(text, None), [])
        deep = "[" * 1500 + '"steward@garden.test"' + "]" * 1500 + "\n"
        clean, red = sanitize.sanitize_text(deep, None)
        self.assertEqual(red, {"email": 1})
        self.assertEqual(sanitize.check_text(clean, None), [])


class FixpointTest(unittest.TestCase):
    def test_adjacent_items_are_all_redacted(self):
        for text, counts in ADJACENT:
            with self.subTest(text=text):
                clean, red = sanitize.sanitize_text(text, None)
                self.assertEqual(red, counts, clean)
                self.assertEqual(sanitize.check_text(clean, None), [])
                self.assertNotIn("555", clean)
                self.assertNotIn("steward@", clean)

    def test_text_that_does_not_settle_is_refused(self):
        with mock.patch.object(sanitize, "MAX_PASSES", 1):
            with self.assertRaises(Refused) as ctx:
                sanitize.sanitize_text(ADJACENT[0][0], None)
        self.assertEqual(ctx.exception.extra["kinds"], ["address"])  # the kinds found in the passes that ran
        self.assertNotIn("555", ctx.exception.message)


class LinearTimeTest(unittest.TestCase):
    """Long runs of the characters a token is made of are read once, not once per character."""

    def test_long_runs_take_linear_time(self):
        for text in ("-" * 50000 + "\nNote: x\n", "a." * 25000 + "=x", "-".join(["bed"] * 10000) + " Note: y",
                     "%41" * 50000 + "@x", "mysql " * 10000 + "\n", "1 " * 50000, "4111-" * 20000, "123-45-" * 20000,
                     "password is " * 20000, "password for a b c " * 10000, "sshpass -f x " * 10000,
                     "passport " * 20000, "hf_" * 30000, "the pin is 5V " * 10000, "01 " * 40000,
                     "(01) " * 20000, "+1 (0)" * 20000, "0412 " * 20000, "phone: " * 20000, "call me on " * 10000,
                     "reach Lee Ann Bo on " * 8000, "phone:" + " " * 50000 + "x", "phone_" * 20000 + "=x",
                     "a_" * 30000 + "=x", "x.phone:" * 15000, "aB" * 40000):
            with self.subTest(text=text[:12]):
                start = time.time()
                self.assertEqual(sanitize.sanitize_text(text, None), (text, {}))
                self.assertEqual(sanitize.check_text(text, None), [])
                self.assertLess(time.time() - start, 1.0)


class ParityTest(unittest.TestCase):
    """One mode: what check_text (validate, export) accepts, sanitize_text (ingest) stores unchanged, and what
    ingest stores, validate accepts."""

    def corpus(self):
        out = list(LOOK_ALIKES)
        out += ["Contact: %s\n" % s for s in PERSONAL.values()]
        out += [_secret_line("github"), "password: gardenshed42\n", '{"who": "steward@garden.test"}\n',
                "The shed wifi password is Garden2026!Shed\n", "card 4111-1111-1111-1111 and SSN: 123456789\n",
                "pay=4111%201111%201111%201111\n",
                "line\n{\"ok\": true}\n" + "Mail %s\n" % PERSONAL["email"], "tel=%2B1%20212%20555%200142\n",
                '{\n "who": "steward@garden.test",\n "n": 1\n}\n']
        out += [text for text, _counts in ADJACENT]
        out += ['{"who": "steward@garden.test", "price": 2.50, "big": 1e400}\n', "[" * 600 + "]" * 600 + "\n"]
        out += ["Numbers: %s\n" % line for line, _number in PHONES] + [text + "\n" for text, _clean in CUED_PHONES]
        return out

    def test_validate_accepts_what_ingest_stores_and_export_accepts_what_validate_accepts(self):
        for policy in (None, KEEP_ALL, REDACT_ALL, REFUSE_ALL):
            for text in self.corpus():
                with self.subTest(policy=policy, text=text[:30]):
                    kinds = sanitize.check_text(text, policy)
                    try:
                        clean, red = sanitize.sanitize_text(text, policy)
                        refused = False
                    except Refused as exc:
                        refused = True
                        self.assertTrue(set(exc.extra["kinds"]) <= set(kinds))
                    if not kinds:
                        self.assertFalse(refused)
                        self.assertEqual((clean, red), (text, {}))
                    else:
                        self.assertTrue(refused or clean != text)
                    if not refused:
                        self.assertEqual(sanitize.check_text(clean, policy), [])
                        self.assertEqual(sanitize.sanitize_text(clean, policy), (clean, {}))


class ValidateTest(_support.TempCase):
    def test_p18_uses_the_same_rules(self):
        root = _support.init_topic(self.tmp, "garden", "Community garden")
        repo = store.Repo.open(root)
        entry, _dup = sources.add(repo, "Mail %s for a key\n" % PERSONAL["email"], "note", "Stored by ingest")
        self.assertEqual(entry["redactions"], {"email": 1})
        graph.clear_cache()
        self.assertEqual([p.text() for p in validate.validate(repo).problems], [])
        raw, _dup = sources.add(repo, "Call %s about the shed\n" % PERSONAL["phone"], "note", "Written raw",
                                sanitizer=lambda text, policy: (text, {}))
        graph.clear_cache()
        codes = [(p.code, p.file) for p in validate.validate(repo).problems]
        self.assertEqual(codes, [("P18", "sources/%s.txt" % raw["id"])])
        manifest = dict(repo.manifest)
        manifest["policy"] = dict(manifest["policy"], personal=dict(manifest["policy"]["personal"], phone="keep"))
        store.write_json(repo.path("ontology.json"), manifest)
        graph.clear_cache()
        self.assertEqual(validate.validate(repo.reload()).problems, [])


class RedactFileTest(_support.TempCase):
    def test_redact_file_writes_through_a_rescanned_temp(self):
        src = os.path.join(self.tmp, "in.txt")
        dst = os.path.join(self.tmp, "out", "clean.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("Mail %s about bed 4\n" % PERSONAL["email"])
        report = sanitize.redact_file(src, dst, None)
        self.assertEqual(report["redactions"], {"email": 1})
        with open(dst, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Mail [redacted:email] about bed 4\n")
        self.assertEqual(sorted(os.listdir(os.path.dirname(dst))), ["clean.txt"])

    def test_redact_file_refuses_credentials_and_its_own_source(self):
        src = os.path.join(self.tmp, "in.txt")
        dst = os.path.join(self.tmp, "clean.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(_secret_line("aws"))
        with self.assertRaises(Refused):
            sanitize.redact_file(src, dst, None)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["in.txt"])
        with self.assertRaises(UsageError):
            sanitize.redact_file(src, src, None)


if __name__ == "__main__":
    unittest.main()
