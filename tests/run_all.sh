#!/usr/bin/env bash
# run_all.sh - every test suite in this package. Run it on a Linux PC or VM, NOT on a live deck: the tests bind the
# same multicast groups and ports as the real services.
#   bash tests/run_all.sh            (as a normal user; ~15 minutes)
# Needs: python3 3.9+; for ui_test, dash_*_test: playwright + Chromium; for boot_test: root (network namespaces),
# otherwise it is skipped; for dm_relay_e2e, dm_route_e2e: a local RabbitMQ (rabbitmq-server) and pika, otherwise
# they are skipped; for meshwatch_test's real-alfred part: root and an alfred binary (ALFRED_BIN=...), otherwise
# that part is skipped. The bridge always runs the way the service runs it: python3 -S with the bundled vendor/ libraries.
# Tests that load the bridge's code directly run with python3 -S too (clean), so they never pick up other copies of
# PyTAK, takproto or protobuf installed on this computer.
cd "$(dirname "$0")"
mkdir -p /tmp/mmb_chat /tmp/mmb_dm /tmp/mmb_ident
PKG="$(cd .. && pwd)"; FAIL=0
run() {   # a suite fails on a non-zero exit, a missing summary, or a summary short of its total
  printf '%-24s ' "$1"; out="$(timeout 900 python3 $PYFLAGS "$@" 2>&1)"; r=$?
  sum="$(echo "$out" | grep -oE '[0-9]+/[0-9]+ passed' | tail -1)"; echo "${sum:-(no summary)}"
  a="${sum%%/*}"; b="${sum#*/}"; b="${b%% *}"
  if [ $r != 0 ] || [ -z "$sum" ] || [ "$a" != "$b" ]; then FAIL=1; echo "$out" | grep -E '^FAIL|Traceback' | head -5; fi
}
clean() { PYFLAGS=-S run "$@"; }   # python3 -S: no packages from this computer, only the bundled vendor/ ones
skip() { printf '%-24s skipped: %s\n' "$1" "$2"; }
has() { python3 -c "import $1" >/dev/null 2>&1; }
run privacy_test.py "$PKG"
clean proto_test.py /tmp/mmb_proto_test
run suite_test.py /tmp/mmb_suite_test
run outbox_test.py
clean dm_filter_test.py
clean itak_udp_dm_test.py
clean meshaddr_test.py
clean deck_status_test.py
clean meshwatch_test.py
clean ident_check.py
clean durability_test.py
clean pytak_patch_test.py
clean fresh_setup_test.py
run fake_ots_test.py
clean e2e_chat.py
run dm_e2e.py
run gps_test.py "$PKG/chat" "$PKG/dashboard" /tmp/mmb_gps_test
run mesh_nodes_test.py
run wizard_test.py
run echo_test.py
run look_test.py
run voice_test.py "$PKG"
run rotate_test.py
clean regex_worst.py
run big_test.py
run link_test.py
run robust_test.py
clean repair_test.py
run outbox_e2e.py
clean fuzz_test.py "$PKG" /tmp/mmb_fuzz_test 5000 5000
if has playwright; then run ui_test.py; run dash_server_link_test.py; run dash_dm_route_test.py; run dash_link_test.py
else skip "ui + dashboard tests" "playwright not installed"; fi
if [ "$(id -u)" = 0 ]; then run boot_test.py; else skip boot_test.py "needs root (network namespaces)"; fi
if command -v rabbitmqctl >/dev/null && rabbitmqctl -q status >/dev/null 2>&1 && has pika; then
  run ../server/mesh_mailroute/tests/mailrule_test.py
  run dm_relay_e2e.py "$PKG/chat" "$PKG/server/mesh_mailroute" /tmp/mmb_dmrelay
  run dm_route_e2e.py "$PKG/bridge" "$PKG/server/mesh_mailroute" /tmp/mmb_dmroute
else skip "RabbitMQ tests" "no local RabbitMQ (or pika)"; fi
if [ "$(id -u)" != 0 ]; then printf '%-24s ' install_sim.sh; bash install_sim.sh "$PKG" > /tmp/mmb_install_sim.log 2>&1; r=$?; tail -1 /tmp/mmb_install_sim.log; [ $r = 0 ] || { FAIL=1; grep FAIL /tmp/mmb_install_sim.log | head -5; }
else skip install_sim.sh "run it as a normal user (the installer refuses root)"; fi
[ "$FAIL" = 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit $FAIL
