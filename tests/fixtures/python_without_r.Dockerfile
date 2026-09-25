# Context: repository source plus dist/*.whl and an executable dist/uv for Linux.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*
COPY dist/uv /usr/local/bin/uv
COPY dist/*.whl /tmp/wheels/
RUN uv python install 3.12 && uv tool install /tmp/wheels/*.whl
ENV PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COPY tests /acceptance/tests
WORKDIR /acceptance
CMD ["uv", "run", "--no-project", "--python", "3.12", "tests/without_r.py"]
