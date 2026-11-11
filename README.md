# dshinfer —— DeepSeek-V4-Pro-Qwen3.5-9B 推理服务层

一个**从零手写、不依赖 vLLM/TGI** 的 OpenAI 兼容推理服务层。目标不是造一个更快的推理引擎，
而是把「一个模型怎么变成线上服务」这件事的每一层都摊开：协议、并发、背压、流式、取消、观测。

```
HTTP (FastAPI)  ──►  Service（准入/排队/超时/取消/埋点）──►  Engine（协议）──►  Mock / inproc（进程内 libllama）
       ▲                                                                          │
       └────────────────────── SSE / JSON ◄── TokenEvent ◄────────────────────────┘
```

两种引擎，同一套服务层：

| `ENGINE` | 实现 | 进程模型 | 并发 | 前缀缓存 | 用在 |
|---|---|---|---|---|---|
| `inproc`（默认） | `vendor/llama_cpp` → `llama.dll`；容器里是自建的 `dshinfer-engine` wheel | 同进程 | **1** | 只记得上一条 | 单人对话、要低延迟 |
| `mock` | 纯 Python 假引擎 | 同进程 | 按配置 | 无 | 测试、压测、CI |

> 曾经的 `llamacpp`（`llama-server.exe` 子进程、4 slot、跨会话前缀缓存）已于 2026-11-11 删除：
> 单用户场景用不上它的槽位与多模型路由，而多进程本身每请求多 20–30 ms、还要多养一套进程看护。
> 要回看那版实现：`git show 04fd0a3^:src/dshinfer/engine/llamacpp_server.py`。

### 引擎怎么分发

`vendor/llama_cpp` 是源码真相源；`packaging/` 把它和配套的原生库打成平台 wheel
`dshinfer-engine`（`0.3.36.post1`）。装了这个 wheel，`import llama_cpp` 就有 GPU 能力，
不需要编译器、不需要 CUDA Toolkit：

```powershell
python packaging\stage_native.py --platform win_amd64 --from-dir D:\llm\runtime\llama-inproc\bin
python packaging\build_engine_wheel.py --platform win_amd64     # → packaging/dist/*.whl（1.58 GB）
python packaging\build_index.py                                 # → packaging/index/simple/（PEP 503）
```

原生库与绑定**同源**（来自同一个 llama-cpp-python 0.3.36 cu124 构建），所以是
「我们拥有分发、还不拥有编译」——这一点是踩出来的：拿 llama.cpp 官方 Linux 包（`GGML_BACKEND_DL=ON`）
拼 0.3.36 绑定，decode 会跳 `0x100` 崩溃。经过见 `packaging/engine/README.md`。

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
uv run pytest -q                     # 130 个用例，ASGI 直连不占端口
uv run uvicorn dshinfer.api:app --port 8000
```

日常运维走 `scripts\ops.ps1 <命令>`（Windows 原生）或 `bash docker/ops.sh <命令>`（容器/云），
两边子命令同名同义：`up / down / status / check / logs / bench / models / tui / verify`。
三种形态的完整对照与隔离约束见 [docs/deploy.md](docs/deploy.md)。

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
| `GET /admin/config` | 生效配置的契约键（上下文/KV/并发/权重尺寸），给两端一致性自检用 |
| `GET /admin/traces` | 最近 N 条请求的时间线摘要（新的在前） |
| `GET /admin/traces/{id}` | 单条请求的完整时间线：每个打点 + 派生 span |
| `GET /metrics` | Prometheus：请求数、令牌数、TTFT、端到端延迟、在飞、队列深度 |

请求头 `X-Request-Id` 可自带，响应头会回显；非流式响应还带 `Server-Timing: queue;dur=…, ttft;dur=…, total;dur=…`。

## 配置（环境变量前缀 `DSHINFER_`）

调优参数的默认值只有一处：`config/base.env`（容器与 Windows 原生都读它）。
容器专属路径在 `config/docker.env`，本机路径在 `.env`（模板 `.env.example`）。
优先级：进程环境变量 > 本机 `.env` > `config/base.env` > `config.py` 的平台默认值。
下表是字段说明；要改默认值请改 `config/base.env`，不要再抄到别处（有测试守着）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENGINE` | `inproc` | `inproc`（进程内 `vendor/llama_cpp`，并发 1）/ `mock`（测试） |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `MAX_CONCURRENCY` | `1` | 同时在飞的请求上限；进程内引擎一次只 decode 一个，>1 只是排在那把锁上 |
| `QUEUE_LIMIT` | `64` | 排队上限，超了直接 429（背压） |
| `QUEUE_TIMEOUT_S` | `30` | 排队超时 |
| `REQUEST_TIMEOUT_S` | `180` | 单请求生成总超时 |
| `GGUF_PATH` | `D:\llm\models\gguf\...Q4_K_M.gguf` | 量化权重路径 |
| `INPROC_LIB_DIR` | `D:\llm\runtime\llama-inproc\bin` | 进程内引擎的 DLL 目录（`ENGINE=inproc`），与 `vendor/llama_cpp` 同源 |
| `N_GPU_LAYERS` | `-1` | 全部层上 GPU |
| `N_CTX` | `65536` | 单个请求最多能用多少（进程内只有一个 context） |
| `CACHE_TYPE_K` / `CACHE_TYPE_V` | `q8_0` / `q8_0` | KV 量化；q8 让上下文翻倍（32K→64K），PPL +0.09%，速度不变 |
| `N_PARALLEL` | `1` | 引擎序列数；进程内引擎恒为 1，只用于校验与展示 |
| `N_BATCH` / `N_UBATCH` | `2048` / `1024` | 逻辑批 / 微批；`N_UBATCH=1024` 让预填充快约 10% |
| `FLASH_ATTN` | `true` | FlashAttention；关掉明显变慢也更费显存 |
| `CHAT_TEMPLATE_PATH` | `D:\llm\runtime\chat_template.jinja` | 模型自带模板 |
| `ENABLE_THINKING` | `false` | `true` 时思考过程走 `reasoning_content`，不混进正文 |
| `MOCK_TTFT_MS` / `MOCK_TOKENS_PER_SECOND` | `120` / `40` | 假引擎的延迟模型 |

## 已实现的服务层策略

- **背压**：`admit()` 先看排队长度，再看信号量；抢不到槽位就是 429，而不是把请求堆在显存边上。
- **取消**：客户端断流 → `CancelledError` → `finally` 里 `engine.abort(cid)`，不白算 token。
- **超时**：`asyncio.timeout()` 包住生成过程，超时返回 504。
- **一致**：流式/非流式同一事件流；`finish_reason` 区分 `stop`/`length`。
- **停止串**：引擎在切片边界判断，**停止串本身不会被吐给用户**。
- **观测**：TTFT 直方图、在飞请求 gauge、令牌计数器；日志是 structlog JSON。

## Phase 1：接真模型（已完成）

> 本节与 Phase 2/3 的数字都来自**已删除的 llamacpp 子进程形态**（4 槽位、`llama-server.exe`），
> 保留作对照基线；现役形态只有一个 context，这些槽位/并发数字不再可复现。

目标模型 `xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic`（9.41B 参数，Qwen3.5 混合架构：
33 层里 24 层线性注意力 + 8 层全注意力，另有 MTP 头和视觉塔）。8 GB 显存只能跑量化版。

**当初为什么用 `llama-server.exe` 子进程**：PyPI 上的预编译轮子当时只到 0.3.4（2025 年初），
不认识 `qwen35` 这个架构（GGUF 元数据 `general.architecture = qwen35`）；官方 `llama.cpp b12202`
已经支持，所以把 `llama-server` 当子进程黑盒，我们只负责 prompt 和 token 流。

**后来为什么又搬回进程内**：`llama-cpp-python` 0.3.36 的 cu124 wheel 自带 CUDA 版 `libllama`，
不装 Toolkit、不编译就能在本机跑；顺手发现我们的 GGUF 元数据本身有矛盾（`block_count=33`
但只有 32 层张量），修好后两个引擎的输出逐字符一致。于是引擎换成了进程内绑定：
省 20–30 ms/请求、KV 与 batch 全部可见，代价是并发退化到 1、C 层崩溃会带走整个服务。

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
uv run uvicorn dshinfer.api:app --port 8000   # 引擎在进程内加载（默认 inproc）
```

## Phase 2：调度（已完成）

把「信号量先到先服务」换成 `scheduler.py` 里的四级策略。

> 下表的「槽位」语境属于已删除的 llamacpp 形态；现在只剩一个 context，优先级/老化/配额仍在队列层生效，
> 只是“抢槽位”退化成了“抢那把 decode 锁”。

**为什么服务层要自己调度**：
引擎批的是 token 步，服务层批的是请求。请求这一层能做的策略比想象中多。

| 策略 | 解决什么 | 实现 |
|---|---|---|
| 优先级 | 离线批量把交互式请求堵在后面 | `X-Priority` 头，数值大的先派 |
| 老化 | 低优先级永远轮不到（饿死） | 每等 `aging_s` 秒等效优先级 +1 |
| 租户配额 | 一个用户开 50 个并发吃光槽位 | `per_tenant_inflight` / `per_tenant_queue` |
| 前缀亲和 | 系统提示词每次重算 | prompt 前 512 字符哈希（或 `X-Session-Id`）→ 固定会话键 |

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

- 没有连续批处理：进程内引擎一次只跑一个 decode，多请求只能排队；真做 continuous batching 得自己管
  `llama_batch` 与 KV 页（`llamacpp` 那版靠的是 llama-server 内部的槽位调度，已随删）。
- 跨请求前缀复用在**架构上**不可得：本模型 26/33 层是线性注意力（recurrent），没有可复用的 KV 页，
  不是配置问题（`docs/perf.md` §5.2 有 A/B 证据）；同一条会话内的续写仍能命中（19 token 里只重算 1 个）。
- 没有鉴权、没有多模型路由、没有 Prometheus 之外的追踪；C 层崩溃会带走整个服务（子进程版能隔离）。
- `ENABLE_THINKING=false` 时思考走 `reasoning_content`（`ThinkSplitter` 已实现）；打开后推理会占用
  `max_tokens`，长回答会被截断。

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
├─ config\
│  ├─ base.env         调优唯一来源（上下文 / KV / 并发 / 批大小…，两形态共用）
│  └─ docker.env       容器专属路径（/models、/var/log/dshinfer）
├─ .env                本机路径覆盖（不提交；调优默认值见 config/base.env）
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
│     ├─ inproc.py    进程内适配器（vendor/llama_cpp → llama.dll）
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
│  ├─ ops.ps1               统一入口 up/down/status/check/logs/bench/tui/verify（与 ops.sh 同名）
│  ├─ verify_env.py         两形态配置一致性比对（17 个契约键）
│  ├─ supervise.ps1         看护进程（由 ops.ps1 up 调用，不做开机自启）
│  ├─ start.ps1             拉起看护 + 等待健康（内部实现）
│  └─ stop.ps1              优雅停止 + 兜底清理（内部实现）
├─ eval\
│  ├─ cases.jsonl        26 个用例 / 14 类
│  ├─ judge.py           规则判定 + 代码真执行
│  └─ run_eval.py        跑批 + 分类汇总 + 存档
├─ docker\
│  ├─ Dockerfile        三阶段：engine（llama.cpp）/ wheel / runtime
│  ├─ compose.yaml      基础编排（GPU 预留 + 权重只读卷 + env_file）
│  ├─ compose.wsl.yaml  WSL 覆盖层（ext4 权重 + 32K 上下文）
│  ├─ ops.sh            容器侧统一入口（与 ops.ps1 同名子命令）
│  └─ requirements.lock 带哈希的固定依赖（pip --require-hashes）
├─ docs\
│  ├─ perf.md            性能工程报告（roofline / 批处理 / 投机 / 量化 / --fit）
│  ├─ deploy.md          两形态部署契约：配置单源、ops 对照、verify、上云
│  ├─ docker.md          容器化全流程（WSL2 + toolkit + 构建验证记录）
│  ├─ tui.md             终端面板：向导 / 7 视图 / 数据来源
│  ├─ bench.md           服务层压测报告
│  ├─ eval.md            评测集、判定策略、基线
│  ├─ ops.md             常驻与启动（ops.ps1 / ops.sh）
│  ├─ trace.md           单请求 trace
│  ├─ models.md          模型管理与权重清单
│  ├─ scope.md           五层分工与欠账
│  ├─ inventory.md       文件清单
│  └─ dsh.md             接入 DSH
└─ tests\
   ├─ test_api.py            接口 / SSE / 错误映射
   ├─ test_scheduler.py      调度与配额
   ├─ test_registry.py       模型注册与热发现
   ├─ test_gguf.py           GGUF 头解析
   ├─ test_render.py         模板渲染
   ├─ test_judge.py          评测判定
   ├─ test_trace.py          trace 环形缓冲
   ├─ test_toolcall.py       工具调用解析
   ├─ test_reasoning.py      思考模式
   ├─ test_isolation.py      两形态隔离守卫（配置单源 / 无宿主路径）
   ├─ test_tui_monitor.py    面板纯函数（相位 / 速率 / sparkline）
   ├─ test_tui_probe.py      采集层降级路径
   └─ test_tui_live.py       快照渲染
```
