# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# ---- Stage 2: Runtime ----
FROM python:3.11-slim

RUN useradd --create-home --uid 1000 --shell /bin/bash alla

COPY --from=builder /usr/local/lib/python3.11/site-packages/ /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/alla* /usr/local/bin/

WORKDIR /app

COPY --chown=alla:alla knowledge_base/ knowledge_base/

USER alla

EXPOSE 8090

CMD ["alla-server"]
