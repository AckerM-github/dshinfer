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
docker/Dockerfile            单容器镜像：ubuntu:24.04 + python3.12 + b12202 的 Linux CUDA 12.8 引擎
docker/compose.yaml          GPU 预留 + 权重只读卷 + env 与 .env 逐条对齐 + healthcheck
docker/chat_template.jinja   从 D:\llm\runtime\chat_template.jinja 复制的 8908 B 模板
docker/compose.wsl.yaml      WSL2 覆盖层：/dev/dxg 直通、ext4 权重路径、32K 上下文默认值
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
docker exec dshinfer nvidia-smi          # 看显存是否 6.7 GB 左右
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
| 9 | `.env` 没进镜像（故意的） | 容器里配置和宿主不一致 | 配置全部由 `docker/compose.yaml` 的 `environment` 给；改配置改 compose 或 `.env` + `env_file:` |

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
- [ ] `docker exec dshinfer nvidia-smi` 显存 ≈ 6.7 GB（64K）或 ≈ 6.1 GB（32K）
- [ ] 对照压测：改 `scripts/bench_load.py` 的 `SERVICE`/`ENGINE` 常量指向容器端口，
      与 `docs/perf.md` 的 1/2/4/8 并发档位逐项比
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
| GPU 直通 | `/dev/dxg` 存在；`/usr/lib/wsl/lib/libcuda.so.1` 存在；`nvidia-smi` 看到 RTX 4060 Laptop 8188 MiB / driver 610.60 / 当前占用 0 MiB | **最难的一步已经通了** |
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

### 10.3 GPU 进容器：用 /dev/dxg 直通，不用 --gpus all

26.04 源里没有 `nvidia-container-toolkit`（`apt-cache search nvidia-container` 为空，NVIDIA 源只挂到 24.04 noble），
所以标准 `--gpus all` 开箱不可用。WSL2 的 GPU 通路本来就是 `/dev/dxg` + 宿主 `/usr/lib/wsl`，
手工塞进容器效果等同（**必须挂整个 `/usr/lib/wsl`**：只挂 `lib/` 时 `nvidia-smi` 报
`couldn't communicate with the NVIDIA driver`，因为 NVML 还要读 `/usr/lib/wsl/drivers` 里的 Store 驱动副本）：

```bash
docker run --rm \
  --device /dev/dxg \
  -v /usr/lib/wsl:/usr/lib/wsl:ro \
  -e LD_LIBRARY_PATH=/usr/lib/wsl/lib \
  nvidia/cuda:12.8.0-base-ubuntu24.04 /usr/lib/wsl/lib/nvidia-smi -L
# 实测输出：GPU 0: NVIDIA GeForce RTX 4060 Laptop GPU (UUID: GPU-95e260be-...)
```

本仓库已经备好覆盖层 [docker/compose.wsl.yaml](compose.wsl.yaml)（清掉 nvidia 设备预留、加 /dev/dxg 与 wsl libs、权重指向 ext4）：

```bash
cp -r /mnt/d/llm/infer ~/infer && cd ~/infer
docker compose -f docker/compose.yaml -f docker/compose.wsl.yaml build
docker compose -f docker/compose.yaml -f docker/compose.wsl.yaml up -d
```

> `deploy: !reset null` 需要 Compose >= 2.24（26.04 源里是 2.40.3）。若你的版本不支持，
> 就把 `docker/compose.yaml` 里的 `deploy:` 整段注释掉再叠这个文件。

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
2. 验证 GPU 直通：上面那条 `nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi`，要看到 4060 Laptop
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

注意：**同一份二进制在容器里就能跑到 41+ t/s**，所以 WSL + 容器 + /dev/dxg 这条通路本身没有显著损失。

但把 `llama-server` 按生产参数起起来，单流生成速度是这样的（同一镜像、同一权重、`-np 4 --kv-unified -ctk/-ctv q8_0 -fa on`）：

| `-c` 上下文 | 单流生成 | 显存 |
|---|---|---|
| 8,192 | 39.3 t/s | 5,497 MiB |
| 16,384 | 40.1 t/s | 5,649 MiB |
| 32,768 | **40.4 t/s** | 6,001 MiB |
| 65,536 | **26.3 t/s** | 6,183 MiB |

读法：
- 8K～32K 之间速度基本平（都 ≈ 40 t/s，和 bench 的 41–42 一致），**32K 是免费的**；
- 一步跨到 64K，生成速度掉 35%（40.4 → 26.3），因为 64K 的 KV 让每步的读取量和图尺寸都上了一个台阶；
- 这不是容器造成的：perf.md 里 43 t/s 那组数字是 `llama-bench` 在默认小上下文下测的，换成 64K 的 server 在原生 Windows 上同样会掉。
- `-np`（slot 数）在 kv_unified 下**不花钱**：`-np 1` 与 `-np 4` 实测 26.24 与 26.30 t/s，64K 的代价与 slot 数无关。
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

确实需要长上下文时再切 65536，代价是生成速度 −35%：

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
`OPS_COMPOSE_FILES`（自定义 compose 文件串）、`MODELS_DIR`（默认 `$HOME/models`）、
`BENCH_MODEL`（默认 Q4_K_M 的容器内路径）。

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
WSL 下必须带 `-v /usr/lib/wsl:/usr/lib/wsl:ro --device /dev/dxg`（脚本已内置）。

实测（2026-11-09，WSL2 Ubuntu 26.04 + docker 29.1.3，32K 上下文配置）：

```
up                      → 容器 recreate，18 s 等到 /healthz ready
bench -p 128 -n 32      → pp128 2054 t/s、tg32 42.06 t/s；跑完自动拉起，4 s 就绪
bench --stay-down ...   → 容器停在 Exited (143)，再 start 5 s 内 healthz 可用
check                   → 7 项全 OK，权重 3/3 sha256 通过，显存 5999/8188 MiB
bogus                   → 打印用法，exit 2
```

写这个脚本时踩到两个 bash 坑，记下来省得再犯：

1. **EXIT trap 里读不到函数的 local 变量** —— trap 在脚本退出时触发，此时函数已返回，
   bash 的动态作用域随之消失，`[ "$was_up" = 1 ]` 永远为假，服务被停掉就再没起来。
   改成顶层全局变量 `BENCH_WAS_UP/BENCH_STAY_DOWN/BENCH_RESTARTED` 后正常。
2. **`case` 模式里的空格** —— 用 `running\ healthy)` 这种反斜杠转义在多层引号传递中容易被吃掉，
   直接给整个模式加引号 `"running healthy")` 更稳。

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

## 14. 内置监控面板：/dashboard

### 14.1 打开方式

容器已经把 8000 发布到宿主，Windows 浏览器直接开（不用进 WSL）：

```
http://127.0.0.1:8000/dashboard        # 页面本身，约 14 KB，零外部依赖
http://127.0.0.1:8000/admin/live       # 页面每 2 s 拉一次的快照 JSON，约 1.6 KB
```

两个路由都 `include_in_schema=False`，不会出现在 OpenAPI 文档里。

页面内容：

| 区域 | 内容 |
| --- | --- |
| 顶部 | 模型名、引擎本轮 n_ctx（悬停看模型上限 65,536）、引擎类型、服务已运行时长、就绪圆点 |
| 四个大卡 | 生成速度（单条 tok/s + 聚合 + 均值）、显存 used/total 进度条、GPU 利用率/温度/功耗/带宽、并发（在飞 + 排队/上限 + 引擎 processing/deferred） |
| 四条曲线 | 生成速度、显存、GPU 利用率、并发，每 2 s 一点，浏览器内存里留 120 点 ≈ 4 分钟 |
| 延迟表 | TTFT / 端到端 / 排队等待 的 p50、p95、均值、样本数（直方图桶内线性插值） |
| 累计表 | 服务层请求数与状态码、服务层令牌（输入/输出/命中缓存）、引擎 prompt 令牌与缓存命中率、调度事件 |
| 引擎表 | llama-server 的 7 个累计计数器（含投机解码草稿/接受） |
| 最近请求 | `/admin/traces?limit=8`：队列等待、TTFT、decode、总耗时、状态 |

### 14.2 三路数据源

| 来源 | 拿法 | 用途 |
| --- | --- | --- |
| 本进程 Prometheus registry | `metrics.REGISTRY.collect()`（`src/dshinfer/dashboard.py` 的 `service_metrics`） | 请求数/状态码/令牌/在飞/排队/调度事件，以及 TTFT、端到端、排队三个 Histogram 的分位数 |
| llama-server `/metrics` | `urllib` 抓 `http://127.0.0.1:8124/metrics` 后按文本解析（`engine_metrics`） | 引擎侧 prompt/生成令牌、processing/deferred、n_busy_slots |
| NVML | `ctypes.CDLL("libnvidia-ml.so.1")`（`gpu_info`） | 显存 used/total、GPU 利用率、显存带宽利用率、温度、功耗 |

引擎那 15 个指标全部带 `llamacpp:` 前缀，例如 `llamacpp:tokens_predicted_total` ——
冒号不是合法的 Prometheus 指标名，Prometheus 抓不了，所以这里自己按行解析。

### 14.3 /admin/live 的字段（实测 1,588 字节）

```json
{
  "ts": 1794240632.853, "uptime_s": 94.1,
  "model": {"id": "deepseek-v4-pro-qwen3.5-9b", "context_tokens": 32768,
            "max_context_tokens": 65536, "engine": "llamacpp",
            "max_concurrency": 4, "queue_limit": 64},
  "health": {"engine": "llama-server", "loading": false, "last_error": null,
             "load_seconds": 9.69, "ready": true, "upstream": {"status": "ok"}},
  "gpu": {"name": "NVIDIA GeForce RTX 4060 Laptop GPU", "total_mib": 8188.0,
          "used_mib": 6246.6, "free_mib": 1941.4, "util_pct": 64, "mem_util_pct": 68,
          "temp_c": 67, "power_w": 69.0, "power_limit_w": 110.0},
  "engine_metrics": {"tokens_predicted_total": 5236, "prompt_tokens_total": 1110},
  "engine_rate": {"instant": 24.8, "window": 28.23, "aggregate": 64.76},
  "service": {"inflight": 4.0, "queued": 0.0, "requests_total": 28.0,
              "requests_by_status": {"ok": 28.0},
              "tokens": {"input": 962.0, "output": 2649.0, "cached": 0.0},
              "scheduler_events": {"enqueue": 32.0, "dispatch": 32.0},
              "histograms": {"dshinfer_ttft_seconds": {"count": 30.0, "mean_s": 1.242,
                                                      "p50_s": 1.389, "p95_s": 3.875}}}
}
```

`engine_rate` 是服务端按引擎计数器算出来的三个速度（都是 tok/s）：

- `instant`：`Δtokens / Δ(引擎累计生成秒数)`，单条请求的 decode 速度（≈ llama-bench 的 tg）；
- `window`：上面那个值的 EWMA（0.6 权重），给大卡显示用；
- `aggregate`：`Δtokens / Δ墙钟`，整个服务的聚合吞吐，并发时这才是"显卡在产出多少"。

两个分母都要求最小采样间隔（墙钟 0.5 s、生成时间 0.1 s），太近就沿用上一次 ——
否则用脚本或截图工具在毫秒内连打十几次快照时，令牌数几乎不变，会差分出 0 或离谱的峰。

### 14.4 为什么不用 Prometheus + Grafana

- 这套要多常驻 300–500 MB 内存（Prometheus 200–300 MB + Grafana 150–200 MB），
  还多两个容器；而 WSL 只有 7.5 GB 内存，显存也只剩 2 GB。
- 数据源全在同一台机器、同一个进程/容器里，抓取周期 2 s，不值得为它引入时序库。
- 面板页面 14 KB、只用标准库（`ctypes` / `urllib` / `threading`），镜像不增加任何依赖。
- 代价是没有历史留存：曲线只活在浏览器内存里（120 点 ≈ 4 分钟），刷新即清。
  真要长期留存或告警，再上 Prometheus 抓 `/metrics`（本服务的指标名是合法的）。

### 14.5 NVML 在 WSL 容器里怎么读

容器里**没有** `nvidia-smi` 二进制（镜像没装，也没装 nvidia-container-toolkit），
但 compose 把整个 `/usr/lib/wsl` 挂进来了，宿主驱动库就在
`/usr/lib/wsl/lib/libnvidia-ml.so.1`，直接 ctypes 打开即可：

```python
lib = ctypes.CDLL("/usr/lib/wsl/lib/libnvidia-ml.so.1")
lib.nvmlInit_v2()
lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem))          # total / free / used（字节）
lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(rates))  # gpu% / memory%
lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(t))        # 0 = NVML_TEMPERATURE_GPU
lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(mw))           # 毫瓦
```

代码里按 `/usr/lib/wsl/lib` → `/usr/lib/x86_64-linux-gnu` → `/usr/local/nvidia/lib64`
→ `libnvidia-ml.so.1` → `libnvidia-ml.so` 顺序探测，全都打不开就降级成 `null`
（原生 Windows 跑 `uv run` 时面板照样能用，只是 GPU 卡片显示"—"）。

### 14.6 实测（2026-11-10，32K 配置，容器 dshinfer）

4 并发持续压测（4 线程各发 `max_tokens=160`，连续跑 150 s）：

| 指标 | 值 |
| --- | --- |
| 单条 decode 速度（`engine_rate.window`） | 28–34 tok/s |
| 聚合吞吐（`engine_rate.aggregate`） | 52–68 tok/s（手算 1,017 tok / 15 s = 67.8） |
| `n_busy_slots_per_decode` | 3.44–3.54（4 个 slot 基本都在忙） |
| GPU | 利用率 44–91%、带宽 68%、62–74 °C、69–110 W |
| 显存 | 6,245–6,247 / 8,188 MiB |
| 服务层 TTFT | p50 1.39 s / p95 3.88 s / 均值 1.24 s（样本 30） |
| 服务层端到端 | p50 3.50 s / p95 6.50 s |
| 排队等待 | p50 3 ms（`MAX_CONCURRENCY=4`，第 5 条才开始排） |

对照：空闲时 0 tok/s、显存 6,231 MiB、GPU 0–2%、2.7 W；单流 12 次请求的 TTFT p50 是
0.28 s —— 4 并发时 TTFT 涨到 1.4 s，是 batching 的代价（4 条一起 decode，首字要等
一个 batch 步进），不是服务层的问题。

NVML 的 `util_pct` 在 WSL 下偶尔会在满负载的采样点读到 0（实测 0%、44%、64%、83%、91%
都出现过），所以 GPU 卡片同时显示"带宽利用率"，那个更稳定。

### 14.7 踩过的坑

1. **容器里跑的不是 `/app/src`**：镜像是 `pip install .` 装到
   `/opt/venv/lib/python3.12/site-packages/dshinfer`。改 `/app/src` 不生效
   （第一次加 `/admin/live` 就吃了 404）。快速迭代用
   `docker cp dashboard.py dshinfer:/opt/venv/lib/python3.12/site-packages/dshinfer/`
   + `docker restart`，正式验证走 `bash docker/ops.sh up --build`。
2. **`docker exec` 不加 `-i` 不接 stdin**：heredoc 形式的脚本会被静默忽略、什么都不执行，
   表现为"命令成功但没输出、也没效果"。
3. **llama-server 的 `predicted_tokens_seconds` / `prompt_tokens_seconds` 是采样 gauge**：
   空闲、刚切 slot、以及 4 并发下都会读到 0。别拿它当主数字，要用计数器差分 + 计数器均值。
4. **`/healthz` 的 `engine` 字段是字符串**（`"llama-server"`），`ready` 与它同级；
   前端写成 `health.engine.ready` 就永远是"引擎未就绪"。代码里兼容了嵌套对象形态。
5. **`n_ctx` 与 `max_context_tokens` 是两个字段**：`config.py:46` 是引擎实跑上下文
   （容器里被 `DSHINFER_N_CTX=32768` 覆盖），`config.py:27` 是模型训练上限 65536。
   面板显示前者、后者放 title，免得拿 65,536 去套 32,768 的部署。
6. **无头截图**：`msedge --headless=new --virtual-time-budget=9000 --screenshot=...` 能出图，
   但虚拟时间会让 2 s 轮询在几百毫秒内跑完，曲线只有几个点、数字容易是 0；
   要真实曲线用 CDP（`--remote-debugging-port` + `Page.captureScreenshot`），
   让页面按真实墙钟跑 20 s 再截。

### 14.8 已知局限

- 曲线只在浏览器内存里，刷新/换标签页就清空；没有历史落盘、也没有告警。
- mock 引擎（`DSHINFER_ENGINE=mock`）时 `engine_metrics` 是空对象，引擎表和 tok/s 显示"—"。
- 只在单机单卡验证过；多卡（`nvidia-smi` 索引 0 以外）没有实现。
- 页面没有鉴权：`/dashboard` 与 `/admin/live` 跟 API 一样只监听 compose 里的
  `127.0.0.1:8000`，别把它直接暴露到公网。



