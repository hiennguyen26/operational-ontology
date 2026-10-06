#!/usr/bin/env bash
# The end-to-end demo: four synthetic topics (garden, kitchen, garden-to-table, market), each a temp git repo, built
# only through the onto command line. People appear as roles; the notes hold a fake address and a fictional phone
# number so the redactor has something to count.
#
#   bash examples/demo.sh            narrate: print every command and its output
#   bash examples/demo.sh --check    quiet: one line per checked step (what CI runs)
#   bash examples/demo.sh --keep     keep the temp folder and print its path
#
# Every step checks what it expects and the demo stops with exit 1 at the first surprise. It needs python3 >= 3.9
# and git, never the network. See examples/README.md for the story.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python3}"
ONTO_BIN="$ROOT/plugins/general-ontology/bin/onto"

QUIET=0
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --check) QUIET=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "demo: unknown option $arg (use --check or --keep)" >&2; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || { echo "demo: git is needed" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' \
  || { echo "demo: $PY must be python >= 3.9 (set PYTHON)" >&2; exit 1; }

# A fixed clock and a neutral git: the same demo gives the same bytes on every machine.
export ONTO_FIXED_NOW="${ONTO_FIXED_NOW:-2026-09-28T12:00:00Z}"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
export GIT_AUTHOR_DATE="$ONTO_FIXED_NOW" GIT_COMMITTER_DATE="$ONTO_FIXED_NOW"
export PYTHONDONTWRITEBYTECODE=1
unset ONTO_REPO ONTO_PROFILE ONTO_HANDOFF ONTO_DENYLIST CLAUDE_PROJECT_DIR 2>/dev/null || true

W="$(mktemp -d "${TMPDIR:-/tmp}/onto-demo.XXXXXX")"
W="$(cd "$W" && pwd -P)"
finish() {
  local code=$?
  if [ "$KEEP" = 1 ]; then echo "demo: kept $W"; else rm -rf "$W"; fi
  exit "$code"
}
trap finish EXIT

# helpers -------------------------------------------------------------------------------------------------------
cat >"$W/helpers.py" <<'PYEOF'
"""Small JSON helpers for demo.sh (standard library only)."""
import hashlib
import json
import os
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def main(cmd, args):
    if cmd == "pack-proposal":  # <example dir> <out>: the local pack and questions as additive pack ops
        folder, out = args
        pack = load(os.path.join(folder, "local.pack.json"))
        ops = [{"op": "add_kind", "name": k, "kind": pack["kinds"][k]} for k in sorted(pack.get("kinds") or {})]
        ops += [{"op": "add_relation", "name": r, "relation": pack["relations"][r]}
                for r in sorted(pack.get("relations") or {})]
        ops += [{"op": "map_kinds", "a": e["a"], "b": e["b"]} for e in pack.get("kind_map") or []]
        qpath = os.path.join(folder, "local.questions.jsonl")
        if os.path.isfile(qpath):
            ops += [{"op": "add_question", "question": q} for q in jsonl(qpath)]
        write(out, {"by": "user", "summary": "Local pack: %s" % (pack.get("title") or "local"), "ops": ops})
    elif cmd == "split-answers":  # <answers.jsonl> <dir>: one folder per answer with q, text and ops.json
        src, out = args
        for n, line in enumerate(jsonl(src), start=1):
            d = os.path.join(out, "%02d" % n)
            os.makedirs(d)
            with open(os.path.join(d, "q"), "w", encoding="utf-8") as fh:
                fh.write(line["q"])
            with open(os.path.join(d, "text"), "w", encoding="utf-8") as fh:
                fh.write(line.get("text") or "")
            write(os.path.join(d, "ops.json"), line.get("ops") or [])
    elif cmd == "fill-src":  # <proposal template> <src id> <out> [drop op n]
        template, src, out = args[:3]
        with open(template, encoding="utf-8") as fh:
            obj = json.loads(fh.read().replace("@SRC@", src))
        if len(args) > 3:
            del obj["ops"][int(args[3]) - 1]
        write(out, obj)
    elif cmd == "get":  # <json file> <python expression over obj>
        obj = load(args[0])
        val = eval(args[1], {"obj": obj, "any": any, "all": all, "len": len, "sorted": sorted})
        print(json.dumps(val) if isinstance(val, (dict, list)) else val)
    elif cmd == "fenced":  # <text file> <needle>: every line with the needle sits inside untrusted fences
        with open(args[0], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        inside, seen = False, 0
        for line in lines:
            if line.startswith("[untrusted src:") and " begins " in line:
                inside = True
            elif line.startswith("[untrusted src:") and line.rstrip().endswith(" ends]"):
                inside = False
            elif args[1] in line:
                seen += 1
                if not inside:
                    print("outside")
                    return
        print("fenced" if seen else "absent")
    elif cmd == "body-chars":  # <text file>: characters after the version line
        with open(args[0], encoding="utf-8") as fh:
            print(len(fh.read().split("\n", 1)[-1].rstrip("\n")))
    elif cmd == "verdicts":  # <review json> <accept|reject> <id a> <id b>: op numbers over the whole proposal
        obj = load(args[0])
        if (obj.get("paging") or {}).get("more"):
            raise SystemExit("helpers: the review lists only some ops; read it with --limit 0")
        pair = sorted(args[2:4])
        ops = obj["proposal"]["ops"]
        hit = [o["n"] for o in ops
               if o.get("op") == "add_edge" and sorted([o["edge"]["src"], o["edge"]["dst"]]) == pair]
        print(",".join("%d" % n for n in (hit if args[1] == "accept" else [o["n"] for o in ops if o["n"] not in hit])))
    elif cmd == "only-p10":  # <validate json> <exit code>: "ok" when clean, or failing only on P10; else why not
        try:
            obj = load(args[0])
        except ValueError:
            obj = None
        code = int(args[1])
        if not isinstance(obj, dict):
            print("no JSON object on stdout")
        elif "error" in obj:
            print("validate failed: %s %s" % (obj.get("error"), obj.get("message") or ""))
        elif not isinstance(obj.get("problems"), list):
            print("no problems list in the result")
        else:
            codes = sorted({p.get("code") for p in obj["problems"] if isinstance(p, dict)})
            if (code == 0 and not obj["problems"]) or (code == 1 and obj["problems"] and codes == ["P10"]):
                print("ok")
            else:
                print("exit %d with problems %s" % (code, codes))
    elif cmd == "sha256":
        with open(args[0], "rb") as fh:
            print(hashlib.sha256(fh.read()).hexdigest())
    elif cmd == "tamper":  # <file>: change one byte of a vendored export
        with open(args[0], "rb") as fh:
            data = fh.read()
        with open(args[0], "wb") as fh:
            fh.write(data.replace(b"Weekly menu", b"Weekly menus", 1))
    else:
        raise SystemExit("helpers: unknown command %s" % cmd)


main(sys.argv[1], sys.argv[2:])
PYEOF

h() { "$PY" "$W/helpers.py" "$@"; }

STEP=""
step() {
  STEP="$1"
  if [ "$QUIET" = 1 ]; then :; else printf '\n== %s\n' "$1"; fi
}
ok() { if [ "$QUIET" = 1 ]; then printf 'ok   %s\n' "$1"; else printf '  ok: %s\n' "$1"; fi; }
fail() {
  printf 'FAIL %s: %s\n' "$STEP" "$1" >&2
  if [ -s "$W/.out" ]; then sed 's/^/  | /' "$W/.out" | head -40 >&2; fi
  if [ -s "$W/.err" ]; then sed 's/^/  ! /' "$W/.err" | head -20 >&2; fi
  exit 1
}

TOPIC=""
SILENT=0
# run <expected exit code> <onto arguments...>: runs in the current topic folder; stdout in .out, stderr in .err.
# Narration prints the command and its output; JSON output (read by the checks) is cut to its first lines.
run() {
  local want="$1"
  shift
  local where="$W"
  if [ -n "$TOPIC" ]; then where="$W/$TOPIC"; fi
  local show=1
  if [ "$QUIET" = 1 ] || [ "$SILENT" = 1 ]; then show=0; fi
  if [ "$show" = 1 ]; then
    local shown="$*"
    printf '\n[%s] $ onto %s\n' "${TOPIC:-demo}" "${shown//$W/\$W}"
  fi
  set +e
  (cd "$where" && "$PY" "$ONTO_BIN" "$@") >"$W/.out" 2>"$W/.err"
  CODE=$?
  set -e
  if [ "$show" = 1 ]; then
    case " $* " in
      *" --json "*)
        head -n 12 "$W/.out"
        local lines
        lines="$(wc -l <"$W/.out" | tr -d ' ')"
        if [ "$lines" -gt 12 ]; then printf '  ... (%s more lines of JSON)\n' "$((lines - 12))"; fi
        ;;
      *) cat "$W/.out" ;;
    esac
    if [ -s "$W/.err" ]; then cat "$W/.err"; fi
  fi
  if [ "$CODE" != "$want" ]; then fail "onto $1 exited $CODE, expected $want"; fi
}

# expect "<what>" "<python expression over obj, the JSON on stdout>"
expect() {
  if [ "$(h get "$W/.out" "bool($2)")" != "True" ]; then fail "$1"; fi
  ok "$1"
}
# expect_text "<what>" "<text that stdout or stderr must contain>"
expect_text() {
  if ! grep -qF -- "$2" "$W/.out" "$W/.err"; then fail "$1 (missing: $2)"; fi
  ok "$1"
}
value() { h get "$W/.out" "$1"; }

git_repo() {
  git -C "$1" init -q
  git -C "$1" symbolic-ref HEAD refs/heads/main
  git -C "$1" config user.name "Demo"
  git -C "$1" config user.email "demo@example.invalid"
  git -C "$1" config commit.gpgsign false
  git -C "$1" config tag.gpgsign false
  git -C "$1" config core.hooksPath /dev/null
}
commit_all() { git -C "$W/$TOPIC" add -A && git -C "$W/$TOPIC" commit -q --no-verify --allow-empty -m "$1"; }

# new_topic <folder> <name> <ns> <title>: what a clone of the template gives, then onto init. The topic rules are
# written as fixed bytes (the same as test_e2e's), so an edit to the template's own dotfiles cannot move a commit id.
new_topic() {
  TOPIC="$1"
  mkdir -p "$W/$1"
  printf '%s\n' 'inbox/' '.onto/' 'build/index.html' '__pycache__/' '*.pyc' '.DS_Store' '* [0-9].*' >"$W/$1/.gitignore"
  printf '%s\n' '* text=auto eol=lf' '**/sources/** -text' >"$W/$1/.gitattributes"
  printf '**/%s merge=union\n' interview/log.jsonl ledger/changes.jsonl metrics/history.jsonl sources/index.jsonl \
    packs/local.questions.jsonl >>"$W/$1/.gitattributes"
  git_repo "$W/$1"
  run 0 init --name "$2" --ns "$3" --title "$4"
  commit_all "init $3"
}

RICH=""
richness() {  # the richness score, read quietly from onto status --json
  SILENT=1
  run 0 status --json
  SILENT=0
  RICH="$(value 'obj["richness"]["score"] if isinstance(obj.get("richness"), dict) else obj["version"]["richness"]["score"]')"
  if [ "$QUIET" = 0 ]; then printf '  (richness %s)\n' "$RICH"; fi
}

apply_pack() {  # the example's local pack and questions, as one proposal accepted in full
  h pack-proposal "$HERE/$1" "$W/$TOPIC-pack.json"
  run 0 propose --proposal "@$W/$TOPIC-pack.json" --json
  local pid
  pid="$(value 'obj["proposal"]["id"]')"
  run 0 apply "$pid" --all accept
}

ANSWERS=0
replay() {  # replay <answers.jsonl>: every answer through onto answer --apply
  local dir="$W/$TOPIC-answers"
  rm -rf "$dir"
  mkdir -p "$dir"
  h split-answers "$1" "$dir"
  ANSWERS=0
  for d in "$dir"/*; do
    run 0 answer "$(cat "$d/q")" "$(cat "$d/text")" --ops "@$d/ops.json" --apply
    ANSWERS=$((ANSWERS + 1))
  done
}

release() {  # release <notes> <tag>
  commit_all "data before $2"
  run 0 release --write --commit --notes "$1"
  if [ "$(git -C "$W/$TOPIC" tag -l "$2")" != "$2" ]; then fail "release did not tag $2"; fi
  ok "released $TOPIC $2"
}

if [ "$QUIET" = 0 ]; then echo "demo: working in W=$W"; fi

# 1. garden ------------------------------------------------------------------------------------------------------
step "garden: init, local pack, interview"
new_topic garden community-garden garden "Community garden"
richness; R_INIT="$RICH"
apply_pack garden
richness; R_PACK="$RICH"
replay "$HERE/garden/answers.jsonl"
[ "$ANSWERS" = 8 ] || fail "expected 8 garden answers, replayed $ANSWERS"
richness; R_ANSWERS="$RICH"
[ "$R_ANSWERS" -gt "$R_PACK" ] && [ "$R_PACK" -ge "$R_INIT" ] \
  || fail "richness did not rise over the interview ($R_INIT, $R_PACK, $R_ANSWERS)"
ok "8 answers applied; richness $R_INIT -> $R_ANSWERS"

step "garden: ingest the handbook excerpt"
cp "$HERE/garden/inbox/handbook-excerpt.md" "$W/garden/inbox/"
run 0 ingest inbox/handbook-excerpt.md --title "Volunteer handbook excerpt" --json
SRC="$(value 'obj["source"]["id"]')"
expect "redactions counted" 'sum(obj["redactions"].values()) >= 2 and "email" in obj["redactions"]'
run 0 get "$SRC" --full
[ "$(h fenced "$W/.out" "IGNORE ALL PREVIOUS")" = "fenced" ] || fail "the injection line is not inside untrusted fences"
[ "$(h fenced "$W/.out" "</script>")" = "fenced" ] || fail "the script line is not inside untrusted fences"
ok "the injection line appears only inside untrusted fences"
richness; R_INGEST="$RICH"
[ "$R_INGEST" -ge "$R_ANSWERS" ] || fail "richness fell after ingest ($R_ANSWERS -> $R_INGEST)"

step "garden: propose, fix, review"
h fill-src "$HERE/garden/handbook.proposal.json" "$SRC" "$W/handbook.json"
run 1 propose --proposal "@$W/handbook.json" --json
expect "refused for the fabricated quote in op 6" 'any(p.get("n") == 6 and p.get("code") == "quote" for p in obj["problems"])'
h fill-src "$HERE/garden/handbook.proposal.json" "$SRC" "$W/handbook-fixed.json" 6
run 0 propose --proposal "@$W/handbook-fixed.json" --json
expect "role:bed-captain matches role:bed-steward" 'any(m["id"] == "role:bed-captain" for m in obj["matches"])'
PROP="$(value 'obj["proposal"]["id"]')"
run 0 eval --gold "$HERE/gold/handbook.gold.json" --proposal "$PROP" --json
expect "eval scores the proposal" '0 < obj["nodes"]["recall"] <= 1 and 0 < obj["edges"]["precision"] <= 1'
run 0 apply "$PROP" --accept 1-4,7 --draft 5 --reject 6
richness; R_APPLY="$RICH"
[ "$R_APPLY" -gt "$R_INGEST" ] || fail "richness did not rise after the review ($R_INGEST -> $R_APPLY)"
ok "reviewed: 5 accepted with the merge, 1 draft, 1 rejected; richness $R_INGEST -> $R_APPLY"
run 0 next --json
expect "next asks who keeps the harvest log" 'any(q["ask"] == "Who keeps Harvest log up to date?" for q in obj["questions"])'
run 0 get constraint:no-pesticides --json
expect "constraint:no-pesticides exists" 'obj["node"]["id"] == "constraint:no-pesticides"'
run 0 validate
ok "garden validates clean"
release first v1

# 2. kitchen -----------------------------------------------------------------------------------------------------
step "kitchen: init, local pack, interview, menu notes"
new_topic kitchen neighborhood-kitchen kitchen "Neighborhood kitchen"
apply_pack kitchen
replay "$HERE/kitchen/answers.jsonl"
cp "$HERE/kitchen/inbox/menu-notes.md" "$W/kitchen/inbox/"
run 0 ingest inbox/menu-notes.md --title "Menu notes" --json
SRC="$(value 'obj["source"]["id"]')"
h fill-src "$HERE/kitchen/menu.proposal.json" "$SRC" "$W/menu.json"
run 0 propose --proposal "@$W/menu.json" --json
PROP="$(value 'obj["proposal"]["id"]')"
run 0 apply "$PROP" --all accept
for id in ingredient:tomato dish:tomato-salad process:menu-planning constraint:allergen-labels deliverable:weekly-menu
do
  run 0 get "$id" --json
done
ok "the kitchen nodes exist"
run 0 validate
release first v1

# 3. garden-to-table ---------------------------------------------------------------------------------------------
step "garden-to-table: import both releases, map kinds, bridges"
new_topic g2t garden-to-table g2t "Garden to table"
run 0 import add --ns garden --from "$W/garden" --ref v1
run 0 import add --ns kitchen --from "$W/kitchen" --ref v1
run 0 propose --proposal "@$HERE/garden-to-table/bridges.proposal.json" --json
PROP="$(value 'obj["proposal"]["id"]')"
run 0 apply "$PROP" --all accept
run 0 import suggest --ns garden --with kitchen --json
expect "the top bridge candidate is tomato same_as tomato" \
  'sorted([obj["candidates"][0]["a"], obj["candidates"][0]["b"]]) == ["garden/crop:tomato", "kitchen/ingredient:tomato"]'
PROP="$(value 'obj["proposal"]["id"]')"
run 0 review "$PROP" --limit 0 --json
VERDICTS="$(h verdicts "$W/.out" accept garden/crop:tomato kitchen/ingredient:tomato)" || fail "cannot read the review"
OTHERS="$(h verdicts "$W/.out" reject garden/crop:tomato kitchen/ingredient:tomato)" || fail "cannot read the review"
[ -n "$VERDICTS" ] || fail "suggest proposed no garden/crop:tomato same_as kitchen/ingredient:tomato op"
if [ -n "$OTHERS" ]; then run 0 apply "$PROP" --accept "$VERDICTS" --reject "$OTHERS"; else run 0 apply "$PROP" --accept "$VERDICTS"; fi
ok "accepted garden/crop:tomato same_as kitchen/ingredient:tomato"

step "garden-to-table: stage C interview and a decision"
replay "$HERE/garden-to-table/answers.jsonl"
run 0 decide --question "When does the weekly menu lock?" --options "thu=Thursday noon,fri=Friday morning" \
  --recommended thu --chosen thu --rationale "The shopping list goes out on Thursday afternoon." \
  --scope "kitchen/deliverable:weekly-menu,goal:weekly-harvest-menu"
run 0 path garden/role:bed-steward kitchen/deliverable:weekly-menu --json
expect "a path crosses a bridge" 'len(obj["paths"]) > 0 and any(s.get("bridge") for s in obj["paths"][0])'
run 0 brief "weekly menu" --budget 800
expect_text "the brief names garden" "garden/"
expect_text "the brief names kitchen" "kitchen/"
[ "$(h body-chars "$W/.out")" -le 3200 ] || fail "the brief does not fit 800 tokens (3,200 characters)"
ok "the brief fits 800 tokens"
run 0 context "write the weekly menu"
expect_text "the context names the goal" "goal:weekly-harvest-menu"
expect_text "the context names a constraint" "constraint:"
for heading in "Goal" "People and roles" "Data and sources" "Processes" "Constraints" "Active decisions" "Open points"
do
  expect_text "the context keeps the heading $heading" "$heading"
done
run 0 gaps --limit 0 --json
expect "gaps lists unbridged_import" 'any(g["type"] == "unbridged_import" for g in obj["gaps"])'
run 0 validate
release first v1

# 4. market ------------------------------------------------------------------------------------------------------
step "market: import garden-to-table without its parents' clones"
new_topic market farm-market market "Farm market"
mv "$W/garden" "$W/garden.away"
mv "$W/kitchen" "$W/kitchen.away"
run 0 import add --ns g2t --from "$W/g2t" --ref v1
mv "$W/garden.away" "$W/garden"
mv "$W/kitchen.away" "$W/kitchen"
cp "$W/market/imports/lock.json" "$W/.out"
expect "garden and kitchen come via g2t" \
  'sorted((e["ns"], e["via"], e["from"]) for e in obj["imports"] if e["ns"] != "g2t") == [("garden", "g2t", None), ("kitchen", "g2t", None)]'
replay "$HERE/market/answers.jsonl"

step "market: garden v2 is a pin conflict until a decision allows it"
TOPIC=garden
printf 'Squash grows in the north bed from this spring.\n' >"$W/garden/inbox/spring-note.md"
run 0 ingest inbox/spring-note.md --title "Spring planting note" --json
SRC="$(value 'obj["source"]["id"]')"
cat >"$W/squash.json" <<EOF
{"by":"agent","source":"$SRC","summary":"Spring note: squash in the north bed","ops":[
 {"op":"add_node","ref":"\$squash","node":{"kind":"crop","name":"Squash","summary":"Grown in the north bed from this spring."},
  "conf":0.8,"prov":[{"src":"$SRC","loc":"L1-L1","quote":"Squash grows in the north bed","by":"agent"}]},
 {"op":"add_edge","edge":{"src":"\$squash","rel":"grown_in","dst":"plot:north-bed"},
  "conf":0.8,"prov":[{"src":"$SRC","loc":"L1-L1","quote":"Squash grows in the north bed","by":"agent"}]}]}
EOF
run 0 propose --proposal "@$W/squash.json" --json
PROP="$(value 'obj["proposal"]["id"]')"
run 0 apply "$PROP" --all accept
release second v2
KEEP_SHA="$(h sha256 "$W/garden/build/export.json")"
TOPIC=market
run 1 import add --ns garden --from "$W/garden" --ref v2
expect_text "garden v2 is refused as a pin conflict" "pin conflict"
run 0 decide --question "Which garden release should the market pin?" --options "v1=Garden v1 through g2t,v2=Garden v2" \
  --chosen v2 --rationale "v2 adds squash, which the stall sells." --scope "garden/" --json
DEC="$(value 'obj["decision"]["id"]')"
run 0 import add --ns garden --from "$W/garden" --ref v2 --override "$DEC" --keep "garden=$KEEP_SHA"
ok "garden v2 pinned under $DEC"
set +e
(cd "$W/market" && "$PY" "$ONTO_BIN" validate --json) >"$W/.out" 2>"$W/.err"
VCODE=$?
set -e
WHY="$(h only-p10 "$W/.out" "$VCODE")"
[ "$WHY" = ok ] || fail "validate after the switch: $WHY"
ok "validate reports a dangling bridge as P10, or nothing"

# 5. tamper ------------------------------------------------------------------------------------------------------
step "tamper: a vendored export edited by hand"
TOPIC=g2t
cp "$W/g2t/imports/kitchen/export.json" "$W/kitchen-export.bak"
h tamper "$W/g2t/imports/kitchen/export.json"
run 1 validate
expect_text "validate finds P15" "P15"
FIRST="$(head -n 1 "$W/.out")"
[ -n "$FIRST" ] || FIRST="$(head -n 1 "$W/.err")"
case "$FIRST" in *mismatch*) ;; *) fail "the version line does not say mismatch: $FIRST" ;; esac
ok "the version line says mismatch"
cp "$W/kitchen-export.bak" "$W/g2t/imports/kitchen/export.json"
run 0 validate

# 6. determinism -------------------------------------------------------------------------------------------------
step "determinism: every export built twice"
for t in garden kitchen g2t market; do
  TOPIC="$t"
  run 0 build --out "$W/build-1/$t"
  run 0 build --out "$W/build-2/$t"
  for f in export.json cards.json; do
    cmp -s "$W/build-1/$t/$f" "$W/build-2/$t/$f" || fail "$t $f differs between two builds"
  done
  ok "$t builds the same bytes twice"
done

printf '\ndemo: all steps passed (richness of garden %s -> %s)\n' "$R_INIT" "$R_APPLY"
