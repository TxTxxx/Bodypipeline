FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock .python-version README.md ./
COPY pipeline ./pipeline
COPY configs ./configs
RUN uv sync --locked --no-dev --no-editable --no-cache && useradd --uid 10001 --create-home pipeline && mkdir -p /data && chown pipeline:pipeline /data
USER pipeline
EXPOSE 8081
ENTRYPOINT ["embodied-pipeline"]
CMD ["server", "--config", "/app/configs/server.demo.json", "--host", "0.0.0.0"]
