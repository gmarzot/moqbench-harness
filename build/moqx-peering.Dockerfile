# moqx's relay image plus libmoqx_peering.so, which upstream's relay stage leaves out.
ARG BUILDER
ARG RELAY
FROM ${BUILDER} AS builder
RUN mkdir -p /out && find /src/_build -name 'libmoqx_peering.so*' -exec cp -a {} /out/ \; && ls /out | grep -q peering
FROM ${RELAY}
COPY --from=builder /out/ /usr/local/lib/
RUN ldconfig && ! ldd /usr/local/bin/moqx | grep -q 'not found'
