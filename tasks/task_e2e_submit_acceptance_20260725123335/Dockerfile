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

WORKDIR /workspace

COPY base/ /workspace/
COPY requirements/ /workspace/requirements/

RUN mkdir -p /workspace/build
