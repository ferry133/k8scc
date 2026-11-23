#!/usr/bin/env bash
#
# Controls for lan-inventory-check (k8scc#118): the one command im's profile
# allows for finding out whether the LAN inventory it wrote can be parsed. It
# runs as shipped, with HOME redirected. The broken file is the shape
# fleet-ops-1e read on a pod (2026-11-24): an unquoted list item holding
# "word: word". Needs PyYAML for the python on PATH; without it nothing here
# can discriminate, and the run fails as "cannot measure".
#
# Run: test/lan-inventory-check-controls.sh   (from the repo root; also in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }
check_not() { local d="$1"; shift; if "$@"; then bad "$d"; else ok; fi; }

PY=${PYTHON:-python3}
if ! "$PY" -c 'import yaml' 2>/dev/null; then
    echo "  FAIL CANNOT MEASURE: no PyYAML for $PY; no cell ran"
    echo "lan-inventory-check: passed=0 failed=1"
    exit 1
fi

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$work/workspace"
f="$work/workspace/lan-inventory.yaml"
run() { env HOME="$work" "$PY" ./lan-inventory-check "$@" > "$work/out" 2> "$work/err"; }

good() { cat <<'Y'
# written by im
meta:
  generated_by: im
  limitations:
    - "The x.x.x.1 rows are not unknown devices: the controller lists them elsewhere."
hosts:
  - ip: 10.9.9.1
    source: both
  - ip: 10.9.9.7
    source: controller
  - ip: 10.9.9.8
    source: controller
  - ip: 10.9.9.50
    source: scan
Y
}

echo "a file that parses: the counts"
good > "$f"; run; rc=$?
check "rc 0" [ "$rc" = 0 ]
check "rows and each source counted" [ "$(cat "$work/out")" = "lan-inventory-check: ok: rows=4 controller=2 scan=1 both=1" ]
check "nothing on stderr" [ ! -s "$work/err" ]
printf 'hosts: []\n' > "$f"; run
check "an empty list is a valid inventory of nothing" [ "$(cat "$work/out")" = "lan-inventory-check: ok: rows=0 controller=0 scan=0 both=0" ]

echo "the pod's broken shape: an unquoted list item with a colon and a space"
good | awk '/^    - "The x.x.x.1/ { print "    - The x.x.x.1 rows are the gateway and not"; print "      unknown devices: the controller lists them elsewhere."; next } { print }' > "$f"
check "control: the fixture really differs from the good file" [ "$(grep -c 'unknown devices: the' "$f")" = 1 ]
check_not "control: and the quoted line is gone" grep -q '"The x.x.x.1' "$f"
run; rc=$?
check "rc 1" [ "$rc" = 1 ]
check "the line is named (6, where the colon is)" grep -q 'not valid YAML at line 6, column ' "$work/err"
check "it says what to do" grep -q 'must be quoted' "$work/err"
check "no count is printed for a file that does not parse" [ ! -s "$work/out" ]

echo "parses, but is not an inventory"
printf -- '- a\n- b\n' > "$f"; run
check "top level a list ⇒ 1" [ $? = 1 ]
printf 'meta: {}\n' > "$f"; run; rc=$?
check "no hosts ⇒ 1" [ "$rc" = 1 ]
check "… and says missing" grep -q '`hosts` is missing' "$work/err"
printf 'hosts: {a: 1}\n' > "$f"; run
check "hosts a mapping ⇒ 1" [ $? = 1 ]
printf 'hosts:\n  - 10.9.9.1\n' > "$f"; run
check "a row that is a string ⇒ 1" [ $? = 1 ]
check "… said as such, not as a traceback" grep -q 'row 1 of `hosts` is not a mapping' "$work/err"
check_not "… no traceback" grep -q Traceback "$work/err"
good | awk '!d && /source: controller/ {sub(/controller/, "nmap"); d=1} {print}' > "$f"
check "control: the bad source is in the fixture" grep -q 'source: nmap' "$f"
run; rc=$?
check "an unknown source ⇒ 1" [ "$rc" = 1 ]
check "… naming the row (2)" grep -q 'row 2 of `hosts`' "$work/err"
good | grep -v 'source: scan' > "$f"; run
check "a row without a source ⇒ 1" [ $? = 1 ]
check "… naming the row (4)" grep -q 'row 4 of `hosts`' "$work/err"

echo "cannot measure is not a verdict"
rm -f "$f"; run
check "no file ⇒ 2" [ $? = 2 ]
good > "$f"; chmod 000 "$f"
if [ "$(id -u)" != 0 ]; then run; check "unreadable ⇒ 2" [ $? = 2 ]; fi
chmod 600 "$f"
head -c 4194305 /dev/zero | tr '\0' '#' > "$f"; run
check "larger than the limit ⇒ 2, not read" [ $? = 2 ]
good > "$f"
mkdir -p "$work/nopy"; printf 'raise ImportError("no parser here")\n' > "$work/nopy/yaml.py"
env HOME="$work" PYTHONPATH="$work/nopy" "$PY" ./lan-inventory-check > "$work/out" 2> "$work/err"; rc=$?
check "no parser ⇒ 2" [ "$rc" = 2 ]
check "… and says the file was not read" grep -q 'no YAML parser' "$work/err"
run; check "control: the same file with the parser ⇒ 0" [ $? = 0 ]

echo "it reads that one file and takes nothing from the caller"
printf 'hosts: []\n' > "$work/other.yaml"
for a in "$work/other.yaml" --file="$work/other.yaml" -h --help /etc/passwd ''; do
    run "$a"; rc=$?
    check "argument '$a' ⇒ 2" [ "$rc" = 2 ]
    check "… and nothing on stdout" [ ! -s "$work/out" ]
done
run; check "control: no argument ⇒ 0" [ $? = 0 ]
printf 'hosts:\n  - {source: scan, note: !!python/object/apply:os.system ["touch %s/pwned"]}\n' "$work" > "$f"; run; rc=$?
check "a python tag is not executed" [ ! -e "$work/pwned" ]
check "… and the file is refused (1)" [ "$rc" = 1 ]

echo "shipped and allowed"
check "the image copies it"            grep -q '^COPY lan-inventory-check /usr/local/bin/lan-inventory-check$' Dockerfile
check "the image installs a pinned parser" grep -qE '"PyYAML==[0-9.]+"' Dockerfile
check "the build fails without the parser" grep -q "python3 -c 'import yaml'" Dockerfile
check "the profile allows exactly the bare command" grep -qF "'Bash(lan-inventory-check)'" claude-session
check_not "no wildcard form of the rule" grep -qF 'Bash(lan-inventory-check *' claude-session

echo "lan-inventory-check: passed=$pass failed=$fail"
[ "$fail" = 0 ]
