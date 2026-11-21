# Contribution lab tools: the namespace holders, the WAN emulator, the taps and
# the SRT and RIST lanes. libsrt and librist are built from pinned tags; TSDuck
# comes from its pinned release package.
FROM ubuntu:26.04
ARG SRT_REF=v1.5.7
ARG RIST_REF=v0.2.20
ARG TSDUCK_VERSION=3.45-4798
ARG TSDUCK_SHA256=318317926aa2c04d1e1d4da11143e696fd27aaf4cfcdba3902c0c9fdcdf14214
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git build-essential cmake pkg-config meson ninja-build tclsh libssl-dev \
      iproute2 iputils-ping nftables ethtool socat iperf3 procps tcpdump python3 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${SRT_REF} https://github.com/Haivision/srt.git /src/srt \
    && cmake -S /src/srt -B /src/srt/build -DCMAKE_BUILD_TYPE=Release -DENABLE_APPS=ON \
         -DENABLE_SHARED=ON -DENABLE_STATIC=OFF -DCMAKE_INSTALL_PREFIX=/usr/local \
         -DUSE_ENCLIB=openssl-evp -DENABLE_AEAD_API_PREVIEW=ON \
    && cmake --build /src/srt/build -j"$(nproc)" && cmake --install /src/srt/build && ldconfig \
    && rm -rf /src/srt
RUN git clone --depth 1 --branch ${RIST_REF} https://code.videolan.org/rist/librist.git /src/librist \
    && meson setup /src/librist/build /src/librist --buildtype=release --prefix=/usr/local -Dbuilt_tools=true \
    && ninja -C /src/librist/build && ninja -C /src/librist/build install && ldconfig \
    && rm -rf /src/librist
RUN curl -fsSL -o /tmp/tsduck.deb \
      "https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/tsduck_${TSDUCK_VERSION}.ubuntu26_amd64.deb" \
    && echo "${TSDUCK_SHA256}  /tmp/tsduck.deb" | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/tsduck.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/tsduck.deb
RUN { srt-live-transmit -version 2>&1 | head -1; echo 'srt-aead: on'; ristsender -h 2>&1 | grep -i -m1 version; \
        tsp --version 2>&1 | head -1; } > /etc/contrib-tools-versions.txt; cat /etc/contrib-tools-versions.txt
LABEL org.moqbench.srt="${SRT_REF}" org.moqbench.rist="${RIST_REF}" org.moqbench.tsduck="${TSDUCK_VERSION}"
