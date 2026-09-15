# First build the Python-only Console image with Dockerfile in this directory.
ARG CONSOLE_IMAGE=mcp-console-python:analysis
FROM ${CONSOLE_IMAGE} AS console

FROM docker.io/docker/sandbox-templates:shell-docker
USER root
RUN UV_PYTHON_INSTALL_DIR=/opt/python uv venv --python 3.13 /opt/analysis && uv pip install --python /opt/analysis/bin/python numpy pandas matplotlib duckdb
COPY --from=console /usr/local/bin/mcp-console /usr/local/bin/mcp-console
ENV PATH="/opt/analysis/bin:${PATH}" RETICULATE_PYTHON=/opt/analysis/bin/python
RUN mkdir -p /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
