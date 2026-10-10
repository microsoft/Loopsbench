# Adapted from Harbor for LoopsBench; see NOTICE and LICENSE.harbor.
FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS builder
WORKDIR /src
# The released/nightly binaries still pin x before its per-request bypass fix.
# Pin both source revisions so rebuilding cannot silently change authorization.
RUN git init . \
 && git remote add origin https://github.com/go-gost/gost.git \
 && git fetch --depth=1 origin 11220b15bba1539b5c6e6a2fef245720e4065419 \
 && git checkout --detach FETCH_HEAD \
 && go mod edit -require=github.com/go-gost/x@v0.19.5-0.20261003164611-dc9c7444b92e
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
 && CGO_ENABLED=0 go build -p 4 -trimpath -ldflags="-s -w" -o /gost ./cmd/gost

COPY dns /tmp/egress-dns
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go test /tmp/egress-dns/main.go /tmp/egress-dns/main_test.go \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /egress-dns /tmp/egress-dns/main.go

FROM gogost/gost:3.2.7-nightly.20260602@sha256:afc0137758ab4ce399d47a299f9abbacbf522b52a17e59cbb4b4e7a1a66e9196
COPY --from=builder /gost /bin/gost
COPY --from=builder /egress-dns /bin/egress-dns

COPY gost.yaml allowlist.txt /opt/egress-sidecar/
COPY --chmod=755 entrypoint.sh /opt/egress-sidecar/entrypoint.sh
COPY --chmod=755 bin/network-policy /usr/local/bin/network-policy

RUN chmod 755 /opt/egress-sidecar/entrypoint.sh /usr/local/bin/network-policy
COPY NOTICE LICENSE.harbor /opt/egress-sidecar/
