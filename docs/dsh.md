# 把本地推理服务接进 DSH

DSH 的模型适配层是 `@deepseek-ai/dsh-llm-pi-ai`：它在 base 层是**休眠**挂载的（`providers` 为空字典，
选择器里一个模型都不多），只有在收到 `llm-pi-ai` 设置段里的 provider 配置时才注册路由。
所以接入不需要改 DSH 代码，只需要让它知道「有一个 OpenAI 兼容的本地端点」。

## 1. 服务端已经具备的（已实测）

| 能力 | 证据 |
|---|---|
| `GET /v1/models` | 返回 `deepseek-v4-pro-qwen3.5-9b`、`context_length: 32768` |
| 流式 SSE | `role` → `content`/`tool_calls` → `finish_reason` → `usage` → `[DONE]` |
| 工具调用 | `delta.tool_calls[{index,id,type,function:{name,arguments}}]`，`finish_reason="tool_calls"` |
| usage 帧 | `stream_options.include_usage=true` 时末尾发 `prompt_tokens/completion_tokens/prompt_tokens_details.cached_tokens` |
| 长上下文 | 21,022 token 的单条 prompt 正常返回（prefill 2,736 tok/s，7.7 s，显存 6,141 MiB） |
| 未知字段 | 请求模型 `extra="allow"`，`max_completion_tokens`、`parallel_tool_calls` 等原样接受 |

复现：

```powershell
cd D:\llm\infer
.\.venv\Scripts\python.exe scripts\probe_openai_sse.py    # 逐帧看 SSE
.\.venv\Scripts\python.exe scripts\probe_long_ctx.py --repeat 1400
```

## 2. 在 DSH 里加一条 provider

**推荐路径（GUI，改完即时生效，不用重启）**：DSH 设置 → **Models** 页 → **Custom model API**
（副标题：Connect a relay, a self-hosted server, or any other OpenAI-compatible endpoint），填：

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API protocol | `OpenAI Chat Completions` |
| API Key | 随便填一个非空 ASCII（本服务不校验） |
| Models | 点 **Fetch available models**，或手填 `deepseek-v4-pro-qwen3.5-9b` |
| 上下文窗口 / 最大输出 | `32768` / `2048` |

保存后模型选择器里会出现这个模型，选它开始对话。

**后备路径（手改配置）**：`C:\Users\74185\.dsh\profiles\desktop\cordis.patch.yml` 末尾追加，
先备份该文件：

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      local:
        displayName: Local llama.cpp
        api: openai-completions
        baseURL: http://127.0.0.1:8000/v1
        apiKeyEnv: DSH_LOCAL_LLM_KEY
        models:
          - id: deepseek-v4-pro-qwen3.5-9b
            name: DeepSeek-V4-Pro-Qwen3.5-9B Q4_K_M
            contextWindow: 32768
            maxTokens: 2048
```

注意：补丁是**按键浅覆盖**（`target[key] = value`），所以 `config` 一旦手写就整体替换该条目的
config——base 层这条本来就是空配置，因此是安全的；但如果你以后在 GUI 里又加过别的 provider，
手写会把它冲掉，改完请回 GUI 核对一遍。

## 3. 启动与常驻

```powershell
# 前台跑（Ctrl+C 退出）
cd D:\llm\infer
$env:DSHINFER_ENGINE='llamacpp'
.\.venv\Scripts\uvicorn.exe dshinfer.api:app --port 8000

# 后台常驻（当前就是这么起的，日志在 D:\llm\runtime\service.*.log）
Start-Process -FilePath 'D:\llm\infer\.venv\Scripts\uvicorn.exe' `
  -ArgumentList 'dshinfer.api:app','--port','8000' `
  -WorkingDirectory 'D:\llm\infer' -WindowStyle Hidden
```

llama-server 是服务自己拉起的子进程，日志 `D:\llm\runtime\llama-server.log`；
服务退出时会一并回收。

## 4. 现实限制（别指望它替代云端模型）

- **单槽 32K**：8 GB 显存里权重占 5.24 GB，KV 约 32 KB/token，32K 上下文 ≈ 1 GB。
- **并发**：`N_PARALLEL=4` + `--kv-unified`，4 条 slot 共享 32K 的 KV 池（不是每条 32K），
  显存 6.1 GB；单条长对话仍能用满 32K，但四条同时跑就分不到那么长。
- **速度**：单流 25–38 tok/s，prefill 约 2,000–2,700 tok/s。DSH 的系统提示 + 工具定义就有几千
  token，每轮都要重新 prefill（命中前缀缓存时能省一部分），体感明显慢于云端。

### 4.1 多步对话的缓存：串行很快，并发全靠运气

llama.cpp 的前缀缓存是**按槽位**保存的：同一段对话的下一步必须落回同一个 slot，才能只算新增的
那几十个 token。实测（`scripts/probe_agent_cache*.py`，6 会话 × 3 步）：

| 工况 | 每步重算的 token | 说明 |
|---|---|---|
| 串行（一次只跑一段对话） | **22–24**（新增部分） | 100% 命中，这正是 DSH 的正常用法 |
| 2 段对话并发 | 一路 22、另一路 175 | 只有一路的槽位缓存活下来 |
| 4–6 段对话并发 | 175–194（整段重算） | 缓存基本失效，等于每步冷启动 |

对策：客户端带上 **`X-Session-Id`**（同一段对话固定不变），服务端会把它作为槽位亲和键
（`scheduler._slot_hint`），至少保证路由不跳槽；但引擎在并发下仍可能挤掉缓存。
所以**别同时对 DSH 跑多个 agent 会话**，尤其别在一个会话等回复时又开另一个。

- **能力**：9B 蒸馏模型 + Q4 量化，工具调用格式对了（6/6 合规），但复杂多步编码任务容易跑偏。
  适合：单文件改动、解释代码、写小脚本、离线隐私场景。
- **上下文 32K 对 DSH 偏紧**：长会话需要定期 `/clear` 或新开会话；把 `n_ctx` 提到 65536 会在
  8 GB 卡上 OOM（KV 要 2 GB）。

## 5. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 模型选择器里没有本地模型 | provider 没注册成功 | 看服务 `/healthz`；确认 baseURL 以 `/v1` 结尾 |
| 请求 404 | baseURL 少写 `/v1` | 改成 `http://127.0.0.1:8000/v1` |
| 首个 token 卡很久 | 上下文接近满、prefill 慢 | 新开会话，或降低 DSH 里挂的工具数量 |
| `context length exceeded` | prompt 超过 32768 | 清理会话历史 |
| 服务起不来 | 8000 被占 | `Get-NetTCPConnection -LocalPort 8000` 找 pid 杀掉 |
