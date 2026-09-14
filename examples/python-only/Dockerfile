# Build from the repository root. Neither stage installs R.
FROM rust:1.95-bookworm AS toolchain
ENV CARGO_PROFILE_RELEASE_DEBUG=0
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv pkg-config libcap-dev cmake clang && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build && /opt/build/bin/pip install uv

FROM toolchain AS build
WORKDIR /source
COPY sandbox-runner.json ./
COPY scripts/stage-sandbox-runner ./scripts/
RUN python3 scripts/stage-sandbox-runner
COPY . .
RUN /opt/build/bin/uv build --wheel --out-dir /wheels

FROM python:3.13-slim-bookworm
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    libcap2 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install numpy pandas matplotlib duckdb uv
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl
RUN mkdir /workspace
WORKDIR /workspace
CMD ["mcp-console"]
