# 文件清单（D:\llm\infer）

统计口径：不含 `.venv`、`__pycache__`、`.pytest_cache`、`eval/results`。
总计 **61 个文件 / 7,952 行**（`uv.lock` 988 行不计入手写量）。

---

## 一、服务层源码 `src/dshinfer/`（18 文件 / 2,363 行）

| 文件 | 行 | 职责 | 状态 |
|---|---:|---|---|
| `__init__.py` | 3 | 包标记 | 完成 |
| `api.py` | 263 | FastAPI 路由：8 个端点、SSE 生成器、OpenAI 风格错误、trace 注入、`X-Request-Id`/`Server-Timing` | 完成 |
| `config.py` | 116 | `Settings(BaseSettings)`，`DSHINFER_` 前缀；显存/并发/量化/思考/看护全部可配 | 完成 |
| `schemas.py` | 78 | OpenAI Chat Completions 协议模型（含 tools / tool_choice / stream_options） | 完成 |
| `service.py` | 604 | 核心编排：模板渲染、准入、流式分帧、usage、工具块解析、思考段剥离、trace 打点 | 完成，最大单文件 |
| `scheduler.py` | 217 | 优先级队列 + 老化 + 租户配额 + 槽位亲和 | 完成 |
| `toolcall.py` | 106 | `<tool_call>` → `tool_calls` 解析（多调用、参数强转、JSON 兜底） | 完成 |
| `trace.py` | 125 | 单请求打点与环形存储（512 条）、span 派生 | 完成 |
| `metrics.py` | 54 | Prometheus：请求数/令牌/队列等待/TTFT/在飞/排队深度 | 完成 |
| `logging.py` | 32 | structlog JSON 输出 | 完成 |
| `main.py` | 23 | uvicorn 启动入口 | 完成 |
| `registry.py` | 146 | 模型注册表：扫目录、缓存、默认模型、热发现 | 完成 |
| `gguf.py` | 142 | GGUF 头解析（架构/层数/上下文/维度），自写以便不依赖 llama.cpp | 完成 |
| `engine/__init__.py` | 28 | `build_engine()` 工厂（mock / llama-server） | 完成 |
| `engine/base.py` | 69 | `Engine` 协议 + `GenRequest`/`TokenEvent`/`Usage` | 完成 |
| `engine/llamacpp_server.py` | 251 | 子进程生命周期、启动参数、`/completion` 流解析、孤儿进程清理、`timings` 回读 | 完成 |
| `engine/manager.py` | 136 | 模型状态机 idle→loading→ready、互斥加载、热切换 | 完成 |
| `engine/mock.py` | 79 | 假引擎（无模型也能跑通全链路） | 完成 |

## 二、测试 `tests/`（8 文件 / 826 行，58 个用例全绿）

| 文件 | 行 | 覆盖 |
|---|---:|---|
| `test_api.py` | 200 | 健康检查、模型列表、非流式、流式、stop、队列满、指标、工具调用三态 |
| `test_scheduler.py` | 125 | 插队、老化、租户配额、队列满、超时、前缀亲和、归还 |
| `test_trace.py` | 139 | span 计算、打点幂等、环形容量、API 回显/自动 ID、404 |
| `test_registry.py` | 89 | 现场构造 GGUF 头、坏文件、扫描、默认 ID、热发现 |
| `test_judge.py` | 87 | 九种判定器（含真跑代码） |
| `test_toolcall.py` | 76 | 单/多调用、参数类型、JSON 兜底、正文保留 |
| `test_reasoning.py` | 69 | 思考段切分（跨 chunk、无标记、关闭透传、工具共存） |
| `test_render.py` | 41 | 模板渲染回归（thinking 开关、tool_calls 历史） |

## 三、脚本 `scripts/`（21 文件 / 2,079 行）

| 文件 | 行 | 用途 |
|---|---:|---|
| `supervise.ps1` | 124 | 看护进程：健康检查、失败阈值、重启、日志轮转、`stop.flag` |
| `start.ps1` | 44 | 拉起看护（防双开，等待 `/healthz` 就绪） |
| `stop.ps1` | 46 | 优雅停 → `taskkill /T` → 清端口与 llama-server 孤儿 |
| `bench_load.py` | 180 | 四档并发压测，服务 vs 直连引擎对照 |
| `bench_spec.py` | 152 | 投机解码（5 组配置、接受率） |
| `bench_sched.py` | 143 | 优先级调度 A/B（FIFO vs priority） |
| `bench_cache_reuse.py` | 137 | `--cache-reuse` 四种配置的前缀命中实验 |
| `profile_stream.py` | 143 | 流式开销剖析（服务 vs 引擎、pinning 开关） |
| `probe_prefix.py` | 150 | 跨请求前缀共享三种打法 |
| `probe_agent_cache*.py` | 185 | DSH 场景下 1/2/4/6 会话的缓存命中 |
| `probe_tools.py` | 141 | 模型工具调用格式合规性（6/6） |
| `probe_long_ctx.py` | 53 | 6K / 21K 长上下文预填充 |
| `probe_openai_sse.py` | 53 | 逐帧校验 SSE 结构 |
| `probe_perf_service.py` | 82 | 服务层三段对话 TTFT/缓存/吞吐 |
| `probe_llamaserver.py` | 66 | 直连引擎基准 |
| `agent_demo.py` | 137 | 最小 agent 循环（工具执行 + 回灌） |
| `trace_one.py` | 107 | 单请求 trace 拉取 + 瀑布图 |
| `smoke_service.py` | 80 | 冒烟：非流式/流式/指标 |

## 四、评测 `eval/`（3 文件 / 377 行）

| 文件 | 行 | 说明 |
|---|---:|---|
| `cases.jsonl` | 26 | 26 个用例 / 14 类：事实、推理、数学、指令、JSON、代码执行、翻译、计数、拒答、长上下文、工具调用、文风 |
| `judge.py` | 145 | 九种判定：`contains_all/any`、`exact`、`regex`、`json`、`tool_call`、`no_tool_call`、`length_between`、`python_runs`（真跑代码） |
| `run_eval.py` | 206 | 异步跑批、温度 0、逐条记录、分类汇总、留存 `results/<stamp>.json` |

基线：**25/26（96%）**，唯一失败 `code-fizzbuzz-exec`（缩进 1 空格）。

## 五、文档 `docs/`（8 文件 / 896 行）+ 根目录

| 文件 | 行 | 内容 |
|---|---:|---|
| `perf.md` | 268 | 性能主报告：roofline、参数扫描、批处理、投机解码、量化、KV 统一池、前缀缓存 |
| `bench.md` | 108 | 服务 vs 引擎四档压测、42% 反例、天花板分析、与 vLLM 定性对照 |
| `dsh.md` | 118 | DSH 接入：兼容性证据、GUI 四字段、patch 写法、限制 |
| `trace.md` | 97 | 单请求 trace：打点语义、接口、局限 |
| `eval.md` | 87 | 评测集设计与判定优先级、fizzbuzz 案例研究、盲区 |
| `ops.md` | 83 | 看护语义、启停、自愈实测、排障 |
| `scope.md` | 79 | 五层分工、代码盘点、欠账 9 项、判断规则 |
| `models.md` | 56 | 模型注册表：接口、实测、设计取舍 |
| `README.md` | 316 | 总览：架构、运行、接口、配置表、各阶段成果、目录树 |
| `pyproject.toml` | 37 | 依赖与入口（`dshinfer` 命令、pytest 配置） |
| `.env` | 17 | 当前生效配置（纯 ASCII 注释） |

---

## 六、哪些"完善"，哪些"到此为止"

**服务层（第 3 层）：可用级完善。**
协议、流式、工具、usage、错误码、调度、观测、模型管理、看护、评测、trace 都有且被测过。

**推理层（第 2 层）：不是我们写的。**
`llama.cpp` 提供算子和 KV 管理，我们只暴露参数面（`n_ctx`/`n_parallel`/`kv_unified`/`flash_attn`/`n_batch`/`n_ubatch`/`cache_reuse`）。
这是有意的分工，不是未完成项——要在这一层"完善"，等价于重写 vLLM。

**明确没做的**：鉴权/限流、多租户隔离、CI/Docker、Linux 验证、分布式、视觉塔与 MTP 头。
