# moq2ts lane: the moq2ts-cli publisher (built against a pinned moqxr) and the
# moq5-based moq2ts-receiver, both from pinned commits.
FROM ubuntu:26.04
ARG MOQ2TS_REF=5e7b72eedadf7623b0ffee87785a9e333a06e1b5
ARG MOQXR_REF=v0.4.18
ARG MOQ5_REF=6594c13e7b974926f9238718551bafa2a8cd8cb3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential cmake ninja-build pkg-config patch \
      qt6-base-dev libsrt-gnutls-dev libssl-dev \
      libavcodec-dev libavformat-dev libavutil-dev libavdevice-dev libswresample-dev libswscale-dev \
      socat iproute2 procps python3 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/openmoq/moq2ts.git /src/moq2ts && git -C /src/moq2ts checkout -q ${MOQ2TS_REF} \
    && git clone --depth 1 --branch ${MOQXR_REF} https://github.com/openmoq/moqxr.git /src/moqxr \
    && git clone https://github.com/openmoq/moq5.git /src/moq5 && git -C /src/moq5 checkout -q ${MOQ5_REF}
RUN cmake -S /src/moq2ts -B /src/moq2ts/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DMOQXR_SOURCE_DIR=/src/moqxr -DOPENMOQ_BUILD_TESTS=OFF -DBUILD_TESTING=OFF \
    && cmake --build /src/moq2ts/build --target moq2ts-cli \
    && install -m 0755 /src/moq2ts/build/moq2ts-cli /usr/local/bin/moq2ts-cli
RUN cd /src/moq5 && MOQ_DEPS_DIR=/src/moq5-deps scripts/setup_picoquic_deps.sh > /src/moq5-deps.env \
    && . /src/moq5-deps.env \
    && cmake -S /src/moq5 -B /src/moq5/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
         -DCMAKE_INSTALL_PREFIX=/opt/moq5 -DMOQ_BUILD_TESTS=OFF -DMOQ_BUILD_SIM=OFF -DMOQ_BUILD_EXAMPLES=OFF \
         -DMOQ_BUILD_ADAPTER_PICOQUIC=ON -DMOQ_BUILD_PQ_THREADED=ON -DMOQ_BUILD_SERVICE=ON \
         -DMOQ_BUILD_MSF=ON -DMOQ_BUILD_MEDIA_OBJECT=ON -DMOQ_BUILD_MPEG2TS=ON -DMOQ_BUILD_PACKAGE_EXPORTS=ON \
         -DMOQ_PICOQUIC_SOURCE_DIR="$MOQ_PICOQUIC_SOURCE_DIR" -DMOQ_PICOTLS_PREFIX="$MOQ_PICOTLS_PREFIX" \
    && cmake --build /src/moq5/build && cmake --install /src/moq5/build \
    && cmake -S /src/moq2ts/src/receive -B /src/receiver -G Ninja -DCMAKE_BUILD_TYPE=Release \
         -DCMAKE_PREFIX_PATH=/opt/moq5 -DMOQ_PICOQUIC_SOURCE_DIR="$MOQ_PICOQUIC_SOURCE_DIR" \
         -DMOQ_PICOTLS_PREFIX="$MOQ_PICOTLS_PREFIX" \
    && cmake --build /src/receiver --target moq2ts-receiver \
    && install -m 0755 /src/receiver/moq2ts-receiver /usr/local/bin/moq2ts-receiver

RUN ldconfig && ! ldd /usr/local/bin/moq2ts-cli /usr/local/bin/moq2ts-receiver | grep 'not found'
LABEL org.moqbench.moq2ts="${MOQ2TS_REF}" org.moqbench.moqxr="${MOQXR_REF}" org.moqbench.moq5="${MOQ5_REF}"
