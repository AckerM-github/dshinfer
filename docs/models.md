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

- **服务内**下载/删除模型（Ollama 的 `pull` / `rm`）——文件级的下载与校验已在 `scripts/fetch-model.sh` 里做（见下节）
- 多模型同时常驻 + 按显存预算调度（需要 16 GB 以上才有意义）
- `mmproj` 视觉投影一起加载（当前只加载文本侧）

---

# 权重文件清单与校验（部署侧）

> 上一节讲服务层能力（扫描/热切换）；这一节讲**文件本身从哪来、怎么确认没坏**。
> 权重实体不进 git、不进镜像（GitHub 单文件 100 MB 硬限制、二进制不可 diff、clone 一次要 10 GB），
> 进 git 的只有三样：`models/models.json`（清单）、`models/SHA256SUMS`（校验和）、`scripts/fetch-model.sh`（工具）。

## 1. 三个文件

| 文件 | 内容 | 进 git |
|---|---|---|
| `models/models.json` | 每个模型的 id、相对路径、字节数、sha256、role、直链（可空）、provenance | ✅ |
| `models/SHA256SUMS` | `sha256sum` 格式，`sha256sum -c` 直接可用（不依赖本脚本） | ✅ |
| `scripts/fetch-model.sh` | `--check` / `--download` / `--sums` / `--list` | ✅ |
| `~/models/*.gguf`、`D:\llm\models\gguf\*.gguf` | 权重本体 | ❌（`.gitignore` 里 `*.gguf`） |

**为什么清单是 JSON 不是 YAML**：容器和 WSL 里都没有 pyyaml，`python3 -c 'json.load'` 是标准库，
零依赖；YAML 会逼着镜像多装一个包，或者逼着脚本自己写半个解析器。

## 2. 三个模型

| id | 文件 | 字节 | role | sha256 前 16 |
|---|---|---:|---|---|
| `dsv4-9b-q4km` | `DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf` | 5,629,111,264 | production（默认） | `a692de9520e4c0bb` |
| `dsv4-9b-q3km` | `...Q3_K_M.gguf` | 4,623,526,880 | alternative | `a781c335a4208aec` |
| `qwen3.5-0.8b-q8-draft` | `draft/Qwen3.5-0.8B-Q8_0.gguf` | 811,843,840 | experiment | `0ad885ffd4bb022f` |

草稿模型刻意放在 `draft/` 子目录：`src/dshinfer/registry.py:84` 只 `glob("*.gguf")`（不递归），
所以它不会出现在 `/v1/models` 里被误加载；服务层也没有 `--model-draft` 支持，
它只给 `scripts/bench_spec.py` 直连引擎做投机解码实验用。

## 3. 用法

```bash
bash scripts/fetch-model.sh --list                      # 清单 + 目标目录
bash scripts/fetch-model.sh --check                     # 校验 $DSHINFER_MODELS_DIR 或 ~/models
bash scripts/fetch-model.sh --check --target /mnt/d/llm/models/gguf   # 校验 Windows 那份
bash scripts/fetch-model.sh --download                  # 只下清单里带 url 的，断点续传 + 校验
bash scripts/fetch-model.sh --sums                      # 改了清单就重生成 SHA256SUMS
```

退出码：`0` 全部通过 / `2` 用法或环境错 / `3` 缺文件 / `4` 大小不符 / `5` sha256 不符 /
`6` 清单无直链（需本地生成）或下载失败。

## 4. provenance：为什么现在没有可直下的 URL

2026-11-09 实测：基座 `xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic`
@ `e4bfa83c72896cbb4990c909967d6f27bb97bac4` **只发布 safetensors**
（`model-0000{1..4}-of-00004.safetensors` + `model-mtp.safetensors`，共 18.8 GB），**没有 GGUF**。
所以本机的 Q4_K_M / Q3_K_M 是本地用 `llama-quantize` 产出的，不存在"下载即得"的直链。
清单里因此把 `url` 留空，把可复现的关键信息写进 `provenance`：
`base_repo` / `base_rev` / `quantizer`（llama.cpp b12202）/ `outtype` / `quantize_cmd`。

要真正"从零复现"，需要一台内存 ≥ 24 GB 的机器：

1. 下基座 safetensors（18.8 GB）
2. `python convert_hf_to_gguf.py --outtype f16 <base_dir> --outfile model-f16.gguf`（约 18 GB）
3. `llama-quantize model-f16.gguf out-Q4_K_M.gguf Q4_K_M`

草稿模型 `Qwen/Qwen3.5-0.8B`（Q8_0）同理是本地量化。
**从别人手里拿到同名文件时**，用 `--check` 或 `sha256sum -c models/SHA256SUMS` 就能确认是不是同一份权重——
这正是清单存在的意义，比"我记得是这个文件"可靠。

## 5. 实测（2026-11-09 23:1x）

| 动作 | 结果 |
|---|---|
| 草稿模型同步 `D:\llm\models\gguf\draft` → `~/models/draft`（9p → ext4） | 811,843,840 B，**9.0 s** |
| `--check --target ~/models` | 3/3 OK，exit 0 |
| `--check --target /mnt/d/llm/models/gguf` | 3/3 OK，exit 0 |
| `sha256sum -c models/SHA256SUMS`（纯 coreutils） | 3/3 OK |
| 空目录 | MISSING ×3，exit 3 |
| 截断到 1000 B | SIZE-MISMATCH，exit 4 |
| 同尺寸零填充（`truncate -s 811843840`） | SHA-MISMATCH，exit 5 |
| `--download` 遇无直链条目 | 打印 provenance，exit 6 |
| `--download` 遇已存在且通过校验 | SKIP，exit 0 |
| 未知参数 | exit 2 |

## 6. 一个 GGUF 元数据不自洽（2026-11-11，重要）

Q4_K_M 那份文件（`mradermacher` 转的）头部写着：

| 键 | 值 | 文件里实际 |
|---|---|---|
| `general.architecture` | `qwen35` | — |
| `qwen35.block_count` | **33** | 只有 `blk.0`–`blk.31` 共 32 层，`blk.32.*` 张量 **0 个** |
| `qwen35.nextn_predict_layers` | **1** | 没有 MTP 层张量 |
| `qwen35.attention.recurrent_layers` | 长度 **33** | `[1,1,1,0]`×8 之后多一个 `0` |

也就是说：**元数据在描述一个不存在的 MTP 层**。转换器把那一层的张量丢了，元数据没跟着改。

后果分版本：

| 引擎 | 行为 |
|---|---|
| 本仓库的 `llama-server`（build **12202** / `d6be06557`） | 容忍，照常加载运行 —— 所以两个月都没人发现 |
| 上游 **`7d7f0287`**（2026-11-08，llama-cpp-python 0.3.36 自带） | 按元数据逐层找张量 → `llama_model_load: error loading model: check_tensor_dims: tensor 'blk.32.attn_norm.weight' not found` |

0.3.35 同样失败，**不是绑定版本问题**。

### 修复

只改头部三个值，张量区一个字节不动：

```
qwen35.block_count              33 → 32
qwen35.nextn_predict_layers      1 → 0
qwen35.attention.recurrent_layers  截断到 32 个元素
```

脚本：`D:\dshworkspace\_wheel\gguf_rewrite.py`（第一版用"原地补丁"把数组头写坏，
加载器在读 KV 时死循环；改成整体字节流重排后正常）。
数据区起点仍是 `10970080`，427 个张量的 offset 全部不变。

### 验证（修复是无损的）

| 项 | 结果 |
|---|---|
| 进程内加载 | 3.7–4.0 s |
| 贪婪 32 token，进程内（修复件 + 新版 llama.cpp） vs llama-server（原件 + build 12202） | **逐字符完全相同** |
| 修复件启动 llama-server | 正常，41.5 tok/s |
| 原件备份 | `_orig/DeepSeek-...Q4_K_M.orig.gguf` |

**为什么要做**：MTP 层的权重本来就不在文件里，32 层的权重一个没少，所以"删掉不存在的层"只是让元数据说真话。
`registry.py` 读出来的 `n_layers` 现在是 32（原来是 33，也是错的）。

## 7. 两个实现坑（记下来省得再踩）

1. **别用 TAB 当 `read` 的字段分隔符**：TAB 属于 IFS 空白，`read` 会把连续分隔符折叠成一个，
   于是空的 `url` 字段被吞掉、`note` 顶到 `url` 位上（第一版就撞了，`--download` 把 note 当 URL 去 GET）。
   改用 `0x1f`（单元分隔符，非空白）后空字段保留。
2. **`registry.py` 只扫顶层**：往 `models/` 根目录扔一个草稿小模型，它会被 `/v1/models` 列出来，
   并可能在换模型时被当成一个可切换目标。子目录放对了就没有这个副作用。
