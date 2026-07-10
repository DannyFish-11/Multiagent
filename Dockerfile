# memory-agent 服务镜像(PHASE2.5 M-C)
# 多阶段构建;镜像内不含模型权重、不含任何密钥(密钥经环境变量/.env 注入)
# 目标 < 500MB(python:3.12-slim + 纯 API 依赖,不装 local-embed/torch)

# ---- 构建阶段:装依赖到独立虚拟环境 ----
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# 只装运行时依赖(含 a2a extra;不含 local-embed 的 torch 全家桶)
RUN uv sync --frozen --no-dev --extra a2a --no-install-project
COPY adapters ./adapters
COPY core ./core
COPY services ./services
RUN uv sync --frozen --no-dev --extra a2a

# ---- 运行阶段 ----
FROM python:3.12-slim
RUN useradd -m -u 10001 agent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY adapters ./adapters
COPY core ./core
COPY services ./services
COPY scripts ./scripts
COPY config.yaml ./config.yaml
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MEMORY_AGENT_CONFIG=/app/config.yaml
# 记忆数据/日志/导出目录全部挂 volume(容器可随意销毁重建)
VOLUME ["/app/data", "/app/logs", "/app/exports", "/app/reports"]
RUN mkdir -p /app/data /app/logs /app/exports /app/reports && chown -R agent:agent /app
USER agent
EXPOSE 8002
# healthcheck 用标准库,slim 镜像无需 curl
HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=20s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/healthz',timeout=4).status==200 else 1)"
CMD ["python", "-m", "services.api"]
