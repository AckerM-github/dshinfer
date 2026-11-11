# packaging/ —— 自建 `dshinfer-engine` wheel

把 `vendor/llama_cpp`（Python 绑定）+ 配套原生库打成两个平台 wheel。
**不编译、不依赖 pip 上游包**，产物由 `docker/Dockerfile` 直接 `COPY` 进镜像。

```text
packaging/
  engine/           wheel 的元数据与说明（pyproject.toml / README / NOTICE / 三份许可）
  native/<平台>/    stage_native.py 摊出来的原生库（不进 git）
  dist/             产物（不进 git）
  index/simple/     PEP 503 静态索引页（进 git）
  stage_native.py   vendor 之外的第二件事：把原生库摊平到 native/<平台>/
  build_engine_wheel.py  手写 zip wheel（没有构建后端）
  build_index.py   生成索引页
```

## 构建

```powershell
python packaging\stage_native.py --platform win_amd64 --from-dir D:\llm\runtime\llama-inproc\bin
python packaging\stage_native.py --platform manylinux_2_35_x86_64 --from-wheel D:\dshworkspace\_wheel\up036-cu124.whl
python packaging\build_engine_wheel.py --platform win_amd64
python packaging\build_engine_wheel.py --platform manylinux_2_35_x86_64
python packaging\build_index.py
```

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

PyPI 单文件上限 100 MB，两个 wheel 都超，所以走 GitHub Releases + 静态索引页：

```bash
gh release create engine-v0.3.36.post1 packaging/dist/*.whl --notes "dshinfer-engine 0.3.36.post1"
pip install dshinfer-engine --extra-index-url https://ackerm-github.github.io/dshinfer/simple/
```

（`gh release` 尚未执行；本机只有 SSH key 与 GCM 凭据，需要用户确认后再发。）
