#!/bin/sh
# Adapted from Harbor for LoopsBench; see NOTICE and LICENSE.harbor.
set -eu
rm -f /tmp/egress.ready
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
