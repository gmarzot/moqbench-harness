# FFmpeg for the contribution source fixtures only; never in a carriage path.
FROM ubuntu:26.04
ARG FFMPEG_REF=n9.0.2
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential pkg-config nasm libx264-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${FFMPEG_REF} https://github.com/FFmpeg/FFmpeg.git /src/ffmpeg \
    && cd /src/ffmpeg && ./configure --prefix=/usr/local --enable-gpl --enable-libx264 \
         --disable-doc --disable-ffplay --disable-debug \
    && make -j"$(nproc)" && make install && cd / && rm -rf /src/ffmpeg
LABEL org.moqbench.ffmpeg="${FFMPEG_REF}"
ENTRYPOINT ["ffmpeg"]
