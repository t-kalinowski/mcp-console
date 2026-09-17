# Build from this repository's root. The final Linux template has no native runner.
FROM rust:1.95-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv pkg-config libcap-dev cmake clang && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build && /opt/build/bin/pip install uv
WORKDIR /source
COPY sandbox-runner.json checkout_workflow.py ./
COPY scripts/stage-sandbox-runner ./scripts/
RUN python3 scripts/stage-sandbox-runner
COPY . .
RUN cargo build --release

# Docker's shell variant supplies no coding agent or model authentication flow.
# The -docker variant also permits testing retirement of containers inside the VM.
FROM docker.io/docker/sandbox-templates:shell-docker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base-core r-base-dev r-cran-reticulate r-cran-jsonlite r-cran-png \
    python3 python3-dev python3-venv ca-certificates && rm -rf /var/lib/apt/lists/*
RUN Rscript --vanilla -e 'install.packages("reticulate", repos="https://cloud.r-project.org"); stopifnot(requireNamespace("reticulate"))'
RUN python3 -m venv /opt/analysis && /opt/analysis/bin/pip install numpy pandas matplotlib duckdb uv
COPY --from=build /source/target/release/mcp-console /usr/local/bin/mcp-console
ENV PATH="/opt/analysis/bin:${PATH}" RETICULATE_PYTHON=/opt/analysis/bin/python \
    R_HOME=/usr/lib/R R_PROFILE_USER=/dev/null
RUN mkdir -p /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
