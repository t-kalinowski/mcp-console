# Build from the repository root: docker build -f examples/docker/Dockerfile -t my-console:analysis .
FROM rust:1.95-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv pkg-config libcap-dev cmake clang && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build && /opt/build/bin/pip install uv
WORKDIR /source
COPY sandbox-runner.json ./
COPY scripts/stage-sandbox-runner ./scripts/
RUN python3 scripts/stage-sandbox-runner
COPY . .
RUN /opt/build/bin/uv build --wheel --out-dir /wheels

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base-core r-cran-reticulate r-cran-dbi r-cran-jsonlite r-cran-png \
    python3 python3-venv python3-dev libcap2 ca-certificates build-essential && rm -rf /var/lib/apt/lists/*
# Large shared libraries need -fPIC on ARM64 as well as x86-64.
RUN mkdir -p /root/.R && printf '%s\n' 'CXXPICFLAGS=-fPIC' 'CXX17PICFLAGS=-fPIC' 'CXX20PICFLAGS=-fPIC' > /root/.R/Makevars
# Current reticulate supplies the runtime bridge used by Console.
RUN MAKEFLAGS=-j2 Rscript --vanilla -e 'install.packages(c("DBI", "nanoarrow", "reticulate"), repos="https://cloud.r-project.org"); stopifnot(requireNamespace("DBI"), requireNamespace("nanoarrow"), requireNamespace("reticulate"))'
RUN MAKEFLAGS=-j2 Rscript --vanilla -e 'install.packages("duckdb", repos="https://cloud.r-project.org"); stopifnot(requireNamespace("duckdb"))'
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake libssl-dev libcurl4-openssl-dev && rm -rf /var/lib/apt/lists/*
# DuckDB's Arrow scanner requires Dataset support, including for in-memory SQL previews.
RUN LIBARROW_BINARY=true ARROW_DATASET=ON MAKEFLAGS=-j2 Rscript --vanilla -e \
    'install.packages("arrow", repos="https://cloud.r-project.org"); stopifnot(all(vapply(c("reticulate", "duckdb", "DBI", "nanoarrow", "arrow"), requireNamespace, logical(1), quietly=TRUE)), arrow::arrow_info()$capabilities[["dataset"]])'
RUN Rscript --vanilla -e \
    'install.packages(c("pillar", "tibble", "utf8"), repos="https://cloud.r-project.org"); stopifnot(all(vapply(c("pillar", "tibble", "utf8"), requireNamespace, logical(1), quietly=TRUE)))'
RUN python3 -m venv /opt/analysis && /opt/analysis/bin/pip install \
    numpy pandas matplotlib duckdb uv
COPY --from=build /wheels /wheels
RUN /opt/analysis/bin/pip install /wheels/*.whl && rm -rf /wheels
ENV PATH="/opt/analysis/bin:${PATH}" RETICULATE_PYTHON=/opt/analysis/bin/python \
    R_HOME=/usr/lib/R R_PROFILE_USER=/dev/null
RUN mkdir /workspace
WORKDIR /workspace
CMD ["mcp-console"]
