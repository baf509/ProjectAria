# =============================================================================
# llama.cpp with ROCm support for AMD APUs/GPUs
# Pre-built binaries from: https://github.com/lemonade-sdk/llamacpp-rocm
# =============================================================================
#
# Default target: gfx1151 (Ryzen AI MAX+ Pro 395 / STX Halo)
# Override GPU_TARGET build arg for other architectures:
#   gfx1150 - STX Point (Ryzen AI 300)
#   gfx120X - RDNA4 (RX 9070/9060)
#   gfx110X - RDNA3 (RX 7900/7800/7700/7600)

FROM ubuntu:24.04

ARG GPU_TARGET=gfx1151
ARG LLAMACPP_RELEASE=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates jq \
    # ROCm runtime dependencies
    libdrm2 libdrm-amdgpu1 libnuma1 libelf1 \
    && rm -rf /var/lib/apt/lists/*

# Download pre-built llamacpp-rocm release
# Binaries are at the root of the zip (no bin/ subdirectory)
RUN set -ex && \
    if [ "$LLAMACPP_RELEASE" = "latest" ]; then \
        RELEASE_TAG=$(curl -sL https://api.github.com/repos/lemonade-sdk/llamacpp-rocm/releases/latest | jq -r .tag_name); \
    else \
        RELEASE_TAG="$LLAMACPP_RELEASE"; \
    fi && \
    URL="https://github.com/lemonade-sdk/llamacpp-rocm/releases/download/${RELEASE_TAG}/llama-${RELEASE_TAG}-ubuntu-rocm-${GPU_TARGET}-x64.zip" && \
    echo "Downloading: $URL" && \
    curl -fSL -o /tmp/llamacpp.zip "$URL" && \
    mkdir -p /opt/llamacpp && \
    unzip -o /tmp/llamacpp.zip -d /opt/llamacpp && \
    rm /tmp/llamacpp.zip && \
    chmod +x /opt/llamacpp/llama-server /opt/llamacpp/llama-cli 2>/dev/null || true

# Set library paths so bundled ROCm libs are found
ENV LD_LIBRARY_PATH=/opt/llamacpp:/opt/llamacpp/lib:${LD_LIBRARY_PATH}
ENV PATH=/opt/llamacpp:${PATH}

# Model storage
VOLUME /models

EXPOSE 8080

# Default: start llama-server on port 8080
# Override LLAMACPP_MODEL to point to your GGUF file
ENV LLAMACPP_MODEL=/models/model.gguf
ENV LLAMACPP_GPU_LAYERS=99
ENV LLAMACPP_CTX_SIZE=8192
ENV LLAMACPP_HOST=0.0.0.0
ENV LLAMACPP_PORT=8080

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["exec llama-server \
    --model \"$LLAMACPP_MODEL\" \
    --n-gpu-layers $LLAMACPP_GPU_LAYERS \
    --ctx-size $LLAMACPP_CTX_SIZE \
    --host $LLAMACPP_HOST \
    --port $LLAMACPP_PORT"]
