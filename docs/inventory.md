# 文件清单（D:\llm\infer）

统计口径：不含 `.venv`、`__pycache__`、`.pytest_cache`、`eval/results`。
手写文件 **92 个 / 16,471 行**（`uv.lock` 1,081 行、`eval/latest.json` 403 行不入手写量）。

---

## 一、服务层源码 `src/dshinfer/`（28 文件 / 6,829 行）

三组：核心服务层（13 个）、引擎适配层（6 个）、终端面板 `tui/`（9 个，只在 `--extra tui` 时装）。
另有 `vendor/llama_cpp/`（15 文件 / 16,910 行，进程内引擎用的第三方拷贝，来源与规矩见 `vendor/README.md`）。

| 文件 | 行 | 职责 | 状态 |
|---|---:|---|---|
| `__init__.py` | 3 | 包标记 | 完成 |
| `api.py` | 299 | FastAPI 路由：11 个端点（含 `/admin/config` 配置契约）、SSE 生成器、OpenAI 风格错误、trace 注入、`X-Request-Id`/`Server-Timing` | 完成 |
| `config.py` | 206 | `Settings(BaseSettings)`，`DSHINFER_` 前缀；`env_file` 搜索链（镜像内副本 → 仓库 `config/base.env` → 本机 `.env`）+ 按平台分叉的路径默认值 | 完成 |
| `schemas.py` | 78 | OpenAI Chat Completions 协议模型（含 tools / tool_choice / stream_options） | 完成 |
| `service.py` | 604 | 核心编排：模板渲染、准入、流式分帧、usage、工具块解析、思考段剥离、trace 打点 | 完成，最大单文件 |
| `scheduler.py` | 217 | 优先级队列 + 老化 + 租户配额 + 槽位亲和 | 完成 |
| `toolcall.py` | 106 | `<tool_call>` → `tool_calls` 解析（多调用、参数强转、JSON 兜底） | 完成 |
| `trace.py` | 125 | 单请求打点与环形存储（512 条）、span 派生 | 完成 |
| `metrics.py` | 54 | Prometheus：请求数/令牌/队列等待/TTFT/在飞/排队深度 | 完成 |
| `logging.py` | 32 | structlog JSON 输出 | 完成 |
| `main.py` | 23 | uvicorn 启动入口 | 完成 |
| `registry.py` | 146 | 模型注册表：扫目录、缓存、默认模型、热发现 | 完成 |
| `gguf.py` | 199 | GGUF 头解析（架构/层数/上下文/维度/MoE），自写以便不依赖 llama.cpp | 完成 |
| `engine/__init__.py` | 32 | `build_engine()` 工厂（mock / inproc / llama-server） | 完成 |
| `engine/base.py` | 59 | `Engine` 协议 + `GenRequest`/`TokenEvent`/`Usage` | 完成 |
| `engine/llamacpp_server.py` | 231 | 子进程生命周期、启动参数（含 `-fit off`）、`/completion` 流解析、孤儿进程清理、`timings` 回读 | 完成 |
| `engine/inproc.py` | 268 | 进程内引擎：`vendor/llama_cpp` → `llama.dll`；`threading.Lock` 串成并发 1、`create_completion(stream=True)`、前缀复用与 `cached_tokens` 自算 | 完成，默认引擎 |
| `engine/manager.py` | 136 | 模型状态机 idle→loading→ready、互斥加载、热切换 | 完成 |
| `engine/mock.py` | 79 | 假引擎（无模型也能跑通全链路） | 完成 |
| `tui/__init__.py` | 25 | 面板模块分工与导出 | 完成 |
| `tui/__main__.py` | 149 | CLI 入口：向导 / `--monitor` / `--snapshot` / `--dump-env` / `--preset` | 完成 |
| `tui/wizard.py` | 613 | 10 步启动向导（每屏 ↑↓/Enter/Esc/?），产出预设 + env 片段 + 启动监视器 | 完成 |
| `tui/preset.py` | 299 | 预设数据模型：读写 json、导出 env、平台默认路径、`problems()`/`warnings()` | 完成 |
| `tui/live.py` | 515 | 采集编排层：每 2 s 把 NVML / 系统 / 两个 Prometheus 端点合成一帧快照 | 完成 |
| `tui/probe.py` | 1,162 | 采集层：NVML（ctypes 直读，不调 `nvidia-smi`）、CPU/内存、PCIe、显存 | 完成 |
| `tui/monitor.py` | 1,095 | Textual App：7 视图（1–7）、状态条、三主题、空格冻结、KPI/曲线/请求表 | 完成，面板最大单文件 |
| `tui/widgets.py` | 245 | 自绘组件：`Kpi`、`Spark`（块字符曲线 `spark_line`）、直方图、紧凑布局适配 | 完成 |
| `tui/theme.py` | 129 | 三套配色 STRATA/OCEAN/PAPER + 浅色主题可读性映射、`set_active_theme()` | 完成 |

## 二、测试 `tests/`（13 文件 / 1,880 行，134 个用例全绿）

| 文件 | 行 | 覆盖 |
|---|---:|---|
| `test_api.py` | 200 | 健康检查、模型列表、非流式、流式、stop、队列满、指标、工具调用三态 |
| `test_scheduler.py` | 125 | 插队、老化、租户配额、队列满、超时、前缀亲和、归还 |
| `test_trace.py` | 139 | span 计算、打点幂等、环形容量、API 回显/自动 ID、404 |
| `test_registry.py` | 89 | 现场构造 GGUF 头、坏文件、扫描、默认 ID、热发现 |
| `test_gguf.py` | 204 | GGUF 头解析：架构/层数/头数/MoE（含坏文件与截断） |
| `test_judge.py` | 87 | 九种判定器（含真跑代码） |
| `test_toolcall.py` | 76 | 单/多调用、参数类型、JSON 兜底、正文保留 |
| `test_reasoning.py` | 69 | 思考段切分（跨 chunk、无标记、关闭透传、工具共存） |
| `test_render.py` | 41 | 模板渲染回归（thinking 开关、tool_calls 历史） |
| `test_isolation.py` | 176 | **两形态隔离守卫**：调优键只能在 `config/base.env`、`base.env` 无绝对路径、`docker.env` 只提容器路径、compose 基础层无宿主路径、Dockerfile 必须带配置契约、`src/` 无宿主路径、`.ps1` 必须 ASCII 或带 BOM |
| `test_tui_probe.py` | 493 | 采集层：NVML/CPU/内存/PCIe 的降级路径、`/metrics` 解析 |
| `test_tui_monitor.py` | 113 | 面板纯函数：相位判定、缓存率、tok/s 派生、`spark_line`、CJK 宽度 |
| `test_tui_live.py` | 68 | 采集编排 + 快照在测试后端上的渲染 |

## 三、脚本 `scripts/`（24 文件 / 2,791 行）

两个入口脚本先看：`ops.ps1`（Windows 原生统一入口，13 个子命令）与 `verify_env.py`（两形态配置比对）；
容器侧的入口是 `docker/ops.sh`，子命令与 `ops.ps1` 同名，见 [deploy.md](deploy.md)。

| 文件 | 行 | 用途 |
|---|---:|---|
| `ops.ps1` | 239 | **Windows 原生统一入口**：up/down/stop/start/restart/status/check/logs/bench/models/tui/verify/fetch |
| `verify_env.py` | 147 | 采 `/admin/config` 或本地服务，按 17 个契约键比对两形态是否一致（`--compare`，退出码 0/1/2） |
| `supervise.ps1` | 124 | 看护进程：健康检查、失败阈值、重启、日志轮转、`stop.flag` |
| `start.ps1` | 44 | 拉起看护（防双开，等待 `/healthz` 就绪） |
| `stop.ps1` | 46 | 优雅停 → `taskkill /T` → 清端口与 llama-server 孤儿 |
| `fetch-model.sh` | 189 | 按 `models/models.json` 下载权重：断点续传 + sha256 校验（退出码 0/2/3/4/5/6） |
| `wsl-deploy.sh` | 162 | WSL 侧部署：clone/pull（默认 GitHub HTTPS）→ `ops.sh up --build` |
| `bench_load.py` | 211 | 四档并发压测，服务 vs 直连引擎对照（`--service/--engine/--levels`） |
| `bench_spec.py` | 152 | 投机解码（5 组配置、接受率） |
| `bench_sched.py` | 143 | 优先级调度 A/B（FIFO vs priority） |
| `bench_cache_reuse.py` | 137 | `--cache-reuse` 四种配置的前缀命中实验 |
| `profile_stream.py` | 143 | 流式开销剖析（服务 vs 引擎、pinning 开关） |
| `probe_prefix.py` | 150 | 跨请求前缀共享三种打法 |
| `probe_agent_cache.py` / `2` / `3` | 66 / 59 / 60 | DSH 场景下 1/2/4/6 会话的缓存命中 |
| `probe_tools.py` | 141 | 模型工具调用格式合规性（6/6） |
| `probe_long_ctx.py` | 53 | 6K / 21K 长上下文预填充 |
| `probe_openai_sse.py` | 53 | 逐帧校验 SSE 结构 |
| `probe_perf_service.py` | 82 | 服务层三段对话 TTFT/缓存/吞吐 |
| `probe_llamaserver.py` | 66 | 直连引擎基准 |
| `agent_demo.py` | 137 | 最小 agent 循环（工具执行 + 回灌） |
| `trace_one.py` | 107 | 单请求 trace 拉取 + 瀑布图 |
| `smoke_service.py` | 80 | 冒烟：非流式/流式/指标 |

## 四、容器与配置 `docker/` + `config/`（8 文件 / 1,636 行）

| 文件 | 行 | 用途 |
|---|---:|---|
| `docker/Dockerfile` | 145 | 三阶段：engine（官方 llama.cpp + cudart）/ wheel（hatch 打包）/ runtime（`pip --require-hashes` + 烤入配置契约） |
| `docker/compose.yaml` | 74 | 基础编排：GPU 预留（nvidia 运行时）+ 权重只读卷 + `env_file` + healthcheck |
| `docker/compose.wsl.yaml` | 25 | WSL 覆盖层，只两件事：ext4 权重路径、32K 上下文默认值 |
| `docker/ops.sh` | 435 | **容器侧统一入口**：up/down/stop/start/restart/status/check/logs/bench/models/tui/verify/fetch |
| `docker/requirements.lock` | 721 | `uv export --frozen` 生成、带哈希的依赖清单（镜像里 `pip --require-hashes` 安装） |
| `docker/chat_template.jinja` | 172 | 聊天模板（镜像里自带一份，避免依赖权重目录） |
| `config/base.env` | 48 | **调优唯一来源**：上下文/KV/并发/批大小/FA/FIT…，零绝对路径 |
| `config/docker.env` | 16 | 容器专属路径：`/models`、`/opt/llama/bin/llama-server`、`/var/log/dshinfer/...` |

## 五、评测 `eval/`（3 手写文件 / 377 行 + 跑批产物）

| 文件 | 行 | 说明 |
|---|---:|---|
| `cases.jsonl` | 26 | 26 个用例 / 14 类：事实、推理、数学、指令、JSON、代码执行、翻译、计数、拒答、长上下文、工具调用、文风 |
| `judge.py` | 145 | 九种判定：`contains_all/any`、`exact`、`regex`、`json`、`tool_call`、`no_tool_call`、`length_between`、`python_runs`（真跑代码） |
| `run_eval.py` | 206 | 异步跑批、温度 0、逐条记录、分类汇总、留存 `results/<stamp>.json` |

基线：**25/26（96%）**，唯一失败 `code-fizzbuzz-exec`（缩进 1 空格）。

## 六、文档 `docs/`（12 文件 / ≈2,450 行）+ 根目录

| 文件 | 行 | 内容 |
|---|---:|---|
| `perf.md` | 390 | 性能主报告：roofline、参数扫描、批处理、投机解码、量化、KV 统一池、前缀缓存、容器内复测、深度 vs 容量（`--fit` 真凶） |
| `docker.md` | 701 | 容器化全流程：分层、WSL2 + toolkit、显存预算、`ops.sh`、源码进 WSL、依赖锁定、构建验证记录 |
| `deploy.md` | 219 | **两形态部署契约**：三形态对照、配置单源与优先级、ops 命令对照、`verify_env.py`、清白性验证、上云清单、守卫测试 |
| `tui.md` | 276 | 终端面板：向导 10 步、7 视图、数据来源与降级、三主题、容器内跑法、P0 改造记录 |
| `bench.md` | 108 | 服务 vs 引擎四档压测、42% 反例、天花板分析、与 vLLM 定性对照 |
| `eval.md` | 103 | 评测集设计与判定优先级、fizzbuzz 案例研究、盲区 |
| `models.md` | 143 | 模型注册表、权重清单与 sha256、量化档取舍 |
| `ops.md` | 115 | 常驻与启动：`ops.ps1` 与 `ops.sh` 两个入口、看护语义、自愈实测、排障 |
| `dsh.md` | 118 | DSH 接入：兼容性证据、GUI 四字段、patch 写法、限制 |
| `trace.md` | 97 | 单请求 trace：打点语义、接口、局限 |
| `scope.md` | 79 | 五层分工、代码盘点、欠账、判断规则 |
| `inventory.md` | ~150 | 本文件 |

根目录：`README.md` 329（总览/运行/接口/配置/目录树）、`pyproject.toml` 43（依赖与入口）、
`.env.example` 21（本机路径模板）、`.gitignore` 28、`.dockerignore` 17、`uv.lock` 1,081（锁定，不计手写量）。

---

## 七、哪些"完善"，哪些"到此为止"

**服务层（第 3 层）：可用级完善。**
协议、流式、工具、usage、错误码、调度、观测、模型管理、看护、评测、trace、容器化、双形态一致性都有且被测过。

**推理层（第 2 层）：不是我们写的。**
`llama.cpp` 提供算子和 KV 管理，我们只暴露参数面（`n_ctx`/`n_parallel`/`kv_unified`/`flash_attn`/`n_batch`/`n_ubatch`/`cache_reuse`/`fit`）。
这是有意的分工，不是未完成项——要在这一层"完善"，等价于重写 vLLM。

**明确没做的**：鉴权/限流（面板与接口都无 key）、多租户隔离、CI、多卡调度、分布式、视觉塔与 MTP 头。

