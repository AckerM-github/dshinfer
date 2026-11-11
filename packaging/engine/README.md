# dshinfer-engine

`dshinfer` 的进程内推理引擎：把 **llama-cpp-python 的 Python 绑定**（源自我们仓库的
`vendor/llama_cpp/`）和**配套的原生库**（`libllama.so` / `llama.dll` 等）打进同一个平台
wheel，安装后 `import llama_cpp` 即可用，不需要编译器、不需要 CUDA Toolkit。

## 版本映射

| 本包 | llama-cpp-python | 原生库来源 |
|---|---|---|
| `0.3.36.post1` | `0.3.36`（cu124 wheel） | **同一个 wheel 里的预编译库**（llama.cpp SONAME `0.25.3` / `0.5.0`，静态后端） |

`.post1` 是我们的打包修订。原生库**必须与绑定同源**，这一条是踩出来的，见下面
"为什么 Linux 不用官方 tar.gz"。

## 里面有什么

```text
llama_cpp/                 15 个 .py，逐字节来自 vendor/llama_cpp/
  lib/
    llama.dll …            仅 win_amd64：10 个 DLL（含 cublas/cudart）
    libllama.so …          仅 manylinux：7 个 .so（不含 CLI、不含 rpc）
```

Linux 那 7 个是有讲究的：`libllama.so`（ctypes 直接打开的名字）、`libllama.so.0`、
`libggml.so.0`、`libggml-base.so.0`、`libggml-cpu.so.0`、`libggml-cuda.so.0`、`libmtmd.so.0`。
wheel 是 zip，装出来没有符号链接，所以每个别名都必须是实文件；但也**不能**把
`.so` / `.so.0` / `.so.0.25.3` 三份全带上——`libggml-cuda.so` 有 880 MB，复制三遍就是 2.6 GB。

原生库按平台打包，所以同一个版本号有两个 wheel：

```text
dshinfer_engine-0.3.36.post1-py3-none-win_amd64.whl
dshinfer_engine-0.3.36.post1-py3-none-manylinux_2_35_x86_64.whl
```

## 构建

```powershell
# 1) 把原生库摊进 packaging/native/<platform>/
python packaging\stage_native.py --platform win_amd64 `
    --from-dir D:\llm\runtime\llama-inproc\bin
python packaging\stage_native.py --platform manylinux_2_35_x86_64 `
    --from-wheel D:\dshworkspace\_wheel\up036-cu124.whl

# 2) 打 wheel（不编译，只打包）
python packaging\build_engine_wheel.py --platform win_amd64
python packaging\build_engine_wheel.py --platform manylinux_2_35_x86_64
```

产物在 `packaging/dist/`。

## 本次产物

| 平台 | 大小 |
|---|---|
| `win_amd64` | 1.58 GB |
| `manylinux_2_35_x86_64` | 0.87 GB |

具体 sha256 记在仓库的 `packaging/README.md`（不写进包内，否则每改一次本文档就要重打一次包）。

Windows 那份大 2 倍，因为它要自带 `cublas64_12.dll` + `cublasLt64_12.dll`（735 MB）和
`ggml-cuda.dll`（873 MB）；Linux 的 CUDA 运行时由基础镜像提供，我们只带 `libggml-cuda.so`。

## 为什么 Linux 不用官方 tar.gz（踩坑记录）

最初 Linux 侧是从 `llama-b12211-bin-ubuntu-cuda-12.8-x64.tar.gz` 里取 `.so` 的，结果容器里
`POST /v1/chat/completions` 直接把进程打死，`dmesg` 只有一行：

```text
python[..]: segfault at 100 ip 0000000000000100 sp ... error 14
```

gdb 栈指向 `ggml_graph_compute_thread` → `libggml-cpu-alderlake.so`，即后端插件里跳了个 0x100
的函数指针。排查过程与结论：

1. 同一个镜像里用 tarball 自带的 `llama-cli` 跑同一个模型**完全正常**（18.7 / 15.5 t/s），
   所以不是 GPU、驱动、模型的问题。
2. 绕开 Python 的高层逻辑、只用 C API（`llama_batch_get_one` + `llama_decode`）**同样崩**，
   所以不是绑定层的 Python 代码问题。
3. 换成 llama-cpp-python 官方 manylinux wheel 里的 `.so`（同一份 `vendor/llama_cpp/*.py`）
   **立刻正常**。
4. 对比库版本才看清差别：wheel 里是 `libllama.so.0.5.0` / `libggml-base.so.0.25.3`，
   静态后端（只有 `libggml-cpu.so`）；官方 tar.gz 是 `0.9.0` / `0.29.0` 且
   `GGML_BACKEND_DL=ON`（20 个 `libggml-cpu-<arch>.so` 插件）。

**结论**：绑定层是按 wheel 里那个 llama.cpp 版本写的 ctypes 声明，跨版本拼装会在 `ggml` 内部
结构体上错位。原生库必须与绑定同源——所以 `stage_native.py` 的 Linux 分支改成从
**同一个 wheel** 里取库，并且按 DT_NEEDED 闭包只带 7 个文件。

## 安装

```bash
pip install packaging/dist/dshinfer_engine-0.3.36.post1-py3-none-win_amd64.whl
# 容器/离线环境用 --no-deps：依赖已在镜像的 requirements.lock 里
pip install --no-deps packaging/dist/dshinfer_engine-0.3.36.post1-py3-none-manylinux_2_35_x86_64.whl
```

装完验证：

```bash
python -c "import llama_cpp, pathlib; print(llama_cpp.__version__, pathlib.Path(llama_cpp.__file__).parent)"
```

## 分发

不做。这是学习项目，wheel 只在本机构建、本机安装：产物留在 `packaging/dist/`，
换机器就拷过去 `pip install`。不发 PyPI（单文件上限 100 MB，这两个都超），
也不发 GitHub Releases。

## 边界（诚实说明）

- 我们**拥有分发，不拥有编译**：原生库是 llama.cpp 的构建产物（经 llama-cpp-python 的
  manylinux wheel 转手），我们做的是钉版本、配对、装进自己的包、自己维护 Python 侧。
- 想要"从源码到 wheel 全自主"，需要在带 nvcc 的镜像里 `cmake --build`，每次 30–40 分钟；
  到那时把 `stage_native.py` 的 Linux 分支换成"构建产物 → native/"，包结构不用变。
- 绑定层的 `llama_cpp/*.py` 是我们 vendor 的副本，改它就要在 `vendor/PATCHES.md` 里记一笔。

## 许可

- 本包：MIT（项目主许可）
- `llama_cpp/*.py`：MIT，来自 [llama-cpp-python](https://github.com/abetlen/llama-cpp-python)
- 原生库：MIT，来自 [llama.cpp](https://github.com/ggml-org/llama.cpp)（由 llama-cpp-python 构建分发）

详见 `NOTICE.md`。
