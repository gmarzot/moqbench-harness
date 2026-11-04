FROM public.ecr.aws/ubuntu/ubuntu:22.04
ARG RUST_VERSION=1.98.1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git cmake clang pkg-config perl nasm binutils \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
ENV PATH=/opt/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain "$RUST_VERSION" \
    && rm /tmp/rustup.sh
WORKDIR /src
