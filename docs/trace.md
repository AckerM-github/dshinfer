# 单请求 trace

指标回答「整体多快」，trace 回答「**这一条**慢在哪」。排队 3 秒和预填充 3 秒在
Prometheus 里都只是一个变慢的 TTFT 分位，处理方式却完全不同——所以要能看单条。

## 时间线

```
received ──rendered──admitted────────engine_start──first_token──last_token──done
   │          │         │                    │           │          │
   │          │         └─ queue  ────────────┘           │          │
   │          └─ render ─┘                                │          │
   └──────────────── total ──────────────────────────────────────────┘
                                  └─ ttft ─┘  └─ decode ─┘
```

| 打点 | 含义 | 慢在这里说明什么 |
|---|---|---|
| `received` | 请求进到路由 | — |
| `rendered` | 模板渲染完（含工具段拼接） | 模板/工具 schema 太大，或 jinja 走了 fallback |
| `admitted` | 调度器分到槽位 | 排队久 = 并发满了 / 配额卡住 / 低优先级被插队 |
| `engine_start` | 开始打引擎 | 与 `admitted` 的间隔 = 服务层到引擎的往返 |
| `first_token` | 第一个 token 出来 | 预填充慢 = 前缀没命中、上下文太长、`n_ubatch` 太小 |
| `last_token` | 生成结束 | `decode` 段 = 纯解码，受带宽限制 |
| `done` | 收尾（abort / 记账 / 释放槽位） | 与 `last_token` 有差 = 收尾开销 |

派生 span：`queue`、`render`、`ttft`、`decode`、`total`。

## 怎么用

```bash
cd D:\llm\infer
uv run python scripts\trace_one.py                       # 非流式
uv run python scripts\trace_one.py --stream              # 流式
uv run python scripts\trace_one.py --question "..." --max-tokens 64
```

输出（实测，非流式 48 token）：

```
  X-Request-Id : trace-1794150306
  Server-Timing: queue;dur=28.2, ttft;dur=493.6, total;dur=1918.1

  时间线（总 1918 ms，每格 ≈ 40.0 ms）
  received            0 ms  █                                                 +27 ms
  rendered           27 ms   █                                                +1 ms
  admitted           28 ms   █                                                +0 ms
  engine_start       28 ms   ████████████                                     +494 ms
  first_token       522 ms               ███████████████████████████████████  +1396 ms
  last_token       1918 ms                                                  █  +0 ms
  done             1918 ms                                                  █  +0 ms

  派生 span：queue=28.2ms  render=27.3ms  ttft=493.6ms  decode=1396.2ms  total=1918.1ms
  引擎自报   ：{'prompt': 467.8, 'decode': 1396.2}（prompt/decode，毫秒）
```

流式那条（11 token）的对照：`queue=0.4ms  ttft=359.3ms  decode=323.7ms`——排队几乎为零，
时间全在预填充和解码上，说明是**单请求本身**慢，不是排队。

## 三个接口

| 方式 | 作用 |
|---|---|
| 请求头 `X-Request-Id` | 自带 id，响应头回显；不给就用服务端生成的 `chatcmpl-…` |
| 响应头 `Server-Timing` | `queue / ttft / total` 三个数，`curl -D -` 直接看 |
| `GET /admin/traces?limit=20` / `GET /admin/traces/{id}` | 摘要列表 / 单条详情（含每个打点的绝对偏移） |

```bash
curl -s -D - -o /dev/null http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-1' \
  -d '{"model":"deepseek-v4-pro-qwen3.5-9b","messages":[{"role":"user","content":"你好"}],"max_tokens":16}' \
  | Select-String 'X-Request-Id|Server-Timing'

curl -s http://127.0.0.1:8000/admin/traces/demo-1
```

## 引擎侧的两个数

`engine_ms.prompt` / `engine_ms.decode` 来自 llama-server 的 `timings`，比服务层测的
`ttft` 更干净：不含 HTTP 往返和第一帧编码。两者之差就是这个服务层引入的固定开销
（实测 20–30 ms）。

## 与指标的分工

| | Prometheus 指标 | trace |
|---|---|---|
| 粒度 | 全局分位 | 单条请求 |
| 保留 | 进程生命周期 | 最近 512 条（内存，重启即清） |
| 用途 | 告警、容量规划 | 排障、回归对比 |
| 成本 | 常数级 | 每条十几次 `perf_counter()`，可常开 |

## 局限

- **只在内存里**，重启清空；不做持久化与跨进程聚合（那需要 OTEL collector）。
- **不含引擎内部**：llama.cpp 的 kernel 级时间要 `nsys`/`rocprof`，这里只到 prefill/decode 两段。
- 每条请求的 trace 用 `chatcmpl-` 号索引，`/admin/traces` 返回的是摘要，详情要再拉一次。
- 流式被客户端中途断开时 `status=cancelled`，`decode` 段只有断开前的部分。
