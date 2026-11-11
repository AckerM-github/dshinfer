# 来源与许可

本包把三样东西打在一起。三样都是 MIT，各自的版权归各自作者。

## 1. Python 绑定 —— llama-cpp-python

- 项目：<https://github.com/abetlen/llama-cpp-python>
- 版本：`0.3.36`
- 取出方式：官方 cu124 wheel（`py3-none-win_amd64` / `manylinux_2_35_x86_64`）
- 我们只取其中的 `llama_cpp/*.py`（15 个文件，16,910 行），放在本仓库
  `vendor/llama_cpp/`，构建时逐字节同步进本包；只在 `llama_cpp.py` 里加了一段
  "按 SONAME 加载 ggml 后端"的兼容代码（记录见 `vendor/README.md` 的「我们的改动」）。
- 许可：MIT，见 `LICENSE.llama-cpp-python.md`

## 2. 原生库 —— llama.cpp

- 项目：<https://github.com/ggml-org/llama.cpp>
- 来源：**llama-cpp-python 0.3.36 cu124 wheel 自带的那份构建**（SONAME
  `libllama.so.0.5.0` / `libggml-base.so.0.25.3`，静态后端）。
- Linux：从该 wheel 的 `llama_cpp/lib/` 里按 DT_NEEDED 闭包只取 7 个
  `.so`（`libllama.so`、`libllama.so.0`、`libggml.so.0`、`libggml-base.so.0`、
  `libggml-cpu.so.0`、`libggml-cuda.so.0`、`libmtmd.so.0`）。
- Windows：同一 wheel 自带的 10 个 DLL，加上本机 CUDA 12 运行时的
  `cublas64_12.dll` / `cublasLt64_12.dll` / `cudart64_12.dll`（使包自包含）。
- 许可：MIT，见 `LICENSE.llama.cpp.md`

## 3. 打包脚本与元数据 —— 本项目

- `packaging/stage_native.py`、`packaging/build_engine_wheel.py`、`packaging/build_index.py`、
  本说明、`pyproject.toml`
- 许可：与本项目一致（MIT）

## 为什么版本必须锁在一起

`llama_cpp/*.py` 用 ctypes 按 `llama.h` 的签名声明函数。绑定版本与 `libllama` 的构建
必须一一配套，否则会出现符号缺失、参数错位，或者更隐蔽的——`ggml` 内部结构体对不上，
decode 时跳到野函数指针。本机踩过两次：

- build 12202 的 DLL + `0.3.36` 绑定 = 加载时 `access violation`；
- llama.cpp 官方 Linux 包 b12211（`GGML_BACKEND_DL=ON`）+ `0.3.36` 绑定 =
  `segfault at 100 ip 0000000000000100`（同一个镜像里 `llama-cli` 却完全正常）。

所以本包的原生库一律取自**与绑定同一次发布**的 wheel，不跨版本拼装。
