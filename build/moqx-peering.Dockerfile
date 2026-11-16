# moqx's relay image, plus libmoqx_peering.so when the build makes one; newer builds link it statically.
ARG BUILDER
ARG RELAY
FROM ${BUILDER} AS builder
RUN mkdir -p /out && find /src/_build -name 'libmoqx_peering.so*' -exec cp -a {} /out/ \;
FROM ${RELAY}
COPY --from=builder /out/ /usr/local/lib/
RUN ldconfig && ! ldd /usr/local/bin/moqx | grep -q 'not found'
