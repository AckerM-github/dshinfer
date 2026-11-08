# 模型管理（Phase 6.1）

目标：把"改配置、重启、只能有一个模型"变成 Ollama 那样的"目录里有什么就能用什么"。

## 做了什么

| 能力 | 实现 | 文件 |
|---|---|---|
| 枚举模型 | 扫 `DSHINFER_MODELS_DIR` 下的 `*.gguf`，读文件头拿架构/层数/训练上下文 | `src/dshinfer/registry.py` |
| 读 GGUF 头 | 自己解析（不依赖 llama-cpp-python，那个绑定认不出 `qwen35`） | `src/dshinfer/gguf.py` |
| 热切换 | 卸载旧进程 → 起新进程 → 等 `/health`，全程不重启服务 | `src/dshinfer/engine/manager.py` |
| 孤儿清理 | 启动前按 exe 路径杀掉上次遗留的 `llama-server.exe`（否则 5 GB 显存被占，下次加载 OOM） | `engine/llamacpp_server.py::_kill_stale` |

## 接口

```bash
# 有什么模型（含大小、架构、训练上下文、是否已加载）
curl http://127.0.0.1:8000/v1/models

# 切换（正在处理请求时返回 409，避免打断流）
curl -X POST http://127.0.0.1:8000/admin/models/load -H 'Content-Type: application/json' -d '{"id":"deepseek-v4-pro-qwen3.5-9b"}'

# 卸载（释放显存，服务继续跑）
curl -X POST http://127.0.0.1:8000/admin/models/unload

# 管理视图：当前加载状态 + 全部模型
curl http://127.0.0.1:8000/admin/models
```

`/v1/models` 返回的每一条都带 `loaded` / `size_gb` / `arch` / `n_layers` / `n_ctx_train`，
客户端（DSH、Open WebUI、LiteLLM）可以直接拿 `id` 当模型名用。

## 实测

```
scan D:\llm\models\gguf → 138 ms（冷，读 5.24 GB 文件的头）
                         0.5 ms（热，命中 mtime+size 缓存）
模型: deepseek-v4-pro-qwen3.5-9b  arch=qwen35  layers=33  ctx_train=262144  embd=4096  5.24 GB
首次加载 7.78 s → 卸载 → 热加载 5.87 s → 对话正常（1 + 1 = 2）
孤儿清理：启动前发现 2 个 llama-server 进程，杀 2 个，启动后只剩 1 个
```

## 设计取舍

- **只常驻一个模型**：8 GB 显存里 Q4 权重就 5.24 GB，两个模型必然 OOM。
  Ollama 在同样约束下也是"换模型就卸载旧的"，这里把它做成显式状态机，而不是隐式行为。
- **切换要显式**：不在请求里按 `model` 字段自动切换——那会让两个并发请求互相把对方的模型踢掉。
  请求里 `model` 与当前加载的不一致时返回 400，让调用方先调 `/admin/models/load`。
- **扫描不信缓存**：按 `(路径, mtime, 大小)` 缓存，但每次请求都 stat 一遍，
  所以"往目录里扔一个 .gguf"下一秒就能在列表里看到，不需要重启。

## 还没做

- 下载/删除模型（Ollama 的 `pull` / `rm`）
- 多模型同时常驻 + 按显存预算调度（需要 16 GB 以上才有意义）
- `mmproj` 视觉投影一起加载（当前只加载文本侧）
