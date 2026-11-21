#!/usr/bin/env bash
#
# Controls for lan-scan (k8scc#105): the one scan command the permission
# profile allows. It runs as shipped, with fake nmap / arp-scan ahead on PATH
# that record their argv one field per line. A network is scanned with exactly
# the argv below; anything that is not a network is refused before either tool
# runs, the options the security review named (--script, -oN, -iL,
# --pcapsavefile) included.
#
# Run: test/lan-scan-controls.sh   (from the repo root; also runs in CI)

set -uo pipefail
cd "$(dirname "$0")/.."

pass=0 fail=0
ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "  FAIL: $*"; }
check()     { local d="$1"; shift; if "$@"; then ok; else bad "$d"; fi; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
for t in nmap arp-scan; do
    printf '%s\n' '#!/usr/bin/env bash' "{ echo '--- $t'; for a in \"\$@\"; do printf '%s\n' \"\$a\"; done; } >> \"\$FAKE_LOG\"" \
        "[ -e \"\$FAKE_DIR/$t.fail\" ] && exit 3; echo '$t ran'" > "$work/bin/$t"
    chmod +x "$work/bin/$t"
done
scan() { : > "$work/log"; env PATH="$work/bin:$PATH" FAKE_LOG="$work/log" FAKE_DIR="$work" bash ./lan-scan "$@" > "$work/out" 2> "$work/err"; }

echo "a network is scanned with exactly this argv"
scan 10.9.9.0/24; rc=$?
check "rc 0" [ "$rc" = 0 ]
check "nmap then arp-scan, nothing else" [ "$(cat "$work/log")" = "$(printf '%s\n' '--- nmap' -sn -n 10.9.9.0/24 '--- arp-scan' 10.9.9.0/24)" ]
check "both outputs printed under a header" [ "$(grep -c '^## ' "$work/out")" = 2 ]
scan 10.9.9.0/24 10.9.8.0/24 10.9.1.7/32
check "three networks: six runs" [ "$(grep -c '^--- ' "$work/log")" = 6 ]
check "… each network once per tool" [ "$(grep -cx '10.9.8.0/24' "$work/log")" = 2 ]
scan 10.9.0.0/16; check "/16 is the widest accepted" [ $? = 0 ]

echo "private networks only, passed on as decimal (fleet-ops-d5 on #107)"
for net in 172.16.0.0/16 172.31.5.0/24 192.168.1.0/24 10.255.0.0/16; do
  scan "$net"; check "accepted: $net" [ $? = 0 ]
done
for net in 8.8.8.0/24 1.34.181.0/24 172.15.0.0/16 172.32.0.0/16 192.169.0.0/16 11.0.0.0/16 100.64.0.0/16; do
  scan "$net"; rc=$?
  check "refused, not private: $net" [ "$rc" = 2 ]
  check "… neither tool ran: $net" [ ! -s "$work/log" ]
done
scan 010.009.009.000/24
check "leading zeros: passed on as decimal, not as typed (010 is octal 8 to inet_aton)" \
  [ "$(cat "$work/log")" = "$(printf '%s\n' '--- nmap' -sn -n 10.9.9.0/24 '--- arp-scan' 10.9.9.0/24)" ]
scan 10.9.9.0/024; check "a three-digit prefix is refused" [ $? = 2 ]

echo "a tool that fails"
touch "$work/nmap.fail"; scan 10.9.9.0/24; rc=$?; rm -f "$work/nmap.fail"
check "rc 1" [ "$rc" = 1 ]
check "… arp-scan still ran" grep -qx -- '--- arp-scan' "$work/log"

echo "refused before either tool runs"
for args in \
    "10.9.9.0/24 --script=x" "--script=x 10.9.9.0/24" "-oN /tmp/x" "10.9.9.0/24 -oN /tmp/x" \
    "-iL /etc/passwd" "--pcapsavefile=/tmp/x 10.9.9.0/24" "-sS 10.9.9.0/24" "10.9.9.1" \
    "10.0.0.0/8" "10.9.9.0/33" "8.8.8.0/24" "10.9.9.256/24" "999.9.9.0/24" "10.9.9.0/24;id" '10.9.9.0/24$(id)' \
    "10.9.9.0/24
10.9.8.0/24" "fe80::/64" "localhost" ""; do
    # word-split on purpose: each case is the argv im would type
    if [ -z "$args" ]; then scan; else IFS=' ' read -r -a argv <<<"$args"; [[ "$args" == *$'\n'* ]] && argv=("$args"); scan "${argv[@]}"; fi
    rc=$?
    check "refused (rc 2): $(printf %q "$args")" [ "$rc" = 2 ]
    check "… neither tool ran: $(printf %q "$args")" [ ! -s "$work/log" ]
done
scan 10.9.1.0/24 10.9.2.0/24 10.9.3.0/24 10.9.4.0/24 10.9.5.0/24 10.9.6.0/24 10.9.7.0/24 10.9.8.0/24 10.9.9.0/24; rc=$?
check "nine valid networks refused" [ "$rc" = 2 ]
check "… neither tool ran" [ ! -s "$work/log" ]
scan 10.9.1.0/24 10.9.2.0/24 10.9.3.0/24 10.9.4.0/24 10.9.5.0/24 10.9.6.0/24 10.9.7.0/24 10.9.8.0/24
check "control: eight networks accepted" [ $? = 0 ]

echo "lan-scan: passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
