FROM postgres:18.4-bookworm AS postgres-client

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/usr/lib/postgresql/16/bin:${PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends --yes age libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=postgres-client /usr/lib/postgresql/16 /usr/lib/postgresql/16
COPY --from=postgres-client /usr/lib/x86_64-linux-gnu/libpq.so.5 /usr/lib/x86_64-linux-gnu/libpq.so.5

RUN pip install --no-cache-dir uv==0.11.26

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 docket
USER docket

EXPOSE 8000
CMD ["./.venv/bin/uvicorn", "docket.main:app", "--host", "0.0.0.0", "--port", "8000"]

ARG DOCKET_BUILD_REVISION=unknown
ENV DOCKET_BUILD_REVISION=${DOCKET_BUILD_REVISION}
LABEL org.opencontainers.image.source="https://github.com/nkuhanas/Docket" \
      org.opencontainers.image.revision="${DOCKET_BUILD_REVISION}"
