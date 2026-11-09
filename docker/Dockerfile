# syntax=docker/dockerfile:1
#
# dshinfer 单容器镜像：服务层（FastAPI）+ 引擎（llama.cpp / llama-server）+ CUDA 运行库
#
# 为什么这样分层：本项目把 llama-server 当"子进程黑盒"用（见 src/dshinfer/engine/llamacpp_server.py），
# 所以引擎天然可以和 Python 服务打进同一个容器——不需要改一行 src/ 代码，只需要把
# Windows 的 D:\llm\runtime\llama.cpp\llama-server.exe 换成同 build 号的 Linux CUDA 构建。
#
# 权重不进镜像层（5.24 GB，且会随实验频繁换），由 compose 以只读卷挂到 /models。
#
# 构建：docker compose -f docker/compose.yaml build
# 单跑：docker build -f docker/Dockerfile -t dshinfer:local .

ARG UBUNTU_BASE=ubuntu:24.04

# ---------- 阶段 1：取官方 llama.cpp Linux CUDA 构建（与 Windows 侧同为 b12202）----------
FROM ${UBUNTU_BASE} AS engine

ARG LLAMA_BUILD=12202
ARG LLAMA_CUDA=12.8
# 国内网络可指向镜像/内网缓存，或改成 file:// 配合 --build-context 预下载
ARG RELEASE_BASE=https://github.com/ggml-org/llama.cpp/releases/download

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/dl
# 发布包内部目录层级不稳定（可能带 llama-b12202/、cudart-llama-b12202/ 前缀），
# 所以不写死解包路径：find 定位 llama-server 所在目录后摊平到 /opt/llama/bin，
# 再把两个包里所有 lib*.so* 收进 /opt/llama/lib（bin 内自带的 .so 也会被 cp -an 补齐）。
RUN set -eux; \
    curl -fL --retry 3 --retry-delay 2 -o llama.tar.gz \
      "${RELEASE_BASE}/b${LLAMA_BUILD}/llama-b${LLAMA_BUILD}-bin-ubuntu-cuda-${LLAMA_CUDA}-x64.tar.gz"; \
    curl -fL --retry 3 --retry-delay 2 -o cudart.tar.gz \
      "${RELEASE_BASE}/b${LLAMA_BUILD}/cudart-llama-b${LLAMA_BUILD}-bin-ubuntu-cuda-${LLAMA_CUDA}-x64.tar.gz"; \
    mkdir -p /opt/llama/bin /opt/llama/lib /opt/llama/raw; \
    tar -xzf llama.tar.gz -C /opt/llama/raw; \
    tar -xzf cudart.tar.gz -C /opt/llama/raw; \
    rm -f llama.tar.gz cudart.tar.gz; \
    SERVER="$(find /opt/llama/raw -type f -name llama-server | head -n1)"; \
    test -n "$SERVER"; \
    SRCDIR="$(dirname "$SERVER")"; \
    cp -a "$SRCDIR"/. /opt/llama/bin/; \
    find /opt/llama/raw -type f -name 'lib*.so*' -exec cp -an {} /opt/llama/lib/ \; ; \
    rm -rf /opt/llama/raw; \
    chmod +x /opt/llama/bin/llama-server; \
    ls -lh /opt/llama/bin/llama-server /opt/llama/lib | head -n 20

# ---------- 阶段 2：运行时镜像 ----------
FROM ${UBUNTU_BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1：llama.cpp 的 OpenMP 运行时；tini：当 PID 1，负责转发 SIGTERM 给 uvicorn 和引擎子进程
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      libgomp1 libstdc++6 \
      curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=engine /opt/llama /opt/llama

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# 装进独立 venv：不污染系统 python，构建完可以直接 rm 掉 pip 缓存
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install . \
 && /opt/venv/bin/python -c "import dshinfer, fastapi, httpx; print('dshinfer import ok')"

# chat template 随镜像走（原文件在 D:\llm\runtime\chat_template.jinja，8908 B），
# 需要换模型时优先用 compose 的卷覆盖 /app/runtime/chat_template.jinja
COPY docker/chat_template.jinja /app/runtime/chat_template.jinja

# 关键：服务层默认 host 是 127.0.0.1，容器里必须 0.0.0.0 才能被端口映射访问
ENV PATH=/opt/venv/bin:$PATH \
    LD_LIBRARY_PATH=/opt/llama/bin:/opt/llama/lib \
    DSHINFER_HOST=0.0.0.0 \
    DSHINFER_PORT=8000 \
    DSHINFER_ENGINE=llamacpp \
    DSHINFER_LLAMA_SERVER_PATH=/opt/llama/bin/llama-server \
    DSHINFER_MODELS_DIR=/models \
    DSHINFER_GGUF_PATH=/models/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf \
    DSHINFER_CHAT_TEMPLATE_PATH=/app/runtime/chat_template.jinja \
    DSHINFER_ENGINE_LOG_PATH=/var/log/dshinfer/llama-server.log

RUN mkdir -p /var/log/dshinfer /models

EXPOSE 8000

# 注意：/healthz 只要 HTTP 层起来就回 200，不代表模型已 ready。
# start-period 给足首次加载时间（8 GB 笔记本卡 + 5.24 GB 权重，实测 10–40 s，冷启动留余量）
HEALTHCHECK --interval=15s --timeout=5s --start-period=240s --retries=20 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "dshinfer.main"]
