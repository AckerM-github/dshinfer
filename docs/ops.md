# 常驻与启动

对外只有一个入口：**`scripts\ops.ps1`**（Windows 原生）与 **`docker/ops.sh`**（容器），两边子命令同名。
`up` 之后由看护进程盯着服务，挂了自动重启；`down` / `stop` 才会让它停。没有开机自启，原因见文末。

## 0. 两个入口

| 形态 | 入口 | 前置 |
|---|---|---|
| Windows 原生 | `powershell -ExecutionPolicy Bypass -File .\scripts\ops.ps1 up` | `uv` 环境就绪；权重在 `$env:DSHINFER_MODELS_DIR`（默认 `D:\llm\models\gguf`） |
| 容器（WSL / 云） | `bash docker/ops.sh up` | docker + nvidia-container-toolkit，见 [docs/docker.md](docker.md) §10 |

子命令两边完全同名（Windows 侧的是 PowerShell 实现，容器侧是 bash 实现）：

| 命令 | 作用 |
|---|---|
| `up [--build]` | 起服务并等到健康（Windows 侧交给 `start.ps1`，容器侧 compose up） |
| `down` / `stop` / `start` / `restart` | 停止 / 启动 / 重启（容器形态对应 compose 同名操作） |
| `status` | 健康、引擎、显存、端口一览 |
| `check` | 7 项体检：进程或容器、引擎 ready、`/v1/models`、权重 sha256、显存、磁盘 |
| `logs [N] [-f]` | 服务日志（容器形态加 `-e` 读引擎日志） |
| `bench` | 停服务 → 跑 llama-bench → 自动拉起 |
| `models` | 模型清单 + 权重校验 |
| `tui` | 终端监视器 |
| `verify` | 两形态配置一致性比对（17 个契约键） |
| `fetch` | 按 `models/models.json` 下载/校验权重 |

配置约定：**调优值只有一份 = `config/base.env`**，容器路径在 `config/docker.env`，宿主路径写在本机 `.env`
（模板 `.env.example`）；优先级 进程 env > `.env` > `base.env` > 代码里的平台默认。
`check` 看单机健康，`verify` 看两形态是否一致，用法与隔离约束见 [docs/deploy.md](deploy.md)。

## 1. 内部实现（被 ops.ps1 调用，一般不直接用）

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
| `D:\llm\runtime\llama-server.log` | 引擎日志（由服务拉起时写入；容器里是 `/var/log/dshinfer/llama-server.log`） |

## 常用操作

```powershell
cd D:\llm\infer

# 拉起（冷启动约 15 s 就绪）
powershell -ExecutionPolicy Bypass -File .\scripts\ops.ps1 up

# 看状态 / 体检
powershell -ExecutionPolicy Bypass -File .\scripts\ops.ps1 status
powershell -ExecutionPolicy Bypass -File .\scripts\ops.ps1 check
Get-Content D:\llm\runtime\watchdog.log -Tail 20

# 停掉（释放显存）
powershell -ExecutionPolicy Bypass -File .\scripts\ops.ps1 down
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
| `ops.ps1 down` 后 | 看护退出、服务进程消失、`nvidia-smi` 归零 |
| 重复 `ops.ps1 up` | 直接返回，不会起第二个实例 |

## 为什么不做开机自启

这是刻意的：这台机器同时用来学习和压测，常驻一个 5.4 GB 显存的推理进程会干扰别的实验（比如 `llama-bench` 的基线），
而且容器形态和原生形态共用同一张卡。手动拉起 + 看护，既有「挂了自动恢复」，又不占着显存不放。

如果将来要做开机自启，正确做法是注册计划任务（`schtasks /create /sc onlogon`）指向 `ops.ps1 up`，
而不是把看护进程塞进启动目录——后者没有会话隔离，图形界面登录前后行为不一致。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ops.ps1 up` 立刻返回但 `/healthz` 不通 | 旧实例还活着（pid 文件命中） | `ops.ps1 down` 后重来 |
| 端口被占 `[Errno 10048]` | 上一次的 uvicorn 没死透 | `Get-NetTCPConnection -LocalPort 8000` 找 pid，`taskkill /PID <pid> /T /F` |
| 显存不释放 | `llama-server.exe` 孤儿 | `Get-Process llama-server \| Stop-Process -Force` |
| 显存够但加载就 OOM | 容器与原生形态同时在跑 | 先 `ops.ps1 down` 或 `ops.sh down`，两种形态共享一张卡 |
| 看护日志反复 `starting service` | 加载窗口被当成失败 | 确认用的是 `Wait-Healthy` 版本 |
| `pwsh` 不是内部或外部命令 | 本机 PATH 没有 PowerShell 7 | 脚本已改用 `$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe` |

