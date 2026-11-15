# Fabric peer node: the lab image plus the aiomoqt build that carries the peering tools.
ARG BASE
FROM ${BASE}
ARG SPEC
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --pre "${SPEC}"
