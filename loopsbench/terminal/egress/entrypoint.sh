#!/bin/sh
# Adapted from Harbor for LoopsBench; see NOTICE and LICENSE.harbor.
set -eu
rm -f /tmp/egress.ready
# Docker shares these files with containers joining our network namespace.
# Capture trusted bootstrap configuration before the task can start. GOST's
# Go resolver must not consult a task-writable /etc/hosts afterward.
cp /etc/resolv.conf /tmp/egress-resolv.conf
cp /etc/hosts /tmp/egress-hosts
chmod 600 /tmp/egress-resolv.conf /tmp/egress-hosts
printf 'hosts: dns\n' > /etc/nsswitch.conf
case "${EGRESS_CONTROL_INITIAL_NETWORK_MODE:-public}" in
  public) network-policy allow-all ;;
  no-network) network-policy deny-all ;;
  allowlist)
    set -f
    network-policy allow ${EGRESS_CONTROL_INITIAL_ALLOWED_HOSTS:-}
    ;;
  *) exit 2 ;;
esac
touch /tmp/egress.ready
# docker exec applies phase changes and may replace the proxy process.
# Reap old processes so phase transitions can wait for their listener to close.
while :; do sleep 1 & wait "$!"; done
