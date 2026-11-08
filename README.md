# dshinfer —— DeepSeek-V4-Pro-Qwen3.5-9B 推理服务层

一个**从零手写、不依赖 vLLM/TGI** 的 OpenAI 兼容推理服务层。目标不是造一个更快的推理引擎，
而是把「一个模型怎么变成线上服务」这件事的每一层都摊开：协议、并发、背压、流式、取消、观测。

```
HTTP (FastAPI)  ──►  Service（准入/排队/超时/取消/埋点）──►  Engine（协议）──►  Mock / llama.cpp
       ▲                                                                          │
       └────────────────────── SSE / JSON ◄── TokenEvent ◄────────────────────────┘
```

## 分层与职责

| 层 | 文件 | 只做一件事 | 不该做的事 |
|---|---|---|---|
| 协议 | `schemas.py` | 定义 OpenAI 兼容的请求体 | 不碰模型、不碰 GPU |
| 传输 | `api.py` | 路由、SSE 编码、错误映射 | 不写业务逻辑 |
| 服务 | `service.py` | 准入、排队、超时、取消、埋点、格式转换 | 不知道引擎是 llama.cpp 还是 mock |
| 引擎 | `engine/*.py` | 吐 `TokenEvent` 流 | 不知道 HTTP、不知道 JSON 字段名 |
| 配置 | `config.py` | 一切可调参数，环境变量 `DSHINFER_*` | 不读文件、不连网 |
| 观测 | `metrics.py` / `logging.py` | Prometheus 指标 / 结构化日志 | — |

关键取舍：**引擎只吐「文本片段 + 结束原因 + 用量」**。这样流式与非流式共用一条事件流，
`complete_chat()` 就是「把流式结果拼起来」，两者行为天然一致，不会出现「流式对、非流式错」的经典 bug。

## 运行

```powershell
cd D:\llm\infer
uv sync                              # 首次：建 .venv 装依赖
uv run pytest -q                     # 7 个测试，ASGI 直连不占端口
uv run uvicorn dshinfer.api:app --port 8000
```

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s -X POST http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"max_tokens\":16}'
```

## 接口

| 路径 | 用途 |
|---|---|
| `POST /v1/chat/completions` | 对话补全，`stream=true` 走 SSE，支持 `stream_options.include_usage` |
| `GET /v1/models` | 列出模型（OpenAI 客户端启动时会调） |
| `GET /healthz` | 存活 + 引擎状态 |
| `GET /admin/stats` | 在飞请求数、排队数、并发上限、引擎健康 |
| `GET /admin/traces` | 最近 N 条请求的时间线摘要（新的在前） |
| `GET /admin/traces/{id}` | 单条请求的完整时间线：每个打点 + 派生 span |
| `GET /metrics` | Prometheus：请求数、令牌数、TTFT、端到端延迟、在飞、队列深度 |

请求头 `X-Request-Id` 可自带，响应头会回显；非流式响应还带 `Server-Timing: queue;dur=…, ttft;dur=…, total;dur=…`。

## 配置（环境变量前缀 `DSHINFER_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENGINE` | `mock` | `mock` 或 `llamacpp`（真模型） |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `MAX_CONCURRENCY` | `4` | 同时在飞的请求上限（必须 ≤ `N_PARALLEL`） |
| `QUEUE_LIMIT` | `64` | 排队上限，超了直接 429（背压） |
| `QUEUE_TIMEOUT_S` | `30` | 排队超时 |
| `REQUEST_TIMEOUT_S` | `180` | 单请求生成总超时 |
| `GGUF_PATH` | `D:\llm\models\gguf\...Q4_K_M.gguf` | 量化权重路径 |
| `LLAMA_SERVER_PATH` | `D:\llm\runtime\llama.cpp\llama-server.exe` | 引擎可执行文件 |
| `ENGINE_PORT` | `8124` | 引擎子进程监听端口 |
| `N_GPU_LAYERS` | `-1` | 全部层上 GPU |
| `N_CTX` | `65536` | **所有槽位共享**的上下文总额；配合 `KV_UNIFIED` 时单请求可用满 |
| `CACHE_TYPE_K` / `CACHE_TYPE_V` | `q8_0` / `q8_0` | KV 量化；q8 让上下文翻倍（32K→64K），PPL +0.09%，速度不变 |
| `N_PARALLEL` | `4` | 引擎槽位数（`-np`）；配合 `KV_UNIFIED` 时槽位共享上下文池 |
| `KV_UNIFIED` | `true` | 统一 KV 池：不按槽位预留，单请求可用满 `N_CTX`（llama.cpp 版的按需分配） |
| `N_BATCH` / `N_UBATCH` | `2048` / `1024` | 逻辑批 / 微批；`N_UBATCH=1024` 让预填充快约 10% |
| `FLASH_ATTN` | `true` | FlashAttention；关掉明显变慢也更费显存 |
| `CHAT_TEMPLATE_PATH` | `D:\llm\runtime\chat_template.jinja` | 模型自带模板 |
| `ENABLE_THINKING` | `false` | `true` 时思考过程走 `reasoning_content`，不混进正文 |
| `SLOT_PINNING` | `true` | 把槽位作为 `id_slot` 传给引擎，会话钉在同一槽位；多步对话命中率 0%→100% |
| `CACHE_REUSE` | `0` | 引擎的 `--cache-reuse N`；本模型 26/33 层是线性注意力，共享前缀无法复用（见 `docs/perf.md` §5.2） |
| `MOCK_TTFT_MS` / `MOCK_TOKENS_PER_SECOND` | `120` / `40` | 假引擎的延迟模型 |

## 已实现的服务层策略

- **背压**：`admit()` 先看排队长度，再看信号量；抢不到槽位就是 429，而不是把请求堆在显存边上。
- **取消**：客户端断流 → `CancelledError` → `finally` 里 `engine.abort(cid)`，不白算 token。
- **超时**：`asyncio.timeout()` 包住生成过程，超时返回 504。
- **一致**：流式/非流式同一事件流；`finish_reason` 区分 `stop`/`length`。
- **停止串**：引擎在切片边界判断，**停止串本身不会被吐给用户**。
- **观测**：TTFT 直方图、在飞请求 gauge、令牌计数器；日志是 structlog JSON。

## Phase 1：接真模型（已完成）

目标模型 `xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic`（9.41B 参数，Qwen3.5 混合架构：
33 层里 24 层线性注意力 + 8 层全注意力，另有 MTP 头和视觉塔）。8 GB 显存只能跑量化版。

**为什么用 `llama-server.exe` 而不是 llama-cpp-python**：PyPI 上的预编译轮子最新只到 0.3.4
（2025 年初），不认识 `qwen35` 这个架构（GGUF 元数据 `general.architecture = qwen35`）。
官方 `llama.cpp b12202` 已经支持，所以把 `llama-server` 当子进程黑盒，我们只负责 prompt 和 token 流。
这不是妥协：**引擎负责算，服务层负责调度**，本来就是 vLLM 之类系统的分层方式。

踩过的两个坑：

1. **CUDA 后端静默回退**：把 `llama-b12202-bin-win-cuda-12.4-x64.zip` 解压后直接跑，
   显存占用 0 MiB、速度只有 7 tok/s——原因是缺 `cudart64_12.dll` / `cublas64_12.dll` / `cublasLt64_12.dll`，
   ggml 找不到 CUDA 运行时就不声不响地用 CPU。补上 DLL 后显存 5.3 GB、速度 21–38 tok/s。
2. **模板要对齐**：从 GGUF 元数据里抠出 `tokenizer.chat_template`（jinja），用 `jinja2` 渲染，
   生成提示要带 `<|im_start|>assistant\n` 和 `<think>` 块；随手拼 ChatML 也能出字，但格式和训练时不一致。

实测（RTX 4060 Laptop 8 GB，Q4_K_M，8192 ctx × 4 slots）：

| 指标 | 数值 |
|---|---|
| 首次加载（含 mmap） | ~10 s |
| 显存占用 | 5,487 MiB / 8,188 MiB |
| 首字延迟 TTFT | 190 ms（19 token prompt） |
| 单流生成 | 21–38 tok/s |
| 6 并发（4 槽位） | 全部 200，总 3.39 s，聚合 ~72 tok/s |
| 非流式 35 token | 1.43 s |

```powershell
$env:DSHINFER_ENGINE="llamacpp"
uv run uvicorn dshinfer.api:app --port 8000   # 启动时会自动拉起 llama-server 子进程
```

## Phase 2：调度（已完成）

把「信号量先到先服务」换成 `scheduler.py` 里的四级策略。**为什么服务层要自己调度**：
引擎批的是 token 步，服务层批的是请求。请求这一层能做的策略比想象中多。

| 策略 | 解决什么 | 实现 |
|---|---|---|
| 优先级 | 离线批量把交互式请求堵在后面 | `X-Priority` 头，数值大的先派 |
| 老化 | 低优先级永远轮不到（饿死） | 每等 `aging_s` 秒等效优先级 +1 |
| 租户配额 | 一个用户开 50 个并发吃光槽位 | `per_tenant_inflight` / `per_tenant_queue` |
| 前缀亲和 | 系统提示词每次重算 | prompt 前 512 字符哈希 → 固定 `id_slot` |

「连续批处理」不在这一层——那是引擎的活（它得管 KV 页和 token 步）。服务层的对应物是
**准入 + 排队 + 亲和**，这三件事做对了，尾延迟就差好几倍。

### 实测：优先级到底救不救得了对话

场景：8 个批量任务先占满 4 个槽位（每个 120 token），1 秒后插入 3 个对话请求（每个 16 token）。

| | 对话等待均值 | 最大等待 | 端到端 |
|---|---|---|---|
| 全 priority=0（FIFO） | 7.37 s | 7.73 s | 7.76 s |
| 对话 priority=9 | **3.44 s** | 3.87 s | 3.91 s |

**2.1 倍改善**——而且这还是只有 4 个槽位的情况，槽位越少、批量任务越长，差距越大。

### 实测：前缀缓存没想象中好用

同一段 ~600 token 的系统提示词连发三次（只有末尾问题不同），走 `prompt_tokens_details.cached_tokens` 观察：

| 请求 | prompt_tokens | cached | 实际重算 |
|---|---|---|---|
| 第 1 次 | 629 | 0 | 629 |
| 第 2 次 | 629 | 117 | 512 |
| 第 3 次 | 629 | 117 | 512 |

**完全相同的 prompt** 才能吃到 607/611 的缓存；**前缀相同、结尾不同**只命中 117——
llama.cpp 按 batch 边界保留 KV，最后一个 batch 会整个重算。所以「前缀缓存」的收益
高度依赖 batch 大小和请求形态，不能想当然。

```powershell
# 带优先级的请求
curl.exe -s -X POST http://127.0.0.1:8000/v1/chat/completions `
  -H "X-Priority: 9" -H "X-Tenant: alice" `
  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"max_tokens\":16}'
```

## Phase 3：压测（已完成）

完整报告见 [`docs/bench.md`](docs/bench.md)。核心结论三条：

| 问题 | 答案 |
|---|---|
| 我们自己这一层吃掉多少性能？ | **1–2% 吞吐 + 20–35 ms TTFT**（对照 llama-server 原生 `/completion`） |
| 天花板在哪？ | 4 槽位饱和 ≈ 125 tok/s，8 并发吞吐不再涨；带宽墙估算 ≈ 278 tok/s，到位约 45% |
| 哪个默认值最危险？ | `per_tenant_inflight=2` 曾让并发 4 的吞吐**掉 42%**、TTFT 从 204 ms 涨到 1860 ms |

那个 42% 的坑值得单独说：单租户配额小于 `max_concurrency` 时，4 个槽位永远只用得上 2 个，
吞吐卡在「并发 2」、延迟却按「并发 4」排队。现在默认 0（不限制），多租户部署才显式设
`max_concurrency // 租户数` 并重新压尾延迟。

```powershell
uv run python scripts/bench_load.py   # 四档并发 × 服务/原生两个端点
```

## Phase 4：工具调用与 agent（已完成）

模型的 chat 模板里本来就带工具段（`<tools>` + `<tool_call><function=...><parameter=...>` 格式），
所以这一层要做的不是"教模型调工具"，而是**把它的 XML 翻译成 OpenAI 的 `tool_calls`**。

三处改动：

| 位置 | 干了什么 |
|---|---|
| `schemas.py` | 加 `tools` / `tool_choice` / `tool_call_id`；`tool_choice="none"` 就不把工具段拼进提示词 |
| `toolcall.py` | 解析 `<tool_call>` → OpenAI 结构；支持一轮多个调用、多行参数、数字还原、JSON 兜底；**工具调用前面的那句人话留在 content 里** |
| `service.py` | 带工具时先扣住最后 9 个字符不吐（`<tool_call` 可能横跨 chunk），解析出调用后 `finish_reason="tool_calls"` |

实测（RTX 4060，Q4_K_M）：

| 场景 | 结果 |
|---|---|
| 单工具提问 | 格式 6/6 合规，参数 `{"city":"杭州","unit":"c"}` 正确 |
| 一轮要调两个工具 | 输出两个 `<tool_call>` 块，解析出 2 个调用 |
| 不需要工具的问题 | 正常回答，不乱调 |
| 不给工具 | 正常回答（不会凭空发明调用） |
| 多步 agent（2 次天气 + 温差推理） | 2 步收敛，6.2 s / 657 token，答案"杭州更适合，温差 8°C" |
| 计算器路径 | 2 步，2.0 s |

`scripts/agent_demo.py` 是完整的最小 agent 循环：模型出调用 → 本地执行 → `role:"tool"` 回灌 → 模型出最终答案，
最多 6 步。`scripts/probe_tools.py` 是纯模型侧的格式探针（绕开服务层）。

**代价**：带工具的请求，正文会晚 9 个字符才吐出来（要确认不是 `<tool_call>` 的开头）。
流式仍可用，但 `tool_calls` 是在结束时一次性给一个完整 delta，不像 OpenAI 那样逐 token 增量。

## 测试

`tests/test_api.py` 七个用例：健康检查、模型列表、非流式、SSE 流式、停止串、队列满 429、指标暴露。
`tests/test_scheduler.py` 七个用例：优先级插队、老化防饿死、租户配额、队列满、排队超时、前缀亲和、槽位归还。
全部用 `httpx.ASGITransport` / 纯 asyncio，不起端口，2.6 秒跑完 24 个。

`tests/test_toolcall.py` 七个用例：单调用、正文保留、一轮多调用、多行与数字参数、JSON 兜底、无调用、strip。
`tests/test_api.py` 另加三个工具用例：非流式、流式、`tool_choice="none"`。

真模型的冒烟脚本在 `scripts/`：`probe_llamaserver.py`（直连引擎）、`smoke_service.py`（打服务）。

## 已知限制

- 没有连续批处理：`llama-server` 自己按槽位跑，我们只能在**请求级**排队；真做 continuous batching 得自己管 KV。
- 没有前缀缓存复用策略：只开了 `cache_prompt`，靠引擎自己算最长公共前缀。而且本模型
  26/33 层是线性注意力（recurrent），**跨请求共享前缀在架构上就不可复用**，不是配置问题（§5.2 有 A/B 证据）。
- 没有鉴权、没有多模型路由、没有 Prometheus 之外的追踪。
- `enable_thinking=true` 时思考过程会混在正文里（还没拆 `reasoning_content`）。

## 路线

- **Phase 0（完成）**：服务层骨架 + Mock 引擎 + 测试 + 观测。
- **Phase 1（完成）**：接 llama.cpp（GGUF Q4_K_M），真模型端到端跑通并测出 TTFT/吞吐。
- **Phase 2（完成）**：优先级队列 + 老化 + 租户配额 + 前缀亲和，实测对话等待 7.37s → 3.44s。
- **Phase 3（完成）**：压测报告（`docs/bench.md`）：服务层开销 1–2% 吞吐 / 20–35 ms TTFT，并揪出租户配额默认值导致的 42% 吞吐损失。
- **Phase 4（完成）**：工具调用（`tools` / `tool_choice` / `tool_calls` / `role:"tool"`）+ 最小 agent 循环，实测 2 步收敛。
- **Phase 5（完成）**：作为 DSH 的本地模型后端——`n_ctx` 提到 32768、单槽，`/v1/models`、流式、
  `tool_calls`、usage 全部对齐 OpenAI 协议；接入步骤见 [`docs/dsh.md`](docs/dsh.md)。
- **Phase 6（完成）**：模型管理（扫描 GGUF、热切换、孤儿进程清理）见 [`docs/models.md`](docs/models.md)。
- **Phase 7（进行中）**：性能工程，按 roofline 做测量驱动的优化，报告见 [`docs/perf.md`](docs/perf.md)。
  已定位：生成已在带宽墙 90%（43.4 / 48.4 tok/s），批处理是唯一大杠杆（4 序列聚合 143.7 tok/s），
  前缀缓存命中 99%（首字 951 → 190 ms），投机解码在本模型对上负收益。
- **Phase 8（完成）**：评测集（26 例 / 14 类 / 代码用例真执行）、流式开销剖析、常驻看护、单请求 trace。
  文档：评测 [`docs/eval.md`](docs/eval.md)、运维 [`docs/ops.md`](docs/ops.md)、trace [`docs/trace.md`](docs/trace.md)。
  注：跨请求前缀共享在这台机器上**不可得**（26/33 层是线性注意力，无 KV 可复用），见 `docs/perf.md` §5.2。

## 目录

```
D:\llm\infer
├─ pyproject.toml
├─ .env                引擎/上下文/批参数（重启后仍然生效）
├─ src\dshinfer\
│  ├─ api.py          路由 / SSE / 错误映射
│  ├─ service.py      准入、渲染、超时、取消、埋点
│  ├─ scheduler.py    优先级 / 老化 / 租户配额 / 前缀亲和
│  ├─ toolcall.py     工具调用解析（XML → OpenAI tool_calls）
│  ├─ trace.py         单请求时间线（环形缓冲 + 派生 span）
│  ├─ schemas.py      OpenAI 兼容请求体
│  ├─ config.py       Settings（DSHINFER_*）
│  ├─ gguf.py         GGUF 头部解析（架构/层数/训练上下文）
│  ├─ registry.py     模型注册表（扫描 + 热发现）
│  ├─ metrics.py      Prometheus
│  ├─ logging.py      structlog
│  ├─ main.py         uvicorn 入口
│  └─ engine\
│     ├─ base.py      Engine 协议 / TokenEvent / Usage
│     ├─ mock.py      假引擎（可控 TTFT 与速度）
│     ├─ manager.py   引擎生命周期 + 热切换
│     ├─ llamacpp_server.py  llama-server 子进程适配器
│     └─ __init__.py  build_engine()
├─ scripts\
│  ├─ probe_llamaserver.py  直连引擎的探针
│  ├─ smoke_service.py      打服务的端到端冒烟
│  ├─ bench_sched.py        调度策略对比实验
│  ├─ bench_load.py         四档并发 × 服务/原生 压测
│  ├─ bench_spec.py         投机解码五组配置对比
│  ├─ bench_cache_reuse.py  --cache-reuse 的前缀复用 A/B
│  ├─ probe_perf_service.py 多轮对话的 TTFT / 缓存命中
│  ├─ probe_prefix.py       并发/预热/串行三种前缀打法
│  ├─ profile_stream.py     服务层 vs 裸引擎的流式开销剖析
│  ├─ trace_one.py          发一条请求并画出时间线瀑布图
│  ├─ probe_tools.py        模型侧工具格式探针
│  ├─ agent_demo.py         最小 agent 循环（工具执行 + 回灌）
│  ├─ supervise.ps1         看护进程（手动拉起，不做开机自启）
│  ├─ start.ps1             拉起看护 + 等待健康
│  └─ stop.ps1              优雅停止 + 兜底清理
├─ eval\
│  ├─ cases.jsonl        26 个用例 / 14 类
│  ├─ judge.py           规则判定 + 代码真执行
│  └─ run_eval.py        跑批 + 分类汇总 + 存档
├─ docs\
│  ├─ bench.md           服务层压测报告
│  ├─ perf.md            性能工程报告（roofline / 批处理 / 投机 / 量化 / 架构限制）
│  ├─ eval.md            评测集、判定策略、基线
│  ├─ ops.md             常驻与启动
│  ├─ trace.md           单请求 trace
│  ├─ models.md          模型管理
│  └─ dsh.md             接入 DSH
└─ tests\
   ├─ test_api.py
   ├─ test_scheduler.py
   ├─ test_registry.py
   ├─ test_render.py
   ├─ test_judge.py
   ├─ test_trace.py
   └─ test_toolcall.py
```
