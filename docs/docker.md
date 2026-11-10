# 在 Docker 里部署大模型（dshinfer）

> 结论先说：**能，而且是业界通用做法。** 但在你这套项目里，"引擎"这个词指两个不同的东西，
> 先分清才能判断"改动量"和"是不是主流"。

## 1. 先拆清两层：谁是谁

| 层 | 你的东西 | 位置 | 是不是容器里该跑的东西 |
| --- | --- | --- | --- |
| 服务层（网关） | `dshinfer`：FastAPI + 准入/排队/超时/取消/埋点 + OpenAI 兼容协议 | `D:\llm\infer\src\dshinfer` | 是，你自研的这一层 |
| 推理引擎 | `llama-server.exe`（llama.cpp build 12202） | `D:\llm\runtime\llama.cpp\` | 是，被服务层当子进程拉起 |
| 权重 | `DeepSeek-V4-Pro-Qwen3.5-9B-...Q4_K_M.gguf`（5.24 GB） | `D:\llm\models\gguf\` | 挂卷，不进镜像 |

所以"infer 是 llm 的引擎"这句话要修正一下：`D:\llm\infer` 是**服务层**，
真正的引擎是它 spawn 的 llama-server。而这个设计恰好让容器化变简单——
`llamacpp_server.py` 本来就是把引擎当黑盒进程用，换平台只要换二进制路径，**src/ 一行不用改**。

## 2. 业界到底怎么摆

三种形态，业界都在用，但你得选一个：

**A. 单容器：引擎 + 模型加载 + 服务在同一镜像里**（本仓库 `docker/Dockerfile` 走这条）
- 谁在用：ollama、LocalAI、`ghcr.io/ggml-org/llama.cpp:server-cuda`、vLLM 官方镜像——**都是这个形态**。
- 优点：一条命令起来，无网络跳转开销，调试简单，8 GB 单卡最省事。
- 缺点：换模型/换引擎版本要重建镜像（权重挂卷就没这个问题），不能单独扩缩容。

**B. 双容器：引擎容器 + 你的网关容器**（生产/k8s 主流）
- 谁在用：vLLM/TGI/SGLang Deployment + LiteLLM / Envoy AI Gateway / Kong 做准入与多租户。
- 优点：引擎可以独立升级、换后端（llama.cpp ↔ vLLM）、网关可水平扩、镜像与权重解耦。
- 缺点：多一跳 HTTP（同机基本无损），你这套代码要加"外部引擎 URL"模式。

**C. 权重打进镜像层**：**几乎没人这么做**（除了一些超小模型做 demo）。
- 理由：镜像仓库不适合放 GB 级权重、每次换量化都要重推、层缓存爆炸。
- 标准做法：`-v /models:/models:ro` 挂载，或 k8s PVC / HF cache 卷。

你的项目现状 = A 直接可用；B 需要约 20 行改造（见第 6 节）。**两条路都不是野路子**，
但"生产上一个网关 + N 个引擎实例"最终都会走到 B。

## 3. 本仓库新增了什么（全部增量，不动 src/）

```
docker/Dockerfile            单容器镜像：ubuntu:24.04 + python3.12 + b12202 的 Linux CUDA 12.8 引擎（三阶段）
docker/requirements.lock     依赖锁：uv.lock 导出、44 个包全带 sha256，镜像按它 + --require-hashes 安装
docker/compose.yaml          GPU 预留 + 权重只读卷 + env 与 .env 逐条对齐 + healthcheck
docker/chat_template.jinja   从 D:\llm\runtime\chat_template.jinja 复制的 8908 B 模板
docker/compose.wsl.yaml      WSL2 主机配置：ext4 权重路径 + 32K 上下文默认值（GPU 走 nvidia 运行时，与云一致）
docker/ops.sh                运维入口：up / down / status / check / logs / bench / models / shell
.dockerignore                构建上下文排除（上下文是仓库根）
docs/docker.md               本文
models/models.json           权重清单：字节数 + sha256 + 来源（权重本体不进镜像也不进 git）
models/SHA256SUMS            校验和，`sha256sum -c` 直接可用
scripts/fetch-model.sh       权重校验/下载工具：--check / --download / --sums / --list
scripts/wsl-deploy.sh        git 单源部署：clone/pull → ops.sh up --build → 等 /healthz
```

镜像里做的 4 件关键事：

1. **同 build 号换平台**：不是随便拉个 llama.cpp 镜像，而是从 release 下
   `llama-b12202-bin-ubuntu-cuda-12.8-x64.tar.gz` + `cudart-llama-b12202-bin-ubuntu-cuda-12.8-x64.tar.gz`，
   和你 Windows 侧 exe 完全同 build（12202 / commit d6be06557）。**这样 `docs/perf.md` 的调优数据仍然有效**，
   否则换 build 后 PPL/吞吐对不上，等于把之前的基准清零。
2. `DSHINFER_HOST=0.0.0.0`：默认值 127.0.0.1 在容器里会导致端口映射打不通，这是最常见的坑。
3. 路径全部容器化：`/opt/llama/bin/llama-server`、`/models`、`/app/runtime/chat_template.jinja`、`/var/log/dshinfer`。
4. tini 当 PID 1：容器里 `os.name != "nt"`，`_kill_stale()` 的 powershell 分支不会执行，
   引擎是被 uvicorn 拉起的子进程——必须靠 tini 转发 SIGTERM，否则 `docker stop` 会留下孤儿进程。

## 4. 跑起来的步骤（含前置检查）

```powershell
# 0) 前置：本机现在没有 docker（docker 命令 CommandNotFoundException），先装 Docker Desktop
#    并确保 Settings → Resources → WSL Integration 勾上你的 Ubuntu 发行版
wsl -l -v                     # 已有 Ubuntu / kali-linux，均 Stopped，Docker Desktop 会自己起 WSL

# 1) 验证 GPU 在容器里可见（失败就是 nvidia-container-toolkit / 驱动层没通）
docker run --rm --gpus all ubuntu:24.04 nvidia-smi

# 2) 先停掉 Windows 侧正在跑的引擎，避免抢 8124 端口 / 占显存
D:\llm\infer\scripts\stop.ps1

# 3) 构建 + 启动
cd D:\llm\infer
docker compose -f docker/compose.yaml build
docker compose -f docker/compose.yaml up -d
docker compose -f docker/compose.yaml logs -f

# 4) 冒烟
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
docker exec dshinfer nvidia-smi   # 看显存（由 nvidia 运行时注入）
```

**没有 Docker 也想先验证"引擎进容器"这条路**：直接跑官方镜像，你的服务留在宿主机
（把 `.env` 里的 `DSHINFER_ENGINE` 用不上，因为服务层会自己 spawn —— 所以这条路只适合手动比对，
不能直接复用 `LlamaServerEngine`）：

```powershell
docker run --rm --gpus all -p 127.0.0.1:8124:8124 \
  -v D:/llm/models/gguf:/models:ro \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf \
  --host 0.0.0.0 --port 8124 -c 32768 -b 2048 -ub 1024 -fa on -ngl -1 -np 4 --no-webui --metrics --kv-unified -ctk q8_0 -ctv q8_0
```

（官方镜像的 CUDA 版本是 12.8 系列，build 号与 12202 不同，只能做"能跑通"验证，不能做数值对照。）

## 5. 坑清单（按踩到概率排序）

| # | 坑 | 现象 | 处理 |
| --- | --- | --- | --- |
| 1 | `host=127.0.0.1` | 容器起来但 `curl` 连接被拒绝 | 已设 `DSHINFER_HOST=0.0.0.0`；自己改配置时别忘了 |
| 2 | 权重从 `/mnt/d` 读（9p 文件系统） | 冷加载从 10 s 变成 30–90 s，mmap 随机读很慢 | 见下方"权重预热到卷"；或首次加载后 page cache 会缓和 |
| 3 | WSL2 **不隔离显存** | Windows 桌面/Edge/别的容器一起抢，8 GB 卡上 ~6.7 GB 需求极易 CUDA OOM | 加载前关掉吃显存的程序；先降 `N_CTX=32768`（实测 6.1 GB） |
| 4 | 镜像里缺 cudart | `error while loading shared libraries: libcudart.so.12` | Dockerfile 已把 cudart 包收进 `/opt/llama/lib` 并设 `LD_LIBRARY_PATH` |
| 5 | 驱动/cudart 版本 | `CUDA driver version is insufficient` | 驱动 610.60 足够新（支持 CUDA 12.8/13.x），真报错就换 `LLAMA_CUDA=13.5` 的资产 |
| 6 | `docker build` 拉 GitHub release 慢/超时 | 卡在 curl | 加 `--build-arg RELEASE_BASE=<镜像或内网地址>`；或预下载两个 tar.gz 用 `COPY` |
| 7 | 忘了 `scripts/stop.ps1` | 宿主机引擎还在，显存/端口冲突 | 先停宿主，再 up；容器 `engine_port` 是容器内 8124，不是映射端口 |
| 8 | 孤儿 llama-server | `docker stop` 后 `nvidia-smi` 还占显存 | 已用 tini + `stop_grace_period: 30s`；异常时 `docker kill` + `docker compose down` |
| 9 | 配置曾有两份（compose 手抄 .env） | 容器里的调优值和宿主悄悄不一致 | 已收敛成单源：调优值只在 `config/base.env`，容器路径在 `config/docker.env`，两边都通过 `env_file` / 镜像内副本读同一份；`bash docker/ops.sh verify` 逐键比对，见 `docs/deploy.md` |

**权重预热到 docker 卷**（解决坑 2，一次性 5.24 GB 拷贝）：

```powershell
docker volume create dshinfer-models
docker run --rm -v dshinfer-models:/models -v D:/llm/models/gguf:/src alpine \
  sh -c "cp /src/*.gguf /models/ && ls -lh /models"
# 然后 compose 里把 MODELS_DIR 换成命名卷挂载：
#   - dshinfer-models:/models:ro
```

## 6. 想改成双容器（生产形态）？约 20 行

`LlamaServerEngine` 现在硬编码 `base_url="http://127.0.0.1:{engine_port}"` 并且永远自己 spawn，
所以它**无法**指向外部引擎。最小改造：

```python
# src/dshinfer/config.py
engine_url: str = ""        # 非空 = 外部引擎模式（双容器 / k8s）；空 = 自己 spawn（现状）

# src/dshinfer/engine/llamacpp_server.py
async def start(self) -> None:
    if self._s.engine_url:                      # ① 外部模式：只建 client，不校验 GGUF、不 spawn
        self._client = httpx.AsyncClient(base_url=self._s.engine_url, timeout=...)
    else:
        ...                                     # ② 现状：校验路径 → _kill_stale → Popen → _wait_ready
```

配套 compose（引擎和网关各自一个镜像，网关不再需要 `--gpus`、也不需要 CUDA 运行库，镜像能瘦到 ~150 MB）：

```yaml
services:
  engine:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    command: ["-m","/models/DeepSeek-...Q4_K_M.gguf","--host","0.0.0.0","--port","8124","-c","65536",...]
    volumes: ["dshinfer-models:/models:ro"]
    deploy: {resources: {reservations: {devices: [{driver: nvidia, count: all, capabilities: [gpu]}]}}}
  gateway:
    build: {context: .., dockerfile: docker/Dockerfile.gateway}   # 不需要 /opt/llama 与 CUDA
    environment: [DSHINFER_ENGINE_URL=http://engine:8124]
    depends_on: [engine]
```

收益：引擎可换 vLLM/SGLang；网关可多副本；镜像从 ~1 GB（带 CUDA 运行库）降到 ~150 MB。
代价：多一次 HTTP 跳转（同机 <1 ms），并且要接受"服务层不再自己管引擎生命周期"的语义变化
（`_kill_stale`、`engine_start_timeout_s` 这些机制在外部模式下失效）。

## 7. 显存预算（8 GB 是最紧的约束，容器不改变它）

| 项 | 64K + q8_0 KV（现状） | 32K + q8_0 KV（保底） |
| --- | --- | --- |
| 权重 Q4_K_M | 5.24 GB | 5.24 GB |
| KV cache（np=4, kv_unified） | ~1.0 GB | ~0.5 GB |
| CUDA context / cublas workspace | ~0.3–0.5 GB | ~0.3–0.5 GB |
| **合计（Windows 实测 `nvidia-smi`）** | **6,727 / 8,188 MiB** | **~6.1 GB** |
| Windows 桌面/浏览器占用 | 0.5–1.2 GB（不可控） | 同上 |

结论：**64K 在裸机实测能跑，但容器化后余量并没有变多**，第一次上容器建议
`DSHINFER_N_CTX=32768` 跑通，再往上加。真要长期 64K，考虑换 `Q3_K_M`（4.31 GB）。

## 8. 验收清单

- [ ] `docker run --rm --gpus all ubuntu:24.04 nvidia-smi` 能看到 4060 Laptop
- [ ] `/healthz` 返回 `{"status":"ok",...}`，且 `engine` 字段到最后是 ready（不是 loading）
- [ ] `/v1/chat/completions` 流式能出字，首 token 时间与裸机同量级（差 <100 ms）
- [ ] `docker exec dshinfer nvidia-smi` 显存 ≈ 6.8 GB（64K）或 ≈ 6.0 GB（32K）
- [ ] 对照压测：改 `scripts/bench_load.py` 的 `SERVICE`/`ENGINE` 常量指向容器端口，
      与 `docs/perf.md` 的 1/2/4/8 并发档位逐项比（现成命令：`docker exec -i dshinfer /opt/venv/bin/python - < scripts/bench_load.py`）
- [ ] `docker compose down` 后宿主机 `nvidia-smi` 不残留 llama-server

## 9. 没验证的部分（照实说）

- 本机**没有 Docker**，以上镜像与 compose 均**未实际构建运行过**（WSL2 路线见第 10 节，环境已实测）——是按你的代码与
  llama.cpp b12202 发布资产写出来的，第一次 `build` 可能需要微调下载 URL 或库路径。
- llama.cpp 的 `ubuntu-cuda-*` 资产内部目录结构随发布变动，Dockerfile 用 `find` 定位
  `llama-server` 后摊平，仍建议首次构建后 `docker run --rm dshinfer:local ls -l /opt/llama/bin` 确认。
- WSL2 + Docker Desktop 下 8 GB 卡的实际可用显存，比你 `nvidia-smi` 看到的更少（桌面合成器要占），
  这是本项目最大的不确定项。
- 未测试多卡、未测试 `--no-mmap`（`LlamaServerEngine` 目前不传这个参数，权重走 9p 时值得加）。

## 10. 走 WSL2（本机已实测：可行，且是这台机器唯一现实路径）

### 10.1 本机实测环境

| 项 | 实测值 | 结论 |
| --- | --- | --- |
| WSL 版本 | 3.0.3.0，内核 6.18.54.1-microsoft-standard-WSL2 | 足够新，支持 `networkingMode=mirrored` |
| 发行版 | Ubuntu 26.04.1 LTS (resolute)，WSL2，PID 1 = systemd | `/etc/wsl.conf` 里 `systemd=true` 已开，dockerd 能当服务管 |
| 用户 | `linc`，**免密 sudo** | 装 docker 一条 apt 就够 |
| 磁盘 | `/dev/sdd` 1007 GB，已用 3.9 GB，**剩 952 GB**（ext4） | 权重和镜像放这里 |
| 内存 | **7.5 GiB + 2 GiB swap**（宿主机 15.5 GiB 的一半） | 5.24 GB 模型塞得下，但偏紧，建议调 `.wslconfig` |
| GPU 直通 | 2026-11-10 起用 `nvidia-container-toolkit 1.20.2`（NVIDIA 源按架构分目录，26.04 可装）：`--gpus all` 可用，容器内 `nvidia-smi` 看到 RTX 4060 Laptop 8188 MiB / driver 610.60 | **与云端同一套，不再是 WSL 专有鬼路** |
| docker | 未安装（无 docker / dockerd，也没有 Docker Desktop） | 但 `docker.io 29.1.3` + `docker-compose-v2 2.40.3` 就在已配好的阿里云源里 |
| 出网 | WSL 内直连 archive.ubuntu.com 200、github.com 200（~0.8 s） | 不用代理也能下 release；Windows 侧 127.0.0.1:7890 的 Clash 在 NAT 模式下**不会**被 WSL 继承（就是启动时那条 warning） |

### 10.2 装 docker：别装 Docker Desktop

WSL 里原生跑一个 dockerd 就够，少一层 LinuxKit 虚拟机、GPU 链路更短，也不占 Windows 侧资源：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
docker run --rm hello-world
sudo usermod -aG docker $USER   # 之后重开 shell 可免 sudo（不必须）
```

### 10.3 GPU 进容器：装 nvidia-container-toolkit（与云完全同一套）

**2026-11-10 起本机改用标准 NVIDIA Container Toolkit**。此前「26.04 装不了」的判断是错的：
NVIDIA 的 apt 仓库按 CPU 架构分目录（`stable/deb/amd64`），与发行版 codename 无关，Ubuntu 26.04 直接能装。
装完之后 `--gpus all` 在 WSL2 可用，容器里只需要注入宿主的 `libcuda.so.1`（引擎镜像自带 cudart / cublas，见下），
于是**本机 docker 与云端 Linux 的 GPU 通路一模一样**，`docker/compose.wsl.yaml` 里不再有任何 GPU 相关配置。

```bash
# 1) 加 NVIDIA 源（key + list，改写成 signed-by 形式）
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed "s|^deb https://|deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://|" \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit   # 实测 1.20.2-1

# 2) 注册 nvidia 运行时并重启 docker
sudo nvidia-ctk runtime configure --runtime=docker    # 写 /etc/docker/daemon.json 的 runtimes.nvidia
sudo systemctl restart docker
docker info | grep -A1 Runtimes   # → io.containerd.runc.v2  nvidia  runc

# 3) 验证：不再需要 --device /dev/dxg 或 -v /usr/lib/wsl
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

实测（2026-11-10，WSL 3.0.3 / Ubuntu 26.04.1 / docker 29.1.3）：

```
NVIDIA-SMI 610.47.03     KMD Version: 610.60     CUDA UMD Version: 13.3
|  0  NVIDIA GeForce RTX 4060 ...  |  N/A  53C  P0  18W / 110W | 5999MiB / 8188MiB |
容器内：/usr/lib/x86_64-linux-gnu/libcuda.so.1（187,984 B）、/dev/dxg；
        /usr/lib/wsl 并没有挂进容器（ls: cannot access）
```

为什么这样就够——依赖关系的实测：

```
# docker exec dshinfer ldd /opt/llama/bin/libggml-cuda.so
libcublas.so.12   => /opt/llama/lib/libcublas.so.12     # 镜像自带（引擎包里的）
libcublasLt.so.12 => /opt/llama/lib/libcublasLt.so.12   # 镜像自带
libcudart.so.12   => /opt/llama/lib/libcudart.so.12     # 镜像自带（Dockerfile 的 cudart 阶段）
libcuda.so.1      => /usr/lib/x86_64-linux-gnu/...      # 宿主/toolkit 注入的唯一一个
```

即：**CUDA 用户态库在镜像里，驱动在宿主，toolkit 只负责把驱动和 /dev/dxg 递进来**。
libnvidia-container 1.20.2 自带 WSL2 适配（WSL 的 `libcuda.so.1` 就是 `/usr/lib/wsl/lib` 里那份），
所以也不需要 `--device /dev/dxg`。**Dockerfile 一行都不用改**。

> 早期「手工 /dev/dxg + 挂整个 /usr/lib/wsl」的做法（2026-11-09 版本文档）在没装 toolkit 的机器上仍可用，
> 历史见 `git log -p -- docker/compose.wsl.yaml`；本仓库默认路径已不再使用它。

装了 toolkit 之后 [docker/compose.wsl.yaml](compose.wsl.yaml) 只剩两条有效配置：

```yaml
volumes:
  - ${MODELS_DIR:-/home/linc/models}:/models:ro   # WSL 用 ext4 权重；云上换 /srv/models
environment:
  DSHINFER_N_CTX: "32768"                          # 8 GB 显存的上下文默认值
```

```bash
cd ~/infer
docker compose -f docker/compose.yaml -f docker/compose.wsl.yaml up -d
```

### 10.4 权重必须放 ext4，不要挂 /mnt/d

`/mnt/d` 是 9p，mmap 随机读慢 5–10 倍（第 5 节坑 2）。952 GB 空间足够：

```bash
mkdir -p /home/linc/models
cp /mnt/d/llm/models/gguf/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf /home/linc/models/
# 可选：Q3_K_M 也拷一份（4.31 GB），64K 上下文爆显存时用
```

### 10.5 .wslconfig（可选，建议）

宿主 15.5 GiB，WSL 默认只给一半；5.24 GB 权重经 page cache 走 mmap，7.5 GiB 会紧张：

```ini
# C:\Users\74185\.wslconfig（当前不存在）
[wsl2]
memory=10GB
swap=8GB
autoMemoryReclaim=gradual
# 让 WSL 也能用 Windows 侧 127.0.0.1:7890 的 Clash（那条 localhost 代理 warning 会消失）
networkingMode=mirrored
```

改完 `wsl --shutdown` 生效。注意 `memory` 只管 **RAM**，**不管显存**——8 GB 显存全系统共享，
Windows 桌面/浏览器随时能把它吃掉一部分（实测这一分钟是 0 MiB 占用，是测 64K 的好时机）。

### 10.6 建议按这个顺序动手

1. `sudo apt install -y docker.io docker-compose-v2` → `docker run --rm hello-world`
2. 装 `nvidia-container-toolkit`（§10.3 三条命令）→ `docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi` 要看到 4060 Laptop
3. 拷权重到 `/home/linc/models`、拷仓库到 `~/infer`
4. `docker compose -f docker/compose.yaml -f docker/compose.wsl.yaml build`（首次下 188 MB + 594 MB）
5. **先用 `DSHINFER_N_CTX=32768` 跑通**，再试 65536
6. `curl http://127.0.0.1:8000/healthz` + `docker exec dshinfer nvidia-smi` 对显存

---

## 11. WSL2 实测结果（2026-11-09，容器已跑通）

实测环境：Ubuntu 26.04.1 LTS / WSL 3.0.3 / 内核 6.18.54.1-microsoft-standard-WSL2、
docker.io 29.1.3、容器内 llama.cpp build 12202 (CUDA 12.8)、RTX 4060 Laptop 8188 MiB、
权重 Q4_K_M、KV 全 q8_0、-fa on、-ngl -1。

### 11.1 镜像、加载、显存

| 项 | 实测值 |
|---|---|
| 镜像 | `dshinfer:local`，2.64 GB（disk）/ 1.04 GB（content） |
| 构建耗时 | 首次约 6 分钟（含下载 188 MB 引擎包 + 594 MB cudart） |
| 引擎冷加载 | 22.3 s（`/healthz` 里 `load_seconds`） |
| 显存 @ 64K | 6,181 / 8,188 MiB |
| 显存 @ 32K | 6,001 MiB |
| 显存 @ 8K | 5,497 MiB |
| 权重体积 | 5.24 GB GGUF，mmap 自 `/models`（ext4 卷） |

### 11.2 速度：容器代价只有 3–5%，真正的坑是上下文长度

容器内直接跑 `llama-bench`（与 docs/perf.md 第 2 节同命令、同参数）：

| 测试 | 容器内实测 | Windows 原生（perf.md） | 差 |
|---|---|---|---|
| pp512 | 2,077–2,337 t/s | 2,399 | −3% ～ −13% |
| tg128 | 41.3–42.5 t/s | 43.45 | −2% ～ −5% |

注意：**同一份二进制在容器里就能跑到 41+ t/s**，所以「容器 + nvidia 运行时」这条通路本身没有显著损失（WSL 与云一致）。

但把 `llama-server` 按生产参数起起来，单流生成速度是这样的（同一镜像、同一权重、`-np 4 --kv-unified -ctk/-ctv q8_0 -fa on`）：

| `-c` 上下文 | 单流生成 | 显存 |
|---|---|---|
| 8,192 | 39.3 t/s | 5,497 MiB |
| 16,384 | 40.1 t/s | 5,649 MiB |
| 32,768 | **40.4 t/s** | 6,001 MiB |
| 65,536 | **26.3 t/s** | 6,183 MiB |

读法（2026-11-10 归因后修正，详见 §16）：
- 8K～32K 之间速度基本平（都 ≈ 40 t/s，和 bench 的 41–42 一致），**32K 是免费的**；
- 表里 65,536 那行 26.3 t/s **不是上下文的代价**：是 llama.cpp 的 `--fit` 自动降配——它发现 8 GB 装不下 64K 的全量 offload，就把 34 层里的 4 层悄悄留在 CPU（日志 `offloaded 30/34 layers to GPU`），而 `/props` 里 `n_ctx` 依旧是 65536、没有任何报错。把 `--fit` 关掉后 64K 短上下文回到 **40.6 t/s**、显存 6,759 MiB。
- `-np`（slot 数）在 kv_unified 下不花钱：64K 下 `-np 1` / `-np 4` = 26.8 / 27.1 t/s（两个都被 fit 拖着）；关 fit 后都是 42。
- 64K 下换 f16 KV：20.5 t/s（更慢）；`-fa off` + 64K 直接 `failed to create context`（OOM）→ **q8_0 KV + FA 是 8 GB 上跑 64K 的唯一可行组合**。

### 11.3 那条 Flash Attention 警告怎么读

引擎日志里会出现：

```
W resolve_fused_ops: layer 3 is assigned to device CPU but Flash Attention over K/V rows
  is assigned to device CUDA0 (usually due to missing support)
W resolve_fused_ops: Flash Attention over K/V rows not supported, set to disabled
```

这是 build 12202 的 **fused-op 选择器**关掉了「FA over K/V rows」这一个融合算子，不是把 FA 整个关掉。
证据：64K + `-fa off` 直接 OOM，而 `-fa on` 能跑；16K/32K 下速度与 bench 的 `-fa 1` 相当。
结论：可以忽略，别为它去改参数。

### 11.4 WSL 会被自动关机：容器会「假死」

最后一个 `wsl.exe` 会话退出后约 30 秒，WSL 会关掉整个发行版（systemd 日志里是
`systemd-logind: The system will power off now!`），于是 dockerd 停、容器停。
下次任何 `wsl.exe` 命令会把发行版重新拉起，`docker.service`（`Restart=always`）
和容器（`restart: unless-stopped`）都会自动恢复，**但权重要重新加载 22 s**，
这段时间 `curl /healthz` 会连不上（我刚踩过：第一发冒烟测试就撞在重启窗口里，ConnectionResetError）。

要让它常驻，二选一：

```powershell
# A. 留一个会话挂着（最简单）
wsl.exe -d Ubuntu -- bash -lc "exec sleep infinity"
```

```ini
# B. C:\Users\<你>\.wslconfig
[wsl2]
vmIdleTimeout=86400000   # 24h 不自动关机（毫秒）
```

判断「现在是不是真的在跑」：`docker ps` 看到 `Up ... (healthy)` 且
`curl 127.0.0.1:8000/healthz` 返回 `"ready": true`。

### 11.5 8 GB 上的推荐配置（已写进 compose.wsl.yaml）

```yaml
environment:
  DSHINFER_N_CTX: "32768"   # 32K：40 t/s，显存 6.0 GB，留 2 GB 给 Windows 桌面
```

确实需要长上下文时再切 65536。**注意**：`--fit` 已默认关闭（`DSHINFER_FIT=false`，见 §16），所以切 64K 不再掉速；代价只有显存从 6.0 GB 涨到 6.76 GB（桌面还要占 0.5–1 GB，8 GB 卡会比较紧）：

```bash
DSHINFER_N_CTX=65536 docker compose -f docker/compose.yaml -f docker/compose.wsl.yaml up -d --force-recreate
```

### 11.6 冒烟结果（32K/64K 都通过）

```
TTFT 0.88 s / 首字到 40 token 用时 2.35 s（64K 配置）
中文解释：语义正确；代码题：缩进正确、可直接运行
/v1/models 同时列出 Q4_K_M（loaded）与 Q3_K_M（not loaded）
```

## 12. 日常运维：一条命令（docker/ops.sh）

前面 11 节里的命令够用但太碎。`docker/ops.sh` 把它们收成一个入口：

```bash
bash docker/ops.sh <命令> [参数]
```

自动适配：WSL2 下叠加 `docker/compose.wsl.yaml`，非 WSL 主机只用 `docker/compose.yaml`；
需要时自动给 docker 加 `sudo`（用 `sudo -n docker info` 探测）。三个可覆盖的环境变量：
`OPS_COMPOSE_FILES`（自定义 compose 文件串）、`MODELS_DIR`（默认取容器 `/models` 的
实际挂载源，取不到才退回 `~/models`）、`BENCH_MODEL`（默认 Q4_K_M 的容器内路径）。

| 命令 | 作用 |
| --- | --- |
| `up [--build]` | 构建（可选）+ 后台启动 + 轮询 `/healthz`（最多 300 s），就绪后打印容器状态 |
| `down` / `stop` / `start` / `restart` | 对应 compose 操作；`start` 不重建容器 |
| `status` | 容器 + `/healthz`（json 美化）+ 显存 + docker 磁盘占用 + 权重分区剩余 |
| `check` | 7 项体检：docker / 容器 healthy / 引擎 ready / `/v1/models` / 权重 sha256 / 显存 / 磁盘；有问题 exit 1 |
| `logs [N] [-e] [-f]` | 默认服务日志；`-e` 读容器内 `/var/log/dshinfer/llama-server.log`；`-f` 跟随 |
| `bench [args] [--stay-down]` | 停服务释放显存 → 一次性容器跑 `llama-bench` → 自动拉起服务（`--stay-down` 则停着） |
| `models` | 打印 `models/models.json` 清单 + 校验 `$MODELS_DIR` 里的文件 |
| `shell` | `docker exec -it dshinfer bash` |

`bench` 默认参数 `-p 512 -n 128 -ngl 99 -fa 1`，想跑别的直接跟参数，例如
`bash docker/ops.sh bench -p 2048 -n 128 -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0`。
它跑一次性容器而不是 `docker exec`，因为服务进程占着 6 GB 显存，不先停会 OOM；
需要 GPU 时统一 `--gpus all`（WSL 与云同一行，脚本已内置）；早期手工 `/dev/dxg` 直通已退役。

实测（2026-11-09，WSL2 Ubuntu 26.04 + docker 29.1.3，32K 上下文配置）：

```
up                      → 容器 recreate，18 s 等到 /healthz ready
bench -p 128 -n 32      → pp128 2054 t/s、tg32 42.06 t/s；跑完自动拉起，4 s 就绪
bench --stay-down ...   → 容器停在 Exited (143)，再 start 5 s 内 healthz 可用
check                   → 7 项全 OK，权重 3/3 sha256 通过，显存 5999/8188 MiB
bogus                   → 打印用法，exit 2
```

写这个脚本时踩到四个坑，记下来省得再犯：

1. **EXIT trap 里读不到函数的 local 变量** —— trap 在脚本退出时触发，此时函数已返回，
   bash 的动态作用域随之消失，`[ "$was_up" = 1 ]` 永远为假，服务被停掉就再没起来。
   改成顶层全局变量 `BENCH_WAS_UP/BENCH_STAY_DOWN/BENCH_RESTARTED` 后正常。
2. **`case` 模式里的空格** —— 用 `running\ healthy)` 这种反斜杠转义在多层引号传递中容易被吃掉，
   直接给整个模式加引号 `"running healthy")` 更稳。
3. **`/tmp` 固定文件名 + sudo = 假报错** —— `check` 原来把 `fetch-model.sh` 的输出重定向到
   `/tmp/ops-check-models.log`。这个文件先前被普通用户（linc）建过（0644），之后用
   `sudo bash docker/ops.sh check` 再写同一个路径时，内核的 `fs.protected_regular`
   （默认 2）会拒绝 root 带 `O_CREAT` 打开 sticky 目录里属于别人的已有文件，
   报 `Permission denied` —— 权重其实校验通过了，脚本却打 `[FAIL]`。
   改用 `mktemp` 拿唯一临时文件，跑完删除。
4. **`sudo` 下 `$HOME` 变成 `/root`** —— `MODELS_DIR` 原来默认 `$HOME/models`，
   于是 `sudo bash docker/ops.sh check` 去校验 `/root/models`，三个权重全 `MISSING`。
   现在先问 docker 要容器 `/models` 的实际挂载源
   （`d inspect -f '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}'`），
   拿不到才退回 `$HOME/models`；这样普通用户和 root 跑都指向同一目录。

## 13. 源码怎么进 WSL：git 单源 + scripts/wsl-deploy.sh

之前是手工 `cp -r /mnt/d/llm/infer ~/infer`（或 rsync）：改一次 D 盘的代码就要记得再拷一次，
两边逐渐漂移，容器里跑的到底是哪一版说不清。

现在：**代码只有一份事实来源 = git 仓库**，WSL 里的 `~/infer` 是 clone 出来的运行副本。

- 改代码：在 Windows 的 `D:\llm\infer` 里改 → commit（本地 remote 是同一份仓库，push 到 GitHub 只是备份）
- 部署：WSL 里 `bash scripts/wsl-deploy.sh` → `git pull --ff-only` → `docker/ops.sh up --build` → 等 `/healthz`

### 13.1 首次迁移：把 cp 副本换成 clone

```bash
# 先只同步代码，不动容器（会打印新目录的 HEAD）
bash /mnt/d/llm/infer/scripts/wsl-deploy.sh --fresh --no-up
# 确认没问题，再重建并重启
bash /mnt/d/llm/infer/scripts/wsl-deploy.sh --fresh
```

`--fresh` 会把现有 `~/infer` 改名成 `~/infer.bak.<时间戳>` 再 clone；旧目录里的 `.env`
会顺手带进新目录（容器不读它，只有 Windows 原生 `uv run` 用）。确认迁移成功后备份可删：
`rm -rf ~/infer.bak.*`。

### 13.2 日常

```bash
cd ~/infer
bash scripts/wsl-deploy.sh                 # 拉最新 + 重建 + 起容器 + 等 /healthz
bash scripts/wsl-deploy.sh --no-build      # 只重启（Python 代码没变）
bash scripts/wsl-deploy.sh --no-up         # 只同步代码，不动容器
bash scripts/wsl-deploy.sh --source git@github.com:AckerM-github/dshinfer.git --fresh
```

| 选项 | 作用 |
| --- | --- |
| `--fresh` | 备份现有目录后重新 clone（首次迁移 / 换来源用） |
| `--no-build` | 跳过镜像构建，只重启容器 |
| `--no-up` | 只同步代码 |
| `--source PATH` | 首次 clone 的来源，默认 `/mnt/d/llm/infer`（也可以是 GitHub 地址） |
| `--branch NAME` | 分支，默认 `main` |
| `--dir PATH` | 目标目录，默认 `$HOME/infer` |

### 13.3 要点与坑

1. **来源默认是本地路径** `/mnt/d/llm/infer`：零网络、秒级 clone，适合当前这台机器。
   想改成直接从 GitHub 拉，`--source git@github.com:AckerM-github/dshinfer.git --fresh`；
   但 WSL 里 GitHub 的 22 端口现在被挡（`Connection closed by 198.18.0.25 port 22`），HTTPS 每次要凭证，
   所以先走本地路径，Windows 侧再 push 到 GitHub 备份。
2. **`git pull --ff-only`**：本地有改动就直接失败，不会静默 merge。脚本同步后会列出未提交改动
   —— 这些文件会被打进镜像，等于「部署了一个不在 git 里的版本」，看到警告先处理。
3. **WSL 里 docker 要 sudo**：脚本自己探测（`sudo -n docker info`）并给 docker 调用加前缀。
   实测踩到的坑：汇总行忘了加 sudo，docker CLI 报 permission denied 时**还会往 stdout 吐一个空行**，
   于是输出变成「镜像 ：」换行「未知」；排查这类空行先想权限。
4. 每次 deploy 都是 `ops.sh up --build`；镜像重建后 compose 会 recreate 容器（重载权重 5–15 s）。
   只想重启进程用 `bash docker/ops.sh restart`，别用 deploy。
5. **脚本拉到自己的新版本必须 re-exec** —— 这是实测踩出来的：`git pull` 把 `wsl-deploy.sh`
   本身换掉以后，bash 仍按旧文件的字节偏移往下读，读到的是错位内容（表现为汇总行没有 sudo、
   输出串行成「镜像 ：」+「未知」）。脚本现在开头记下 `$0` 的 sha256 和 argv，同步完发现
   自己变了就 `exec env DEPLOY_REEXEC=1` 用新版本重跑一遍（`DEPLOY_REEXEC` 防死循环）。
6. **`docker image inspect --format` 的字段名**：`{{.CreatedSince}}` 对 image 不合法
   （那是 `docker images`/`docker ps` 的字段），模板报错又正好被 `2>/dev/null` 吃掉，
   于是这一行空着。改用 `docker images --format '{{.ID}} {{.CreatedSince}} {{.Size}}'`。

### 13.4 实测（2026-11-09，Ubuntu 26.04 / docker 29.1.3）

```
--fresh --no-up    → 备份 ~/infer.bak.20261109-234702，clone 932 KB 用时 <1 s，HEAD a781ae8
再跑一次            → Already up to date / 已是最新（幂等）
首次 build         → pip 层重建 59 s（.dockerignore 变过、缓存失效），之后基本全 CACHED
up + 等 /healthz   → 冷启动 14 s，重建后 6 s
全量 deploy        → recreate + 4–10 s 等到 /healthz，汇总行：代码版本 94faf5c /
                     镜像 f10d99d05a63 3 minutes ago 2.64GB / 容器 Up 4 seconds
health 等待        → /healthz 就绪时容器常还是 (health: starting)；脚本现在轮询
                     inspect 的 health 状态（≤30 s）后再打印，未转 healthy 则 warn
re-exec 验证       → clone 在 94faf5c 时给 origin 造一个只改脚本的临时 commit，
                     脚本打「部署脚本自身有新版本，用它重新执行一遍」后重跑一遍，
                     最终版本为临时 commit，无死循环
```

## 14. 终端面板（TUI）

浏览器面板（旧的 /dashboard + /admin/live）已移除：对"本机自己看盘"来说它没有意义，
而且等于把指标接口又对网络开了一个口子。现在的监视器是终端里的 Textual 应用，
直接读 NVML 与引擎 /metrics，自己不开任何端口、也不落盘：

```
uv run --extra tui python -m dshinfer.tui            # 向导 + 监视器
uv run --extra tui python -m dshinfer.tui --monitor  # 跳过向导直接看盘
```

容器里同样能跑（见 docs/tui.md 的 docker/ops.sh tui）。

## 15. 依赖锁定：uv.lock → docker/requirements.lock（构建可复现）

旧镜像用 `pip install ".[tui]"`，等价于「构建那一刻 PyPI 上是什么就装什么」——同一份代码隔两周重建，
fastapi / pydantic / textual 的版本就可能变了：出问题无法回溯，也答不出「这个镜像到底装了哪个版本」。

现在拆成三段，锁点在 `docker/Dockerfile`：

| 阶段 | 做什么 | 为什么 |
| --- | --- | --- |
| 阶段 1 `engine` | 下官方 llama.cpp b12202 的 Linux CUDA 12.8 包 | 和 Windows 侧同 build，perf.md 的调优数据继续有效 |
| 阶段 2 `wheel` | 固定 `hatchling==1.33.0`，把 `src/` 打成 `dshinfer-0.1.0-py3-none-any.whl` | 构建期工具不进运行时镜像；工程本体是纯 Python，产物跨机器一致 |
| 阶段 3 运行时 | `pip install --require-hashes -r docker/requirements.lock`，再 `pip install --no-deps` 装 wheel | 依赖完全由锁文件决定，版本被换 / 镜像源不同会直接构建失败 |

`docker/requirements.lock` 由 uv 从 `uv.lock` 导出（**44 个包 / 721 行，每个包都带 sha256**）：

```bash
uv export --frozen --no-dev --extra tui --format requirements.txt \
    --no-header --no-emit-project -o docker/requirements.lock
```

几个要点：

- **改了 `pyproject.toml` / `uv.lock` 必须重新导出**，否则构建会在 pip 解析时失败——这是故意留的闸门。
- 锁里不含 dev 依赖（pytest 等），但含 `[tui]` 的 textual/rich：**容器里一定能看盘**；
  代价是不再支持「构建时去掉 TUI」，原来那个 `--build-arg TUI_EXTRA=` 已删除。
- `--no-deps` 装 wheel：保证 pip 不会再顺手升级或降级任何依赖。
- 版本自证留在镜像里，排查时直接看：

```bash
docker exec dshinfer head -n 5 /app/runtime/pip-freeze.txt
docker exec dshinfer /opt/venv/bin/pip check
```

## 16. 构建/运行验证记录（2026-11-10）与 `--fit` 那个坑

### 16.1 镜像自证：确实按锁装的

| 项目 | 结果 |
|---|---|
| 构建 | `docker build -f docker/Dockerfile -t dshinfer:local .` → `edddf33417ed…`（加 `-fit` 逻辑后重建 `ef997c3b4db0`），2.66 GB |
| 锁一致性 | 容器内 `/app/runtime/requirements.lock` **md5 = d9180a821d3bbfd15c51f44f9eb2dfe5**（54,309 B），与宿主 `docker/requirements.lock` 完全一致 → `--require-hashes` 用的就是这份 |
| 版本快照 | `/app/runtime/pip-freeze.txt` 39 行：`annotated-doc==0.0.5`、`anyio==4.16.0`、`fastapi==0.162.2`、`textual==8.2.8` … |
| 引擎 | `/healthz` → `load_seconds 7–19`、`n_slots=4, n_ctx_slot=32768, kv_unified=true` |

### 16.2 容器内 TUI 验证（P0 的验收项）

- **headless**：`docker exec -i dshinfer /opt/venv/bin/python - < v_tui_container.py`，
  `app.run_test(size=(150,44))` → `errors=[]`；三套主题循环、`space` 冻结、1–7 视图切换、
  NVML 走 `nvml:linux` 读到 4060 / 8188 MiB，全部正常。
- **真 pty**（不是 mock）：

```bash
docker exec dshinfer sh -lc 'cd /app && timeout 8 script -qec   "/opt/venv/bin/python -m dshinfer.tui --monitor" /tmp/tui.ts >/dev/null 2>&1; echo EXIT=$?'
# EXIT=124（超时被杀 = 界面一直在跑），/tmp/tui.ts 57,548 B，grep -c Traceback = 0
```

### 16.3 `--fit` 自动降配：那个「64K 掉速 35%」的真凶

我们这份构建（b12202）**默认开启 `--fit`**：加载前先花 4.8 秒试算，把参数「塞进」显存。
塞不下时它会**少 offload 几层到 GPU**，而 `/props` 里 `n_ctx` 依旧、日志里也不报错——于是看起来就像「上下文一长就变慢」。

原生 Windows `llama-server`（Q4_K_M、q8_0 KV、`-np 4 --kv-unified`、短 prompt 117 token、生成 128）：

| `-c` | `--fit` 默认（on） | `-fit off` |
|---:|---:|---:|
| 32,768 | 42.00 t/s | 42.06 t/s |
| 40,960 | 41.91 t/s | — |
| 49,152 | 32.00 t/s | — |
| 57,344 | 26.90 t/s | — |
| 65,536 | **26.78 t/s** | **42.05 t/s** |

排除法（都是同一份权重、同一天）：

- **不是 slot 数**：64K 下 `-np 1` = 26.80、`-np 4` = 27.08；
- **不是显存压力**：全程 6.0–6.2 GB / 8.19 GB，没有溢出到宿主；换 4.31 GB 的 Q3_K_M 也只是 48.67（快是量化小，不是不掉级）；
- **不是深度**：短 prompt 117 token 就已经掉 36%，此时 KV 里只有 117 个 token；
- **是 `--fit`**：`-lv 5` 日志能看到它的搜索轨迹
  `offloaded 34/34 → 0/34 → 34/34 → 31/34 → 29/34 → 30/34（最终）`，
  也就是 4 层留在 CPU 上跑；关掉 `-fit` 后显存涨到 6,727 MiB、速度回到 42。
- **容器内一致**（同一份 Linux CUDA 构建、64K/q8/np4）：短 prompt **40.63 / 40.78 t/s**、
  27,098 token 深度 **29.75 t/s**（fit 打开时是 25.4–26.4 与 20.9–21.1）。`grep -c fit` 引擎日志 = 0。

处置：`config.py` 新增 `fit: bool = False`（环境变量 `DSHINFER_FIT`），engine 在其为假时追加 `-fit off`。
理由：`n_ctx` 是我们显式钉死的参数，装不下就该显式失败，而不是悄悄把 4 层踢回 CPU 再假装一切正常。
显存更小的卡若加载 OOM，把它设回 `true` 即可恢复旧的「自动降级」行为。

> 顺带修正本文档旧结论：§11.2 的「64K 掉 35%」、§11.5 的「切 64K 要付 −35% 速度」都是 `--fit` 的副作用，不是上下文本身的代价。
> 深度本身的代价见 `docs/perf.md` §14：64K 填满时约 −22%（线性，来自 KV 字节带宽）。

### 16.4 WSL 也改用 nvidia-container-toolkit：与云完全同构（2026-11-10）

原先的判断是「Ubuntu 26.04 源里没有 toolkit（NVIDIA 官方源只到 24.04 noble）」。**该结论已被推翻**：
NVIDIA 的 apt 仓库按架构分目录（`stable/deb/amd64`），与发行版 codename 无关，26.04 resolute 直接装成功。

| 项 | 结果 |
|---|---|
| 安装 | `nvidia-container-toolkit 1.20.2-1`（toolkit-base / libnvidia-container1 / libnvidia-container-tools），顺带启用 `nvidia-cdi-refresh.path` 与 `.service` |
| 运行时 | `nvidia-ctk runtime configure --runtime=docker` 写入 `/etc/docker/daemon.json` 的 runtimes.nvidia（path 指向 nvidia-container-runtime）；重启后 `docker info` → `Runtimes: io.containerd.runc.v2 nvidia runc` |
| `--gpus all` | ✅ `docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi` → 610.47.03 / KMD 610.60 / RTX 4060 / 5999 MiB |
| 注入内容 | 容器内多出 `/usr/lib/x86_64-linux-gnu/libcuda.so.1`（187,984 B）与 `/dev/dxg`；**没有挂 `/usr/lib/wsl`** |
| 依赖真相 | `ldd /opt/llama/bin/libggml-cuda.so`：`libcublas.so.12` / `libcublasLt.so.12` / `libcudart.so.12` 全部解析到镜像内 `/opt/llama/lib/`，只有 `libcuda.so.1` 来自宿主 → toolkit 只补驱动这一块，**Dockerfile 一行没改** |
| 配置收敛 | `docker/compose.wsl.yaml` 删掉 `devices` / `/usr/lib/wsl` 挂载 / `LD_LIBRARY_PATH` / `deploy: !reset`，只剩「权重目录 + 32K 默认值」；`ops.sh bench` 统一 `--gpus all` |

去掉所有手工挂载后的实测：`ops.sh up` **12 s** 就绪、32K 显存 **6,001 MiB**、`ops.sh check` **7/7 通过**；
64K（只用基础 compose，不带覆盖层）短 prompt **34–35 t/s**、显存 **6,737 MiB**、容器内 `nvidia-smi` 可用（运行时注入）。

