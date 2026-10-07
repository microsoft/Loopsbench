# Adapted from Harbor for LoopsBench; see NOTICE and LICENSE.harbor.
FROM gogost/gost:3.2.7-nightly.20260602@sha256:afc0137758ab4ce399d47a299f9abbacbf522b52a17e59cbb4b4e7a1a66e9196

COPY gost.yaml allowlist.txt /opt/egress-sidecar/
COPY --chmod=755 entrypoint.sh /opt/egress-sidecar/entrypoint.sh
COPY --chmod=755 bin/network-policy /usr/local/bin/network-policy

RUN chmod 755 /opt/egress-sidecar/entrypoint.sh /usr/local/bin/network-policy
COPY NOTICE LICENSE.harbor /opt/egress-sidecar/
