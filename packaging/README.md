# packaging/ —— 自建 `dshinfer-engine` wheel（Windows 本地构建）

本项目是学习项目，**不对外发布**：在 Windows 上本地构建、本地安装就是完整流程。
容器只是复用同一个脚本打出来的 Linux wheel，不走任何远端源。

```text
packaging/
  engine/                 wheel 元数据与说明（pyproject.toml / README / NOTICE / 三份许可）
  native/<平台>/          stage_native.py 摊出来的原生库（不进 git）
  dist/                   产物（不进 git）
  stage_native.py         把原生库摊平到 native/<平台>/
  build_engine_wheel.py   手写 zip wheel（没有构建后端）
```

## 构建：Windows（主路径）

```powershell
python packaging\stage_native.py --platform win_amd64 --from-dir D:\llm\runtime\llama-inproc\bin
python packaging\build_engine_wheel.py --platform win_amd64
uv pip install --no-deps --force-reinstall `
    packaging\dist\dshinfer_engine-0.3.36.post1-py3-none-win_amd64.whl
```

装完自检：

```powershell
.\.venv\Scripts\python.exe -c "import llama_cpp, importlib.metadata as m; print(llama_cpp.__version__, m.version('dshinfer-engine'))"
```

## 构建：Linux（只在要重建容器镜像时用）

```powershell
python packaging\stage_native.py --platform manylinux_2_35_x86_64 --from-wheel D:\dshworkspace\_wheel\up036-cu124.whl
python packaging\build_engine_wheel.py --platform manylinux_2_35_x86_64
```

`docker/Dockerfile` 会 `COPY packaging/dist/*manylinux*.whl` 进镜像。

## 本次产物（2026-11-12）

| 平台 | 大小 | sha256 |
|---|---|---|
| `win_amd64` | 1.58 GB | `1483fbdb91923914f157f919d3b318622ba4c30bcb6caf39328af999e5b5e357` |
| `manylinux_2_35_x86_64` | 0.86 GB | `95f07bbb9d401d55b0b928bce2b34f873e50195586931a988c9985471ad7d3d6` |

## 两条约束（都是踩出来的）

1. **原生库必须与绑定同源**。拿 llama.cpp 官方 Linux 包（`GGML_BACKEND_DL=ON`）拼
   0.3.36 绑定，容器里一 decode 就 `segfault at 100 ip 0000000000000100`；换成绑定
   同一次发布里的静态后端立刻正常。详见 `engine/README.md` 的踩坑记录。
2. **Linux 只带 7 个 `.so`**，不把 `.so` / `.so.0` / `.so.0.x.y` 三份全打进去——
   `libggml-cuda.so` 有 880 MB，复制三遍就超了 GitHub 单文件 2 GB 的上限。

## 分发

不做。wheel 留在 `packaging/dist/`，换机器就拷过去 `pip install`。
不发 PyPI（单文件上限 100 MB，这两个都超）、不发 GitHub Releases。
