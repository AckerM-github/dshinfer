"""模型注册表：扫目录里的 .gguf，报出每个模型能干什么。

这是 Ollama 最核心的体验之一（`ollama list` / `ollama run 任意模型`），也是我们
相对"改配置再重启"的第一个改进点：模型列表是**枚举目录**得到的，不是手写死的。

约定：
  · id 默认取文件名（去掉 .gguf），显式给 default_id 的那个模型用友好名（settings.model_name）
  · 只看文件头，不校验张量能不能跑 —— 能不能跑由引擎决定，注册表只负责"有什么"
  · 扫描结果按 (路径, mtime, 大小) 缓存，新增文件后下一次扫描自动出现
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gguf import read_meta

__all__ = ["ModelEntry", "ModelNotFound", "ModelRegistry"]


class ModelNotFound(LookupError):
    def __init__(self, model_id: str) -> None:
        super().__init__(f"未找到模型: {model_id}")
        self.model_id = model_id


@dataclass(frozen=True)
class ModelEntry:
    id: str
    path: str
    size_bytes: int
    arch: str | None = None
    n_layers: int | None = None
    n_ctx_train: int | None = None
    n_embd: int | None = None

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / (1024**3), 2)

    def to_dict(self, *, loaded: bool = False) -> dict[str, object]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "local",
            "loaded": loaded,
            "path": self.path,
            "size_gb": self.size_gb,
            "arch": self.arch,
            "n_layers": self.n_layers,
            "n_ctx_train": self.n_ctx_train,
            "n_embd": self.n_embd,
        }


def _key(p: Path) -> str:
    try:
        return str(p.resolve()).lower()
    except OSError:
        return str(p).lower()


class ModelRegistry:
    def __init__(
        self,
        models_dir: str = "",
        *,
        default_path: str = "",
        default_id: str = "",
        extra_paths: tuple[str, ...] = (),
    ) -> None:
        self._dir = Path(models_dir) if models_dir else None
        self._default = _key(Path(default_path)) if default_path else ""
        self._default_id = default_id
        self._extra = [Path(p) for p in extra_paths if p]
        self._cache: dict[str, tuple[float, int, ModelEntry]] = {}

    # ---- 扫描 ----
    def _candidates(self) -> list[Path]:
        paths: list[Path] = []
        if self._dir is not None and self._dir.is_dir():
            paths.extend(sorted(self._dir.glob("*.gguf")))
        for p in self._extra:
            if p.suffix.lower() == ".gguf" and p.is_file():
                paths.append(p)

        seen: set[str] = set()
        out: list[Path] = []
        for p in paths:
            k = _key(p)
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    def _entry_for(self, p: Path) -> ModelEntry | None:
        try:
            st = p.stat()
        except OSError:
            return None
        k = _key(p)
        hit = self._cache.get(k)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]

        meta = read_meta(p)
        mid = self._default_id if (self._default and k == self._default and self._default_id) else p.stem
        entry = ModelEntry(
            id=mid,
            path=str(p),
            size_bytes=st.st_size,
            arch=meta.arch if meta else None,
            n_layers=meta.n_layers if meta else None,
            n_ctx_train=meta.n_ctx_train if meta else None,
            n_embd=meta.n_embd if meta else None,
        )
        self._cache[k] = (st.st_mtime, st.st_size, entry)
        return entry

    def scan(self) -> list[ModelEntry]:
        entries = [e for e in (self._entry_for(p) for p in self._candidates()) if e is not None]
        entries.sort(key=lambda e: (e.id != self._default_id, e.id.lower()))
        return entries

    # ---- 查找 ----
    def get(self, model_id: str) -> ModelEntry | None:
        want = (model_id or "").strip().lower()
        if not want:
            return None
        for e in self.scan():
            if e.id.lower() == want:
                return e
        # 也允许直接给文件名 / 绝对路径
        for e in self.scan():
            if Path(e.path).stem.lower() == want or _key(Path(e.path)) == want:
                return e
        return None

    def resolve(self, model_id: str) -> ModelEntry:
        e = self.get(model_id)
        if e is None:
            raise ModelNotFound(model_id)
        return e
