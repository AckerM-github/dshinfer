# syntax=docker/dockerfile:1
#
# dshinfer 单容器镜像：服务层（FastAPI）+ 进程内引擎（llama-cpp-python → libllama.so）
#
# 和上一版的区别（上一版把 llama-server 当子进程黑盒）：
#   1. 镜像里没有 llama-server，也没有 /opt/llama：引擎在服务进程内跑，
#      指标、trace、日志、进程生命周期都只有一份。
#   2. 引擎绑定不在容器里现编，而是用 llama-cpp-python 官方发布的 manylinux wheel
#      （0.3.36，与本机 vendor/llama_cpp 同源），wheel 自带 libllama.so /
#      libggml*.so / libggml-cuda.so —— 所以构建时不需要 nvcc，也不需要 CUDA Toolkit。
#   3. 基础镜像用 CUDA **runtime**（不是 devel）：只缺 libcublas / libcudart 时它已经带齐。
#
# 权重不进镜像层（5.24 GB，且实验期会换 Q4_K_M / Q3_K_M），由 compose 以只读卷挂到 /models。
#
# 构建：docker compose -f docker/compose.yaml build
# 单跑：docker build -f docker/Dockerfile -t dshinfer:local .

ARG UBUNTU_BASE=ubuntu:24.04
# 全局 ARG 必须声明在第一个 FROM 之前，否则后面的 FROM 里拿到的是空值
ARG CUDA_BASE=nvidia/cuda:12.8.0-runtime-ubuntu24.04

# ---------- 阶段 1：构建工程 wheel（hatchling 只活在这一阶段，不进运行时镜像）----------
# 运行时依赖完全由 docker/requirements.lock（从 uv.lock 导出、每包带 sha256）决定；
# 这一阶段只把 src/ 打成 wheel，固定 hatchling 版本，产物是纯 Python 文件，没有编译差异。
FROM ${UBUNTU_BASE} AS wheel

ARG HATCHLING_VERSION=1.33.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

RUN python3 -m venv /build-venv \
 && /build-venv/bin/pip install --no-cache-dir --upgrade pip \
 && /build-venv/bin/pip install --no-cache-dir "hatchling==${HATCHLING_VERSION}" \
 && /build-venv/bin/pip wheel --no-deps --no-build-isolation --no-cache-dir -w /wheels . \
 && ls -l /wheels

# ---------- 阶段 2：运行时镜像 ----------
FROM ${CUDA_BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3.12 是 24.04 的默认 python3；libgomp1 是 llama.cpp 的 OpenMP 运行时；
# tini 当 PID 1，负责把 SIGTERM 转发给 uvicorn。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      libgomp1 libstdc++6 \
      curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 运行时依赖：按 docker/requirements.lock 安装（uv.lock 导出、每包带 sha256）。
# 重新生成（改了 pyproject.toml / uv.lock 之后）：
#   uv export --frozen --no-dev --extra tui --format requirements.txt \
#       --no-header --no-emit-project -o docker/requirements.lock
# --require-hashes：包内容与锁不一致（版本被换 / 镜像源不同 / 供应链投毒）会直接构建失败。
COPY docker/requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
 && rm -f /tmp/requirements.lock

# 引擎绑定：官方 manylinux wheel（自带 libllama.so + libggml-cuda.so，无编译）。
# --no-deps：它的依赖（numpy / diskcache / jinja2 / typing-extensions）已经在上面那份锁里。
# 原生 Windows 走 vendor/llama_cpp（同一份 Python 代码），两者版本必须一致。
ARG LLAMA_CPP_VERSION=0.3.36
ARG LLAMA_CPP_WHEEL_URL=https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.36-cu124/llama_cpp_python-0.3.36-py3-none-manylinux_2_35_x86_64.whl
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    /opt/venv/bin/pip install --no-deps "${LLAMA_CPP_WHEEL_URL}" \
 && /opt/venv/bin/python - <<'PY'
import importlib.metadata as md
import pathlib
import sysconfig

# 只做静态检查，**不能 import llama_cpp**：它在模块级 dlopen libllama.so，
# 而 libllama.so 依赖宿主驱动注入的 libcuda.so.1 —— 构建期没有，
# 运行期由 nvidia container runtime 挂进来。见 §17。
ver = md.version("llama-cpp-python")
assert ver == "0.3.36", ver
site = pathlib.Path(sysconfig.get_paths()["purelib"])
lib = site / "llama_cpp" / "lib"
names = sorted(p.name for p in lib.glob("lib*.so*"))
assert any(n.startswith("libllama.so") for n in names), names
assert any("ggml-cuda" in n for n in names), names
print("llama-cpp-python", ver, "libs:", len(names), names)
PY

# 工程本体：wheel 来自 wheel 阶段，--no-deps 保证不会再解析或升级任何依赖
COPY --from=wheel /wheels /tmp/wheels
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps /tmp/wheels/dshinfer-*.whl \
 && rm -rf /tmp/wheels \
 && /opt/venv/bin/python -c "import dshinfer, fastapi, httpx; print('dshinfer import ok')" \
 && /opt/venv/bin/python -c "import dshinfer.tui; print('dshinfer.tui import ok')"

# 版本自证：镜像里记录实际装了什么（docker exec dshinfer cat /app/runtime/pip-freeze.txt）
COPY docker/requirements.lock /app/runtime/requirements.lock

# 配置契约：与仓库里的 config/*.env 是同一份文件，容器与 Windows 原生读同一套默认值。
# config.py 的 _env_files() 在镜像里优先读 /app/runtime/base.env 与 /app/runtime/docker.env。
COPY config/base.env /app/runtime/base.env
COPY config/docker.env /app/runtime/docker.env
RUN /opt/venv/bin/pip freeze --all > /app/runtime/pip-freeze.txt

# chat template 随镜像走（原文件在 D:\llm\runtime\chat_template.jinja，8908 B），
# 需要换模型时优先用 compose 的卷覆盖 /app/runtime/chat_template.jinja
COPY docker/chat_template.jinja /app/runtime/chat_template.jinja

# 关键：服务层默认 host 是 127.0.0.1，容器里必须 0.0.0.0 才能被端口映射访问。
# 引擎是进程内的，没有引擎端口、没有引擎日志文件，也没有 LD_LIBRARY_PATH 需要摆。
ENV PATH=/opt/venv/bin:$PATH \
    DSHINFER_HOST=0.0.0.0 \
    DSHINFER_PORT=8000 \
    DSHINFER_MODELS_DIR=/models \
    DSHINFER_GGUF_PATH=/models/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf \
    DSHINFER_CHAT_TEMPLATE_PATH=/app/runtime/chat_template.jinja

RUN mkdir -p /var/log/dshinfer /models

EXPOSE 8000

# 注意：/healthz 只要 HTTP 层起来就回 200，不代表模型已 ready。
# start-period 给足首次加载时间（8 GB 笔记本卡 + 5.24 GB 权重，实测 4–10 s，冷启动留余量）
HEALTHCHECK --interval=15s --timeout=5s --start-period=240s --retries=20 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "dshinfer.main"]
