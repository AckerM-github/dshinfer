# 一套源码，三种形态：部署、配置与隔离

> 目标：**同一份 `src/` + `pyproject.toml` + `uv.lock` + `docker/Dockerfile`**，
> 在三种机器上跑出同一套行为，并且谁都不许偷偷依赖另一方的路径。
>
> * 形态 A：Windows 原生（本机开发机，`D:\llm\...`）
> * 形态 B：Linux 容器（本机 WSL2 里的 docker，就是云端形态的模拟机）
> * 形态 C：云上 Linux + docker + nvidia-container-toolkit（与 B 同构）
>
> 判据只有一条：**A 的东西 B 不能依赖，反之亦然**。GPU 供给方式不同是允许的
> （那是硬件差异），但它被限制在 `docker/compose.wsl.yaml` 这一处。

## 1. 三种形态对照

| | A：Windows 原生 | B：本机容器 / 云 | 备注 |
|---|---|---|---|
| 服务层进程 | `.venv\Scripts\python.exe -m dshinfer.main` | `/opt/venv/bin/python -m dshinfer.main`（容器内） | 同一份 `src/` |
| 引擎 | 默认 `inproc`：进程内 `vendor/llama_cpp` → `llama.dll`（并发 1）；`ENGINE=llamacpp` 才拉 `D:\llm\runtime\llama.cpp\llama-server.exe` | `/opt/llama/bin/llama-server`（子进程，4 slot） | 引擎是接口，换实现不动服务层；容器里 `docker.env` 强制回 `llamacpp` |
| GPU 供给 | 宿主驱动 + `ggml-cuda.dll` | nvidia-container-toolkit 注入 `libcuda.so.1` | 唯一无法消除的差异，收敛在 compose 硬件段 |
| 权重 | `D:\llm\models\gguf` | `/models`（只读 bind 或卷） | 5.2 GB 不进镜像 |
| 日志 | `D:\llm\runtime\*.log` | `/var/log/dshinfer`（named volume） | |
| 运维入口 | `scripts/ops.ps1 <命令>` | `bash docker/ops.sh <命令>` | 子命令同名同义 |
| 配置来源 | `config/base.env` + 本机 `.env` | `config/base.env` + `config/docker.env`（烤进镜像） | 见第 2 节 |

## 2. 配置只有一份真相

三份文件，分工严格，**不要互相抄**：

| 文件 | 进 git | 内容 | 谁读它 |
|---|---|---|---|
| `config/base.env` | 是 | 调优参数：上下文、KV 量化、并发、批大小、`FIT`…**零绝对路径** | 容器：`env_file` + 镜像内 `/app/runtime/base.env`；原生：`config.py` 自动加载 |
| `config/docker.env` | 是 | 只在容器里有意义的路径：`/models`、`/opt/llama/bin/llama-server`、`/var/log/dshinfer/...` | 容器（`env_file` + 镜像内副本） |
| 本机 `.env` | 否（`.gitignore`） | 本机路径：`D:\llm\models\gguf`、`D:\llm\runtime\llama.cpp\llama-server.exe`… | `config.py` 自动加载（模板见 `.env.example`） |

生效优先级（高 → 低，pydantic-settings 的规则）：

```
进程环境变量  >  本机 .env  >  config/base.env  >  config.py 里的平台默认值
```

* **平台默认值**写在 `src/dshinfer/config.py` 的 `_default_*()` 里：`os.name == "nt"` 走 D 盘，
  其它平台走 `/opt/llama`、`/models`、`/var/log/dshinfer`（不可写就退到 `~/.local/state/dshinfer`）。
  所以「新克隆没有 .env」也能在两种机器上各自跑起来，不需要谁去改代码。
* 容器里的搜索顺序是 `/app/runtime/base.env` → `/app/runtime/docker.env` → 仓库 `config/base.env` → `.env`，
  后读到的覆盖先读到的；镜像里那两份是 `Dockerfile` 从同一个 `config/` 拷进去的，
  **不存在「镜像里一份、仓库里一份」的双写**。

### 2.1 加一个新开关该改哪里

1. 是**行为/调优**参数且与机器无关 → 写进 `config/base.env`（一个键一行）＋ 在 `config.py` 里加字段（给默认值）。
2. 是**容器专属路径** → 写进 `config/docker.env`。
3. 是**本机路径** → 只写 `.env.example` 与 `config.py` 的平台默认值，不要进 `base.env`。
4. 跑 `uv run pytest -q tests/test_isolation.py`：它会在你把这些值抄回 `docker/compose.yaml` 时立刻变红。

### 2.2 运行期自证：`GET /admin/config`

配置文件看起来一样，不代表跑起来一样（`.env` 优先级、覆盖层、镜像里烤的旧副本都能让它们漂移）。
服务层因此提供只读的 `GET /admin/config`，报**生效值**：

```bash
curl.exe -s http://127.0.0.1:8000/admin/config
docker exec dshinfer curl -s http://127.0.0.1:8000/admin/config
```

返回的契约键（跨机器必须一致）：

`engine / model_name / gguf_file / gguf_bytes / n_ctx / cache_type_k / cache_type_v /
n_parallel / max_concurrency / kv_unified / n_batch / n_ubatch / flash_attn /
slot_pinning / fit / n_gpu_layers / enable_thinking`

以及只做参考的 `platform`（`win32` / `linux`）。路径类字段**故意不报**：三种形态天生不同，
报出来只会制造假差异。

## 3. 两种运行器，同一套命令

```bash
# 容器 / 云
bash docker/ops.sh up --build      # 构建并后台启动，等到 /healthz 就绪
bash docker/ops.sh status
bash docker/ops.sh check           # 7 项体检，全绿 exit 0
bash docker/ops.sh logs 80 [-e] [-f]
bash docker/ops.sh bench [llama-bench 参数]
bash docker/ops.sh verify          # 两端配置一致性自检（第 4 节）
```

```powershell
# Windows 原生
scripts\ops.ps1 up
scripts\ops.ps1 status
scripts\ops.ps1 check
scripts\ops.ps1 logs 80 -Follow
scripts\ops.ps1 bench
scripts\ops.ps1 verify
```

| ops.sh | ops.ps1 | 说明 |
|---|---|---|
| `up [--build]` | `up` | 容器：`compose up`；原生：`start.ps1` + 后台监视 |
| `down` / `stop` / `start` / `restart` | 同名 | 语义一致：`down` 删容器保镜像与日志卷 |
| `status` | `status` | 端口 / `/healthz` / 引擎进程 / 显存 / revision |
| `check` | `check` | 编号体检，任一失败 exit 1 |
| `logs` | `logs` | `service` / `engine` / `watchdog`，`-f` 跟随 |
| `bench` | `bench` | 都走 `scripts/bench_load.py`，只换 `--service` 地址 |
| `models` | `models` | 权重清单 + `sha256` 校验（`scripts/fetch-model.sh --check`） |
| `tui` | `tui` | 终端面板；容器里跑是因为引擎 `8124` 只绑容器回环 |
| `verify` | `verify` | 见第 4 节 |

## 4. 两端一致性自检（`scripts/verify_env.py`）

```bash
# 一次性：容器内采一份、宿主再采一份、逐键比对
bash docker/ops.sh verify
# 产物：.verify/host-<时间戳>.json、.verify/container-<时间戳>.json
```

手动跨机器比对（比如本机 ↔ 云）时，JSON 可以直接拷：

```bash
python3 scripts/verify_env.py --base http://127.0.0.1:8000 --json-out a.json
python3 scripts/verify_env.py --compare a.json b.json     # 0 一致 / 1 有差异 / 2 采集失败
```

脚本只用标准库 `urllib`，所以容器里的 `/opt/venv/bin/python` 与宿主裸 `python3` 都能跑，
不需要装任何依赖。

注意在 WSL 单机上，`ops.sh verify` 的「宿主侧」是走 `127.0.0.1:8000` 端口映射采的，
拿到的仍是同一个容器 —— 它证明的是「同一份配置经两条路径读出来完全一样」。
要真正跨形态比对（Windows 原生 ↔ 容器 ↔ 云），先在 Windows 上跑
`powershell -File scripts\ops.ps1 verify` 生成 `.verify\host-*.json`，再
`python3 scripts/verify_env.py --compare 两份文件`。
2026-11-10 实测（同一份源码，Windows 原生 64K ↔ Linux 容器 64K，各跑一次采集）：
`verify_env.py --compare` 结果 **17 个契约键完全一致**，退出码 0。
也就是说除了 GPU 供给方式，两端跑起来的配置契约没有任何差异。

## 5. 清白性验证：容器确实不依赖 Windows 路径

「代码里没有 `D:\`」是静态检查（`tests/test_isolation.py`）；要证明**构建与运行**也不需要 D 盘，
得在「看不见 `/mnt/d`」的命名空间里跑一遍：

```bash
# 1) 把 /mnt/d 从挂载命名空间里摘掉（只影响这条命令的子树）
sudo unshare -m --propagation private bash -c '
  umount /mnt/d && echo "/mnt/d 已卸载（D 盘不可见）"
  ls /mnt/d 2>&1 | head -n 2
  cd /home/linc/infer                    # ext4 上的 clone，不是 /mnt/d
  bash docker/ops.sh down
  bash docker/ops.sh up --build
  bash docker/ops.sh check
'
```

期望：`ls /mnt/d` 报 `No such file or directory`，而 `up --build` + `check` 全绿。
只要这条能过，说明镜像、`compose.yaml`、`config/*.env`、启动脚本都不依赖 Windows 侧的任何东西。

## 6. 上云清单

1. 宿主：驱动 ≥ 570.26（镜像里的引擎是 llama.cpp b12202 + CUDA 12.8 运行时），
   `nvidia-container-toolkit` 装好（`nvidia-ctk runtime configure --runtime=docker` + 重启 docker）。
2. 权重：`scripts/fetch-model.sh` 的 manifest 里 `url` 为空（本地量化产物），
   云上要先把 `*.gguf` 上传到机器的 `/srv/models` 之类目录，`MODELS_DIR` 指过去。
3. 起服务：
   ```bash
   MODELS_DIR=/srv/models bash docker/ops.sh up --build
   bash docker/ops.sh check
   ```
   `docker/compose.yaml` 的 GPU 段是标准 `deploy.resources.reservations.devices`，
   不需要（也不该）带 `docker/compose.wsl.yaml`——那个覆盖层只放本机的权重路径与 32K 上下文。
4. 反向代理 / API key：**当前没有鉴权**，面板与 `/admin/*` 一律裸奔，
   上公网必须先套反代 + 鉴权（见 README「已知限制」）。

## 7. 守住这些约束

`tests/test_isolation.py`（9 条，纯静态 + 一条平台默认值断言，秒级）：

| 测试 | 守的是什么 |
|---|---|
| `test_base_env_is_the_only_tuning_source` | 调优键只能出现在 `config/base.env`；`compose.yaml` 抄一遍就红 |
| `test_base_env_has_no_machine_paths` | `base.env` 里不许出现盘符、`/mnt`、任何以 `/` 开头的绝对路径 |
| `test_docker_env_only_mentions_container_paths` | `docker.env` 的绝对路径只能是 `/models`、`/opt/llama`、`/app`、`/var/log/dshinfer` |
| `test_compose_base_layer_has_no_host_paths` | 基础 compose 层零 Windows 路径、零反斜杠；权重占位符必须是中性的 `/models` |
| `test_wsl_overlay_only_holds_machine_specifics` | WSL 覆盖层只许放「权重真实目录 + 覆盖率」这类机器事实，不许挂 `/mnt` |
| `test_dockerfile_ships_the_contract_and_no_host_paths` | 镜像必须把 `config/base.env` 与 `config/docker.env` 拷进去 |
| `test_sources_do_not_hardcode_host_paths` | `src/` 除 `config.py` / `preset.py` 外不许出现宿主路径 |
| `test_env_example_only_overrides_paths` | `.env.example` 只放路径覆盖项，不再抄调优参数 |
| `test_settings_defaults_follow_platform` | 平台默认值真的分叉：Windows 出 D 盘，Linux 出 `/opt/llama` |

## 8. 故障判定表

| 现象 | 首查 | 判定 |
|---|---|---|
| 容器里 `n_ctx` 是 32768 而 `config/base.env` 写 65536 | `bash docker/ops.sh status` 的 compose 文件列表 | `compose.wsl.yaml` 覆盖层里写死了 `DSHINFER_N_CTX`（本机默认 32K 是为了 8 GB 显存留余量） |
| 原生服务的上下文不是 `base.env` 的值 | `curl /admin/config` | 本机 `.env` 优先级更高，检查里面有没有同名键 |
| 改了 `config/base.env` 但容器没变 | `docker exec dshinfer cat /app/runtime/base.env` | 镜像是构建时烤进去的：`ops.sh up --build` 重新构建 |
| 容器路径报错 `/models/... not found` | `docker inspect -f '{{json .Mounts}}' dshinfer` | `MODELS_DIR` 指错，或覆盖层没设；权重必须落在 ext4 上（9p 太慢） |
| `verify` 报 `gguf_bytes` 不一致 | 两份 JSON 的 `gguf_file` | 两端权重不是同一份（换了量化档），不是配置漂移 |

---

相关文档：`docs/docker.md`（镜像构建、WSL 安装、性能记录与 `--fit` 那个坑）、
`docs/perf.md`（并发与上下文实测）、`docs/ops.md`（运维细节）。

## 9. ops 脚本的两个坑（2026-11-10 实测踩到并修掉）

**ops.sh：容器不存在时静默退出。** `container_models_dir()` 里 `docker inspect` 失败，
配合 pipefail + `set -e`，`MODELS_DIR=$(...)` 赋值失败会让整个脚本一个字节都不打印就 exit 1。
现在函数末尾与调用处都带 `|| true`，`up --build` 在「先 down 再 up」的干净路径上才能跑通。

**ops.ps1：.ps1 必须纯 ASCII，或带 UTF-8 BOM。** powershell.exe 5.1 读无 BOM 的 .ps1 时按
ANSI(GBK) 解码，中文注释/字符串的字节会把引号吃掉，报 `Missing closing )` 或
`The string is missing the terminator`，而且行号指向别处。现在 ops.ps1 是 ASCII 内容 +
UTF-8 BOM，`tests/test_isolation.py::test_powershell_scripts_are_safe_for_ps51` 守着。

**ops.ps1：参数不要叫 `$Args`。** 它是自动变量；`function Invoke-Repo([string[]]$Args)` 会把
`Invoke-Repo @( ... )` 里除第一个之外的值全吞掉，python 于是没收到参数、直接掉进 REPL ——
表现为 verify 打印「wrote ...」但文件根本没生成。改名 `$ScriptArgs` 后正常。

顺带两处：`Get-Content models.json -Raw` 要加 `-Encoding UTF8`（文件含中文，GBK 解码会喂给
ConvertFrom-Json 一个坏串）；权重目录改读 `$env:DSHINFER_MODELS_DIR`，不再写死 D 盘。

