# vendor/llama_cpp —— 我们自己维护的 llama-cpp-python 源码

这个目录是 **llama-cpp-python 0.3.36 的 Python 源码副本**，不是 pip 依赖。
放进来是为了能读、能改、能钉住版本 —— 它是我们进程内引擎（`src/dshinfer/engine/inproc.py`）的唯一入口。

## 来源

| 项 | 值 |
|---|---|
| 上游 | https://github.com/abetlen/llama-cpp-python |
| 版本 | `0.3.36`（`__init__.py` 里的 `__version__`） |
| 来源文件 | `llama_cpp_python-0.3.36-py3-none-win_amd64.whl`（**cu124** 变体，487.7 MB） |
| 内嵌 llama.cpp | submodule `7d7f0287530bc8981690f361d8c1627dbae1c99a`（2026-11-08） |
| 许可证 | MIT，见同目录 `LICENSE.md` |
| 移植日期 | 2026-11-11 |

选 cu124 而不是 cu132 的原因：`cu132` 那份 wheel 的 `ggml.dll` 静态链了
`ggml-cuda.dll` → `cublas64_13.dll`（CUDA 13），本机驱动只带 CUDA 12 运行时，
整条依赖链在加载阶段就断，**连 CPU 路径都起不来**。

## 搬了什么、没搬什么

搬进来的 15 个文件（**16,910 行**）是 `from llama_cpp import Llama` 的**完整传递闭包**：

```
llama.py 2,520 / llama_cpp.py 5,409 / llama_chat_format.py 4,575 / mtmd_cpp.py 1,579
llama_grammar.py 953 / _internals.py 888 / llama_types.py 316 / _ctypes_extensions.py 166
llama_cache.py 155 / llama_tokenizer.py 120 / _utils.py 99 / llama_speculative.py 64
_logger.py 50 / _ggml.py 12 / __init__.py 4
```

**没搬**：

| 路径 | 行数 | 为什么不搬 |
|---|---:|---|
| `server/` | 1,798 | 它自带的 FastAPI 服务。我们的服务层在 `src/dshinfer/`，两边同时存在会有两套调度语义 |
| `llava_cpp.py` | 154 | 视觉塔绑定，本模型不吃图 |
| `llama_cpp_ext.py` | 136 | 编译期扩展的桩，`py3-none` 的 wheel 里没编译产物 |

结论：搬进来的是一个**库**，不是它附带的服务。

## DLL 不在这里

`.dll` 是二进制（`ggml-cuda.dll` 单个就 873 MB），不进 git。
它们放在 **`D:\llm\runtime\llama-inproc\bin`**（1.59 GB，10 个文件），
由 `config.py::_default_inproc_lib_dir()` 找路径，`engine/inproc.py` 在 import 前
把它塞进 `LLAMA_CPP_LIB_PATH`。

**版本必须配对**：这 10 个 DLL 来自同一个 0.3.36 cu124 wheel。
`D:\llm\runtime\llama.cpp\` 下那份是 build **12202**（`d6be06557`），**不能混用**——
实测把 `LLAMA_CPP_LIB_PATH` 指过去会 `no backends are loaded`，强行手动
`ggml_backend_load()` 之后 decode 立刻 `access violation`。

| 目录 | 对应 llama.cpp | 谁用 |
|---|---|---|
| `runtime\llama.cpp\` | build 12202 / `d6be06557` | `llama-server.exe`（`LlamaServerEngine`） |
| `runtime\llama-inproc\bin\` | `7d7f0287`（2026-11-08） | `vendor/llama_cpp`（`InprocEngine`） |

## 怎么用

`engine/inproc.py` 在 import 前把 `vendor/` 插到 `sys.path[0]`，
所以只要 `DSHINFER_ENGINE=inproc` 就会用这份，不需要 `pip install llama-cpp-python`。

```bash
# Windows 原生
set DSHINFER_ENGINE=inproc

# 或者写进 .env：DSHINFER_ENGINE=inproc
```

## 改上游代码的规矩

1. 改动只允许在**我们用到的那条路径**上（`Llama.__init__` / `create_completion` /
   `generate` / `_ctypes_extensions.load_shared_library`）。
2. 每处改动在文件里用 `# DSH: <原因>` 标注，并同步到本文件下面的"我们的改动"。
3. 上游升级时不要直接覆盖 —— 先在 `_wheel` 里解压新 wheel，`diff` 出增量，再逐处合并。

### 我们的改动

（暂无。当前是原样移植，用来先跑通链路。）

## 已知边界

- **并发 = 1**：一个 `llama_context` 一次只能跑一个 decode。`inproc.py` 用
  `threading.Lock` 串行化，`n_parallel` 对它无效。
- **没有 slots / metrics / id_slot**：那些是 `llama-server` 的东西，这里要自己算。
- **前缀复用只认"上一条 prompt"**：`Llama.generate()` 用 `kv_cache_seq_rm` 截断
  （`llama.py:916-963`），换一个完全不同的 prompt 就把上一个的 KV 冲掉了。
  `llama_cache.py` 里有 `LlamaRAMCache` 可以做跨会话快照，但一条 64K 上下文的
  `LlamaState` 约 1 GB，暂未启用。
