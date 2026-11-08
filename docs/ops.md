# 常驻与启动

一句话：**没有开机自启**。你手动执行 `scripts\start.ps1` 之后，一个看护进程盯着服务，挂了就重启；`scripts\stop.ps1` 才会让它停。

## 三个脚本

| 脚本 | 作用 | 关键行为 |
|---|---|---|
| `scripts\start.ps1` | 手动拉起 | 已有实例在跑就直接返回（不双开）；用 Windows PowerShell 5.1 起看护进程；最多轮询 180 s 等 `/healthz` |
| `scripts\stop.ps1` | 全部停掉 | 先写 `stop.flag` 让看护进程优雅退出，再 `taskkill /T` 收尾，最后兜底清理端口占用与遗留 `llama-server` |
| `scripts\supervise.ps1` | 看护本体（被 start 调用） | 每 5 s 探一次 `/healthz`，连续 3 次失败才重启；启动后的加载窗口不计数 |

## 看护语义

```
start.ps1 ──► powershell supervise.ps1 ──► uv run uvicorn dshinfer.api:app ──► llama-server.exe
                     │  每 5 s 探 /healthz
                     │  连续 3 次失败 → 杀掉整棵进程树 → 重新拉起 → 阻塞等待健康
                     └─ 看到 stop.flag 就退出，不再拉起
```

三条容易踩的规则：

1. **加载期不算失败**。模型加载要 10 s 上下，冷启动更久。`Wait-Healthy` 会阻塞等到健康再回到探测循环；第一版没这么做，结果 20 s 后把正在加载的服务又杀了一遍（日志里连着两次 `starting service`）。
2. **重启杀整棵树**。`taskkill /T` 一定要带上，否则 `llama-server.exe` 会孤儿化，继续占着 5 GB 显存，下一次启动直接 OOM。
3. **停止用标志位**。直接 kill 看护进程的话，它启动时拉起的服务会留下；`stop.flag` 让它自己收尾。

## 文件位置

| 文件 | 用途 |
|---|---|
| `D:\llm\runtime\watchdog.log` | 看护日志（超过 5 MB 轮转成 `.1`） |
| `D:\llm\runtime\watchdog.pid` | 看护进程 pid，`start.ps1` 用它防双开 |
| `D:\llm\runtime\stop.flag` | 停止标志，`stop.ps1` 写入 |
| `D:\llm\runtime\service.log` / `service.err.log` | 服务自身的 stdout / stderr |
| `D:\llm\runtime\llama-server.log` | 引擎日志（由服务拉起时写入） |

## 常用操作

```powershell
cd D:\llm\infer

# 拉起（冷启动约 15 s 就绪）
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1

# 看状态
Invoke-RestMethod http://127.0.0.1:8000/healthz
Get-Content D:\llm\runtime\watchdog.log -Tail 20

# 停掉
.\scripts\stop.ps1
```

停止后再确认一次显存是否归零（孤儿最容易被忽略）：

```powershell
nvidia-smi --query-gpu=memory.used --format=csv
```

## 实测

| 场景 | 结果 |
|---|---|
| 冷启动 | 15 s 就绪（看护 2 s + 服务 3 s + 模型加载 10 s） |
| 杀掉监听 8000 的进程 | **34 s 自愈**（3 × 5 s 判定 + 重启 + 加载 11 s） |
| `stop.ps1` 后 | 看护退出、服务进程消失、`nvidia-smi` 归零 |
| 重复 `start.ps1` | 直接返回，不会起第二个实例 |

## 为什么不做开机自启

这是刻意的：这台机器同时用来学习和压测，常驻一个 5.4 GB 显存的推理进程会干扰别的实验（比如 `llama-bench` 的基线）。手动拉起 + 看护，既有"挂了自动恢复"，又不占着显存不放。

如果将来要做开机自启，正确做法是注册计划任务（`schtasks /create /sc onlogon`）指向 `start.ps1`，而不是把看护进程塞进启动目录——后者没有会话隔离，图形界面登录前后行为不一致。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `start.ps1` 立刻返回但 `/healthz` 不通 | 旧实例还活着（pid 文件命中） | `.\scripts\stop.ps1` 后重来 |
| 端口被占 `[Errno 10048]` | 上一次的 uvicorn 没死透 | `Get-NetTCPConnection -LocalPort 8000` 找 pid，`taskkill /PID <pid> /T /F` |
| 显存不释放 | `llama-server.exe` 孤儿 | `Get-Process llama-server \| Stop-Process -Force` |
| 看护日志反复 `starting service` | 加载窗口被当成失败 | 确认用的是 `Wait-Healthy` 版本 |
| `pwsh` 不是内部或外部命令 | 本机 PATH 没有 PowerShell 7 | 脚本已改用 `$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe` |
