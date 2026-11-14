# Fabric lab node: aiomoqt clients, control-API helpers and tc for relay namespaces.
FROM python:3.12-slim-bookworm
ARG AIOMOQT=0.15.0rc1
ARG AIOPQUIC=0.8.0rc1
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 procps curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --pre "aiomoqt==${AIOMOQT}" "aiopquic==${AIOPQUIC}"
RUN python -m aiomoqt.versions > /etc/aiomoqt-versions.txt 2>&1 || true
LABEL org.moqbench.aiomoqt="${AIOMOQT}" org.moqbench.aiopquic="${AIOPQUIC}"
