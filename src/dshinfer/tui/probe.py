"""终端面板的数据层：跨平台硬件（NVML / /proc / Win32）+ 服务指标。

为什么单独一层：Textual 只该负责画。所有「可能没有、可能很慢、可能报错」的采集
都收在这里，并统一按「拿不到就降级」处理——硬件缺项置 None，HTTP 抓不到返回
None / 空 dict，sample_hardware() 整个函数不抛异常。这样 UI 的渲染循环里不需要
一个 try，headless 测试也只用标准库就能跑起来。

三路数据：
1. NVML（ctypes 直接开 libnvidia-ml.so.1 / nvml.dll）：多卡、显存、利用率、
   温度、功耗、PCIe 代宽、风扇、时钟。驱动不支持的单项置 None，不影响其他项。
2. CPU / 内存：Linux 读 /proc/stat（两次采样差分，模块级保存上一次）、
   /proc/loadavg、/proc/cpuinfo、/proc/meminfo；Windows 走 GetSystemTimes +
   NtQuerySystemInformation（逐核）+ GlobalMemoryStatusEx。
3. 指标：服务层 /metrics（标准直方图桶 + 累计计数器），以及按累计计数器
   差分出来的 tok/s。引擎在进程内，不再有独立的 /metrics 端点。

约定（跨平台、可降级）：
- 纯标准库：不 import textual，也不 import 任何三方包；
- 时间基准用 time.monotonic()，不受系统时钟跳变影响；
- 采集失败只降级不抛异常，error 字段写人话原因。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "CpuInfo",
    "GpuInfo",
    "Hardware",
    "SysMem",
    "fetch",
    "fetch_json",
    "histogram",
    "nvml_library_path",
    "parse_prom",
    "sample_cpu",
    "sample_hardware",
    "sample_mem",
    "service_metrics",
]


# --------------------------------------------------------------------- 数据形状


@dataclass(slots=True)
class GpuInfo:
    """一块 NVIDIA GPU 的当前快照。

    total/used/free 读不到时是 0.0；其余「驱动可能不提供」的项为 None
    （消费级卡没有风扇转速、笔记本卡可能不报 PCIe 代宽，都是正常情况）。
    """

    index: int
    name: str = ""
    driver: str = ""
    total_mib: float = 0.0
    used_mib: float = 0.0
    free_mib: float = 0.0
    util_pct: float | None = None
    mem_util_pct: float | None = None
    temp_c: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    pcie_gen: int | None = None
    pcie_width: int | None = None
    fan_pct: int | None = None
    clock_core_mhz: int | None = None
    clock_mem_mhz: int | None = None


@dataclass(slots=True)
class CpuInfo:
    """CPU 快照。usage_pct 是两次采样之间的整机占用，per_core_pct 逐核。

    第一次采样没有基线：usage_pct=0、per_core_pct=[]（宁可空一轮，也不编数）。
    Windows 的逐核数据来自 NtQuerySystemInformation，读不到时同样是空列表。
    """

    name: str = "unknown"
    cores: int = 0
    usage_pct: float = 0.0
    per_core_pct: list[float] = field(default_factory=list)
    load1: float | None = None


@dataclass(slots=True)
class SysMem:
    """系统物理内存（MiB）。used = total - available。"""

    total_mib: float = 0.0
    used_mib: float = 0.0
    available_mib: float = 0.0


@dataclass(slots=True)
class Hardware:
    """一次硬件快照。gpus 为空时 error 里写原因（没有 GPU 是正常部署形态）。"""

    gpus: list[GpuInfo] = field(default_factory=list)
    cpu: CpuInfo = field(default_factory=CpuInfo)
    mem: SysMem = field(default_factory=SysMem)
    backend: str = "none"
    error: str | None = None


# ------------------------------------------------------------------------ NVML


class _NvmlMemInfo(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _NvmlUtilRates(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


_C_UINT = ctypes.c_uint
_C_VOID_P = ctypes.c_void_p
_P_UINT = ctypes.POINTER(ctypes.c_uint)

# (库路径, backend 标签)。顺序有讲究：WSL 里真正的驱动在 /usr/lib/wsl/lib（宿主
# 驱动 bind mount 进来），/usr/lib/x86_64-linux-gnu 那个在同版本下是占位；Windows
# 先试 System32 可达的 nvml.dll，再退老版 NVSMI 目录。
_WINDOWS_NVML: tuple[tuple[str, str], ...] = (
    ("nvml.dll", "nvml:windows"),
    (r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll", "nvml:windows"),
)
_LINUX_NVML: tuple[tuple[str, str], ...] = (
    ("/usr/lib/wsl/lib/libnvidia-ml.so.1", "nvml:linux"),
    ("/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1", "nvml:linux"),
    ("/usr/local/nvidia/lib64/libnvidia-ml.so.1", "nvml:linux"),
    ("libnvidia-ml.so.1", "nvml:linux"),
    ("libnvidia-ml.so", "nvml:linux"),
)

_nvml_lock = threading.Lock()
_nvml_tried = False
_nvml_lib: Any = None
_nvml_fns: dict[str, Any] = {}
_nvml_handles: list[tuple[int, Any]] = []
_nvml_backend = "none"
_nvml_path = ""
_nvml_driver = ""
_nvml_error: str | None = None


def _bind(lib: Any, name: str, argtypes: list[Any]) -> Any:
    """取符号并钉死签名；符号缺失/不兼容返回 None（老驱动缺函数是常态）。"""
    fn = getattr(lib, name, None)
    if fn is None:
        return None
    try:
        fn.argtypes = argtypes
        fn.restype = ctypes.c_int
    except (AttributeError, TypeError):
        return None
    return fn


def _bind_all(lib: Any) -> dict[str, Any]:
    """把要用的 NVML 函数一次性绑定好，避免每轮采样都 getattr。"""
    return {
        "driver": _bind(lib, "nvmlSystemGetDriverVersion", [ctypes.c_char_p, _C_UINT]),
        "name": _bind(lib, "nvmlDeviceGetName", [_C_VOID_P, ctypes.c_char_p, _C_UINT]),
        "mem": _bind(
            lib, "nvmlDeviceGetMemoryInfo", [_C_VOID_P, ctypes.POINTER(_NvmlMemInfo)]
        ),
        "util": _bind(
            lib, "nvmlDeviceGetUtilizationRates", [_C_VOID_P, ctypes.POINTER(_NvmlUtilRates)]
        ),
        "temp": _bind(lib, "nvmlDeviceGetTemperature", [_C_VOID_P, _C_UINT, _P_UINT]),
        "power": _bind(lib, "nvmlDeviceGetPowerUsage", [_C_VOID_P, _P_UINT]),
        "power_limit": _bind(lib, "nvmlDeviceGetEnforcedPowerLimit", [_C_VOID_P, _P_UINT]),
        "pcie_gen": _bind(lib, "nvmlDeviceGetCurrPcieLinkGeneration", [_C_VOID_P, _P_UINT]),
        "pcie_width": _bind(lib, "nvmlDeviceGetCurrPcieLinkWidth", [_C_VOID_P, _P_UINT]),
        "fan": _bind(lib, "nvmlDeviceGetFanSpeed", [_C_VOID_P, _P_UINT]),
        "clock": _bind(lib, "nvmlDeviceGetClockInfo", [_C_VOID_P, _C_UINT, _P_UINT]),
    }


def _nvml_open() -> Any:
    """第一次用时打开 NVML 并枚举所有 GPU 的 handle；失败记住原因、不再重试。

    打开成功后 lib/fns/handles 都留在模块级：面板每秒采样一次，不能每次都 dlopen。
    """
    global _nvml_tried, _nvml_lib, _nvml_fns, _nvml_handles
    global _nvml_backend, _nvml_path, _nvml_driver, _nvml_error
    if _nvml_tried:
        return _nvml_lib
    with _nvml_lock:
        if _nvml_tried:
            return _nvml_lib
        _nvml_tried = True
        candidates = _WINDOWS_NVML if os.name == "nt" else _LINUX_NVML
        opened_any = False
        last_error = ""
        for path, backend in candidates:
            try:
                lib = ctypes.CDLL(path)
            except OSError as exc:
                last_error = f"打不开 {path}（{exc}）"
                continue
            opened_any = True
            init = getattr(lib, "nvmlInit_v2", None) or getattr(lib, "nvmlInit", None)
            if init is None:
                last_error = f"{path} 里没有 nvmlInit_v2"
                continue
            try:
                init.restype = ctypes.c_int
                rc = init()
            except Exception as exc:  # pragma: no cover - 驱动异常路径
                last_error = f"nvmlInit 抛异常（{exc}）"
                continue
            if rc != 0:
                last_error = f"nvmlInit_v2 返回 {rc}（{path}）"
                continue
            count_fn = _bind(lib, "nvmlDeviceGetCount_v2", [_P_UINT])
            if count_fn is None:
                count_fn = _bind(lib, "nvmlDeviceGetCount", [_P_UINT])
            count = _C_UINT(0)
            if count_fn is None or count_fn(ctypes.byref(count)) != 0:
                last_error = "拿不到 GPU 数量（nvmlDeviceGetCount_v2）"
                continue
            if count.value == 0:
                last_error = "NVML 正常但没有检测到 GPU"
                continue
            handle_fn = _bind(
                lib, "nvmlDeviceGetHandleByIndex_v2", [_C_UINT, ctypes.POINTER(_C_VOID_P)]
            )
            if handle_fn is None:
                handle_fn = _bind(
                    lib, "nvmlDeviceGetHandleByIndex", [_C_UINT, ctypes.POINTER(_C_VOID_P)]
                )
            handles: list[tuple[int, Any]] = []
            for index in range(count.value):
                handle = _C_VOID_P()
                if handle_fn is None or handle_fn(index, ctypes.byref(handle)) != 0:
                    continue
                handles.append((index, handle))
            if not handles:
                last_error = "NVML 能枚举但拿不到任何 handle"
                continue
            fns = _bind_all(lib)
            _nvml_lib, _nvml_fns, _nvml_handles = lib, fns, handles
            _nvml_backend, _nvml_error = backend, None
            _nvml_path = path
            _nvml_driver = _read_driver(fns)
            return lib
        if not opened_any:
            _nvml_error = "找不到 NVML 库（libnvidia-ml.so.1 / nvml.dll）"
        else:
            _nvml_error = last_error or "NVML 不可用"
        return None


def nvml_library_path() -> str:
    """实际成功打开的 NVML 库路径（空串 = 没打开）。

    backend 只区分 windows/linux；WSL 里驱动库在 /usr/lib/wsl/lib，普通 Linux 在
    /usr/lib/x86_64-linux-gnu，要看这个区别用这里，别改 backend 字符串。
    """
    _nvml_open()
    return _nvml_path


def _read_driver(fns: dict[str, Any]) -> str:
    """驱动版本是整机属性，打开库时读一次就够。"""
    fn = fns.get("driver")
    if fn is None:
        return ""
    buf = ctypes.create_string_buffer(96)
    try:
        if fn(buf, _C_UINT(96)) != 0:
            return ""
        return buf.value.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - 驱动异常路径
        return ""


def _read_uint(fn: Any, handle: Any, param: int | None = None) -> int | None:
    """读一个 uint32 输出。函数缺失、返回错误码、抛异常都算「这项不支持」。"""
    if fn is None:
        return None
    value = _C_UINT(0)
    try:
        if param is None:
            rc = fn(handle, ctypes.byref(value))
        else:
            rc = fn(handle, param, ctypes.byref(value))
    except Exception:  # pragma: no cover - 驱动异常路径
        return None
    if rc != 0:
        return None
    return int(value.value)


def _read_gpu_name(fn: Any, handle: Any) -> str:
    if fn is None:
        return ""
    buf = ctypes.create_string_buffer(96)
    try:
        if fn(handle, buf, _C_UINT(96)) != 0:
            return ""
        return buf.value.decode("utf-8", "replace").strip()
    except Exception:  # pragma: no cover - 驱动异常路径
        return ""


def _read_gpu_mem(fn: Any, handle: Any) -> tuple[float, float, float]:
    if fn is None:
        return 0.0, 0.0, 0.0
    mem = _NvmlMemInfo()
    try:
        if fn(handle, ctypes.byref(mem)) != 0:
            return 0.0, 0.0, 0.0
    except Exception:  # pragma: no cover - 驱动异常路径
        return 0.0, 0.0, 0.0
    mib = 1048576.0
    return (
        round(mem.total / mib, 1),
        round(mem.used / mib, 1),
        round(mem.free / mib, 1),
    )


def _read_gpu_util(fn: Any, handle: Any) -> tuple[float | None, float | None]:
    if fn is None:
        return None, None
    util = _NvmlUtilRates()
    try:
        if fn(handle, ctypes.byref(util)) != 0:
            return None, None
    except Exception:  # pragma: no cover - 驱动异常路径
        return None, None
    return float(util.gpu), float(util.memory)


def _sample_gpus() -> tuple[list[GpuInfo], str]:
    """枚举所有卡并逐项读；返回 (gpus, backend)。NVML 不可用时返回 ([], "none")。"""
    lib = _nvml_open()
    if lib is None:
        return [], "none"
    fns = _nvml_fns
    gpus: list[GpuInfo] = []
    for index, handle in _nvml_handles:
        total_mib, used_mib, free_mib = _read_gpu_mem(fns.get("mem"), handle)
        util_pct, mem_util_pct = _read_gpu_util(fns.get("util"), handle)
        temp = _read_uint(fns.get("temp"), handle, 0)  # NVML_TEMPERATURE_GPU = 0
        power_mw = _read_uint(fns.get("power"), handle)
        limit_mw = _read_uint(fns.get("power_limit"), handle)
        gpus.append(
            GpuInfo(
                index=index,
                name=_read_gpu_name(fns.get("name"), handle) or "NVIDIA GPU",
                driver=_nvml_driver,
                total_mib=total_mib,
                used_mib=used_mib,
                free_mib=free_mib,
                util_pct=util_pct,
                mem_util_pct=mem_util_pct,
                temp_c=None if temp is None else float(temp),
                power_w=None if power_mw is None else round(power_mw / 1000.0, 1),
                power_limit_w=None if limit_mw is None else round(limit_mw / 1000.0, 1),
                pcie_gen=_read_uint(fns.get("pcie_gen"), handle),
                pcie_width=_read_uint(fns.get("pcie_width"), handle),
                fan_pct=_read_uint(fns.get("fan"), handle),
                # NVML_CLOCK_GRAPHICS = 0, NVML_CLOCK_MEM = 2
                clock_core_mhz=_read_uint(fns.get("clock"), handle, 0),
                clock_mem_mhz=_read_uint(fns.get("clock"), handle, 2),
            )
        )
    return gpus, _nvml_backend


# ------------------------------------------------------------------ CPU / 内存

_cpu_lock = threading.Lock()
# 上一次采样的累计计数器，做差分用。Linux 与 Windows 统一成同一形状：
# total/idle 是整机累计时间，cores 是 [(total, idle), ...]（单位无所谓，只要同源）。
_cpu_prev: dict[str, Any] = {"total": None, "idle": None, "cores": None}


def _usage_pct(prev_total: float, prev_idle: float, total: float, idle: float) -> float | None:
    """累计计数器差分出占用率；周期内没有推进（dtotal<=0）返回 None。"""
    d_total = total - prev_total
    d_idle = idle - prev_idle
    if d_total <= 0:
        return None
    pct = (d_total - d_idle) / d_total * 100.0
    return max(0.0, min(100.0, pct))


def _read_proc_stat() -> tuple[float, float, list[tuple[float, float]]] | None:
    """读 /proc/stat：返回 (整机 total, 整机 idle, 逐核 [(total, idle), ...])。"""
    try:
        with open("/proc/stat", "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    total = 0.0
    idle = 0.0
    per_core: list[tuple[float, float]] = []
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            values = [float(item) for item in parts[1:11]]
        except ValueError:
            continue
        # guest / guest_nice 已经算在 user / nice 里，只取前 8 列求和，否则重复计数。
        core_total = sum(values[:8])
        core_idle = values[3] + values[4]  # idle + iowait
        if parts[0] == "cpu":
            total, idle = core_total, core_idle
        else:
            per_core.append((core_total, core_idle))
    if total <= 0:
        return None
    return total, idle, per_core


def _read_load1() -> float | None:
    try:
        with open("/proc/loadavg", "rb") as handle:
            return float(handle.read().decode("utf-8", "replace").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_proc_cpu_name() -> str:
    """从 /proc/cpuinfo 抠一个能看的型号名（x86 是 model name，ARM 是 Processor）。"""
    try:
        with open("/proc/cpuinfo", "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        found.setdefault(key.strip(), value.strip())
    for key in ("model name", "Processor", "Hardware", "cpu model", "cpu"):
        value = found.get(key, "").strip()
        if value:
            return value
    return ""


def _platform_cpu_name() -> str:
    """跨平台兜底：platform.processor() 在 Windows 给出可读型号，Linux 可能为空。"""
    try:
        name = platform.processor()
    except Exception:  # pragma: no cover - 极少数平台会抛
        name = ""
    if name:
        return " ".join(name.split())
    try:
        uname = platform.uname()
        return (uname.processor or uname.machine or "unknown").strip()
    except Exception:  # pragma: no cover
        return "unknown"


def _read_proc_mem() -> SysMem | None:
    """读 /proc/meminfo（单位 kB → MiB）；没有 /proc 时返回 None 交给兜底实现。"""
    try:
        with open("/proc/meminfo", "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    info: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            info[key.strip()] = float(parts[0])
        except ValueError:
            continue
    total_kb = info.get("MemTotal")
    if not total_kb:
        return None
    # MemAvailable 是内核 3.14+ 的估算值；老内核退 MemFree。
    available_kb = info.get("MemAvailable", info.get("MemFree", 0.0))
    total_mib = total_kb / 1024.0
    available_mib = available_kb / 1024.0
    return SysMem(
        total_mib=round(total_mib, 1),
        used_mib=round(max(0.0, total_mib - available_mib), 1),
        available_mib=round(available_mib, 1),
    )


def _sysconf_mem() -> SysMem:
    """没有 /proc 的 POSIX（macOS 等）：用 sysconf 兜底，拿不到就全 0。"""
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page / 1048576.0
        available = os.sysconf("SC_AVPHYS_PAGES") * page / 1048576.0
    except (AttributeError, ValueError, OSError):
        return SysMem()
    return SysMem(
        total_mib=round(total, 1),
        used_mib=round(max(0.0, total - available), 1),
        available_mib=round(available, 1),
    )


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _ProcessorPerfInfo(ctypes.Structure):
    _fields_ = [
        ("idle", ctypes.c_longlong),
        ("kernel", ctypes.c_longlong),
        ("user", ctypes.c_longlong),
        ("dpc", ctypes.c_longlong),
        ("interrupt", ctypes.c_longlong),
        ("interrupt_count", ctypes.c_ulong),
    ]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_phys", ctypes.c_ulonglong),
        ("avail_phys", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("avail_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("avail_virtual", ctypes.c_ulonglong),
        ("avail_extended_virtual", ctypes.c_ulonglong),
    ]


def _filetime_value(ft: _FileTime) -> float:
    return float((int(ft.high) << 32) | int(ft.low))


def _read_windows_cpu_times() -> tuple[float, float] | None:
    """GetSystemTimes → (total, idle)；kernel 时间里已经含 idle，总量 = kernel + user。"""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
    except Exception:
        return None
    return _filetime_value(kernel) + _filetime_value(user), _filetime_value(idle)


def _read_windows_per_core() -> list[tuple[float, float]] | None:
    """逐核累计时间：NtQuerySystemInformation(8) = SystemProcessorPerformanceInformation。

    GetSystemTimes 只给整机。逐核只能走这个「未文档化但稳定」的结构：
    每个核 6 个字段、x64 下 48 字节。布局/返回长度对不上就返回 None，
    调用方会让 per_core_pct 留空——不猜、不编。
    """
    if os.name != "nt":
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        query = ntdll.NtQuerySystemInformation
        query.argtypes = [
            ctypes.c_ulong,
            _C_VOID_P,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        query.restype = ctypes.c_long
        count = os.cpu_count() or 0
        if count <= 0:
            return None
        capacity = count + 8  # 留余量：核数统计口径不一致时不被截断
        buffer = (_ProcessorPerfInfo * capacity)()
        returned = ctypes.c_ulong(0)
        status = query(
            8, ctypes.byref(buffer), ctypes.sizeof(buffer), ctypes.byref(returned)
        )
        if status != 0:
            return None
        item_size = ctypes.sizeof(_ProcessorPerfInfo)
        used = int(returned.value) // item_size if returned.value else count
        used = min(used, capacity)
        if used <= 0:
            return None
        out: list[tuple[float, float]] = []
        for i in range(used):
            # 同 GetSystemTimes 的口径：kernel 含 idle，总量 = kernel + user，空闲 = idle。
            out.append((float(buffer[i].kernel + buffer[i].user), float(buffer[i].idle)))
        return out
    except Exception:  # pragma: no cover - 只在 Windows 上跑
        return None


def _sample_cpu_posix() -> CpuInfo:
    name = _read_proc_cpu_name() or _platform_cpu_name()
    cores = os.cpu_count() or 0
    load1 = _read_load1()
    stat = _read_proc_stat()
    if stat is None:
        return CpuInfo(name=name or "unknown", cores=cores, load1=load1)
    total, idle, per_core = stat
    usage = 0.0
    per_pct: list[float] = []
    with _cpu_lock:
        prev = _cpu_prev
        if prev["total"] is not None and prev["idle"] is not None:
            value = _usage_pct(prev["total"], prev["idle"], total, idle)
            if value is not None:
                usage = value
        prev_cores = prev["cores"]
        if prev_cores is not None and len(prev_cores) == len(per_core):
            for (p_total, p_idle), (c_total, c_idle) in zip(prev_cores, per_core):
                value = _usage_pct(p_total, p_idle, c_total, c_idle)
                if value is not None:
                    per_pct.append(round(value, 1))
        _cpu_prev.update(total=total, idle=idle, cores=per_core)
    return CpuInfo(
        name=name or "unknown",
        cores=cores or len(per_core),
        usage_pct=round(usage, 1),
        per_core_pct=per_pct,
        load1=load1,
    )


def _sample_cpu_windows() -> CpuInfo:
    times = _read_windows_cpu_times()
    per_core = _read_windows_per_core()
    cores = os.cpu_count() or (len(per_core) if per_core else 0)
    usage = 0.0
    per_pct: list[float] = []
    with _cpu_lock:
        prev = _cpu_prev
        if times is not None and prev["total"] is not None and prev["idle"] is not None:
            value = _usage_pct(prev["total"], prev["idle"], times[0], times[1])
            if value is not None:
                usage = value
        prev_cores = prev["cores"]
        if per_core is not None and prev_cores is not None and len(prev_cores) == len(per_core):
            for (p_total, p_idle), (c_total, c_idle) in zip(prev_cores, per_core):
                value = _usage_pct(p_total, p_idle, c_total, c_idle)
                if value is not None:
                    per_pct.append(round(value, 1))
        _cpu_prev.update(
            total=None if times is None else times[0],
            idle=None if times is None else times[1],
            cores=per_core,
        )
    return CpuInfo(
        name=_platform_cpu_name(),
        cores=cores,
        usage_pct=round(usage, 1),
        per_core_pct=per_pct,
        load1=None,  # Windows 没有 /proc/loadavg，也不想为这个起 wmic
    )


def _windows_mem() -> SysMem:
    if os.name != "nt":
        return SysMem()
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        status = _MemoryStatusEx()
        status.length = ctypes.sizeof(_MemoryStatusEx)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return SysMem()
    except Exception:  # pragma: no cover - 只在 Windows 上跑
        return SysMem()
    mib = 1048576.0
    total = status.total_phys / mib
    available = status.avail_phys / mib
    return SysMem(
        total_mib=round(total, 1),
        used_mib=round(max(0.0, total - available), 1),
        available_mib=round(available, 1),
    )


def sample_cpu() -> CpuInfo:
    """采一次 CPU。第一次调用没有基线（usage_pct=0、per_core_pct=[]），永不抛异常。"""
    try:
        return _sample_cpu_windows() if os.name == "nt" else _sample_cpu_posix()
    except Exception:  # pragma: no cover - 采集层兜底
        return CpuInfo(name=_platform_cpu_name(), cores=os.cpu_count() or 0)


def sample_mem() -> SysMem:
    """采一次系统内存。拿不到返回全 0，永不抛异常。"""
    try:
        if os.name == "nt":
            return _windows_mem()
        proc = _read_proc_mem()
        return proc if proc is not None else _sysconf_mem()
    except Exception:  # pragma: no cover - 采集层兜底
        return SysMem()


def sample_hardware() -> Hardware:
    """一次硬件快照：GPU（多卡）+ CPU + 内存。**永不抛异常**。

    UI 每秒调一次；任何一项拿不到都只降级：GPU 缺失时 gpus=[] 且 error 写原因，
    CPU/内存失败时给默认值。建议采样间隔 ≥0.5 s，CPU 差分才有意义。
    """
    gpus: list[GpuInfo] = []
    backend = "none"
    errors: list[str] = []
    try:
        gpus, backend = _sample_gpus()
    except Exception as exc:  # pragma: no cover - 采集层兜底
        errors.append(f"GPU 采集失败：{exc}")
    if not gpus:
        errors.append(_nvml_error or "未检测到 NVIDIA GPU（NVML 不可用）")
    try:
        cpu = sample_cpu()
    except Exception as exc:  # pragma: no cover - 采集层兜底
        cpu = CpuInfo(name=_platform_cpu_name(), cores=os.cpu_count() or 0)
        errors.append(f"CPU 采集失败：{exc}")
    try:
        mem = sample_mem()
    except Exception as exc:  # pragma: no cover - 采集层兜底
        mem = SysMem()
        errors.append(f"内存采集失败：{exc}")
    return Hardware(
        gpus=gpus,
        cpu=cpu,
        mem=mem,
        backend=backend,
        error="；".join(errors) if errors else None,
    )


# -------------------------------------------------------------------- HTTP 抓取

_DEFAULT_TIMEOUT = 0.8


def _normalize_url(url: str) -> str:
    """允许配置里只写 127.0.0.1:8000/metrics（面板配置常这么写）。"""
    if "://" in url:
        return url
    if url.startswith("/"):
        return url
    return "http://" + url


def fetch(url: str | None, timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    """抓一段 HTTP 文本；空 url、超时、连不上、非 2xx 一律返回 None。

    面板每秒都在抓，绝不能在这里阻塞或抛异常：短超时 + 全异常兜底。
    """
    if not url:
        return None
    try:
        with urllib.request.urlopen(_normalize_url(url), timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def fetch_json(url: str | None, timeout: float = _DEFAULT_TIMEOUT) -> Any | None:
    """抓 JSON（/admin/live、/v1/models 这类）；抓不到或解析失败都返回 None。"""
    text = fetch(url, timeout=timeout)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# --------------------------------------------------------------- Prometheus 解析

# 样本行：metric{labels} value [timestamp]。标签值里允许空格，用 [^}]* 整体吃掉；
# 可选的时间戳列单独匹配，避免把 metric 1.5 1700000000000 的毫秒数当成值。
_SAMPLE_RE = re.compile(
    r"^(?P<head>[A-Za-z_:][A-Za-z0-9_:]*(?:\{[^}]*\})?)"
    r"\s+(?P<value>[^\s]+)"
    r"(?:\s+[^\s]+)?$"
)
_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
_UNESCAPE_RE = re.compile(r"\\(.)")
_UNESCAPE_MAP = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape_label(value: str) -> str:
    return _UNESCAPE_RE.sub(
        lambda match: _UNESCAPE_MAP.get(match.group(1), match.group(1)), value
    )


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _canonical_labels(raw: str) -> str:
    """把 b="2",a="1" 规范化成 {a="1",b="2"}（标签名排序），好比对、好测试。"""
    labels: dict[str, str] = {}
    for match in _LABEL_RE.finditer(raw):
        labels[match.group(1)] = _unescape_label(match.group(2))
    if not labels:
        return ""
    body = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())
    )
    return "{" + body + "}"


def _split_head(head: str) -> tuple[str, str]:
    """name{a="b"} → ("name", '{a="b"}')；无标签时第二个元素是空串。"""
    if head.endswith("}") and "{" in head:
        name, _, raw = head.partition("{")
        return name.strip(), _canonical_labels(raw[:-1])
    return head.strip(), ""


def parse_prom(text: str | None) -> dict[str, float]:
    """解析 Prometheus 文本暴露格式（忽略 # 注释），返回 {样本键: 值}。

    带标签的样本键会被规范化成 name{k="v",...}（标签名排序、去转义），这样同一份
    dict 里既能取 tokens_predicted_total 这类标量，也能让 histogram() 找到
    xxx_bucket{le="0.05"} 的桶。值是 float（+Inf/NaN 也能转）。
    """
    out: dict[str, float] = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        name, labels = _split_head(match.group("head"))
        if not name:
            continue
        out[name + labels] = value
    return out


def _label_text(sample_key: str, label: str) -> str | None:
    """从规范化后的样本键里取一个标签值。"""
    if "{" not in sample_key:
        return None
    raw = sample_key[sample_key.index("{") :]
    match = re.search(rf'{re.escape(label)}\s*=\s*"((?:[^"\\]|\\.)*)"', raw)
    if match is None:
        return None
    return _unescape_label(match.group(1))


def _label_float(labels_raw: str, label: str) -> float | None:
    if not labels_raw:
        return None
    wrapped = labels_raw if labels_raw.startswith("{") else "{" + labels_raw + "}"
    text = _label_text(wrapped, label)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """从累积直方图桶里插值出分位数（Prometheus histogram_quantile 的简化版）。

    桶内线性插值；最后一个桶通常是 +Inf（累计值就是总数），它没有上界可用，
    所以返回上一个有限上界——宁可低估，也不要报 inf。这个口径与 dashboard.py
    里的 _quantile 一致。
    """
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, cumulative in buckets:
        if cumulative >= rank:
            if le == float("inf"):
                return prev_le
            span = cumulative - prev_c
            if span <= 0:
                return le
            return round(prev_le + (le - prev_le) * (rank - prev_c) / span, 4)
        prev_le, prev_c = le, cumulative
    return buckets[-1][0]


def histogram(prom: dict[str, float] | None, name: str) -> dict[str, Any]:
    """从 name_bucket{le=...} + name_count + name_sum 算 count/均值/p50/p95。

    没有这个直方图时返回 {"count": 0.0, "mean_s": None, "p50_s": None, "p95_s": None}，
    方便 UI 直接渲染 N/A，不用判 None。
    """
    result: dict[str, Any] = {"count": 0.0, "mean_s": None, "p50_s": None, "p95_s": None}
    if not prom:
        return result
    prefix = name + "_bucket"
    buckets: dict[float, float] = {}
    for key, value in prom.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix and not suffix.startswith("{"):
            continue  # name_bucket_xxx 这种同前缀的别的指标
        le = _label_float(suffix, "le")
        if le is None:
            continue
        # 同一个 le 重复出现时取累计值更大的（并发合并的极端情况）
        buckets[le] = max(buckets.get(le, 0.0), float(value))
    count_value = prom.get(name + "_count")
    if count_value is None:
        if not buckets:
            return result
        count_value = max(buckets.values())
    try:
        count = float(count_value)
    except (TypeError, ValueError):
        return result
    if count <= 0:
        return result
    result["count"] = count
    total_value = prom.get(name + "_sum")
    if total_value is not None:
        try:
            result["mean_s"] = round(float(total_value) / count, 4)
        except (TypeError, ValueError):
            pass
    ordered = sorted(buckets.items())
    if ordered:
        result["p50_s"] = _quantile(ordered, 0.5)
        result["p95_s"] = _quantile(ordered, 0.95)
    return result


def service_metrics(url: str | None, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """抓服务层 /metrics（标准 Prometheus 文本），摊平成面板要的形状。

    形状与 dashboard.py 的 service_metrics() 对齐：inflight / queued /
    requests_total / requests_by_status / tokens / scheduler_events / histograms。
    额外带 available（这轮抓到没有）和 raw（原始指标，方便 UI 临时取别的字段）。
    抓不到时所有字段给空值，UI 不用判 None。
    """
    text = fetch(url, timeout=timeout)
    prom = parse_prom(text) if text else {}
    by_status: dict[str, float] = {}
    tokens: dict[str, float] = {}
    events: dict[str, float] = {}
    inflight = 0.0
    queued = 0.0
    for key, value in prom.items():
        name = key.partition("{")[0]
        if name == "dshinfer_inflight_requests":
            inflight = value
        elif name == "dshinfer_queue_depth":
            queued = value
        elif name == "dshinfer_requests_total":
            status = _label_text(key, "status") or "?"
            by_status[status] = by_status.get(status, 0.0) + value
        elif name == "dshinfer_tokens_total":
            direction = _label_text(key, "direction") or "?"
            tokens[direction] = tokens.get(direction, 0.0) + value
        elif name == "dshinfer_scheduler_events_total":
            event = _label_text(key, "event") or "?"
            events[event] = events.get(event, 0.0) + value

    histograms: dict[str, dict[str, Any]] = {}
    for key in prom:
        name = key.partition("{")[0]
        for suffix in ("_bucket", "_count", "_sum"):
            if not name.endswith(suffix):
                continue
            base = name[: -len(suffix)]
            if base not in histograms:
                histograms[base] = histogram(prom, base)
            break
    return {
        "available": text is not None,
        "inflight": inflight,
        "queued": queued,
        "requests_total": sum(by_status.values()),
        "requests_by_status": by_status,
        "tokens": tokens,
        "scheduler_events": events,
        "histograms": histograms,
        "raw": prom,
    }
