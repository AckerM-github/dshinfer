"""启动向导：10 步把"要跑成什么样"问清楚，最后落成预设 + compose 环境变量片段。

按键（每一屏都一样，用户要求一致）：
    ↑ ↓      在选项间移动
    Enter    确认 / 下一步
    Esc      返回上一步（第一屏是退出向导）
    ?        解释当前屏（这一步到底在问什么、这台机器上的实测数字）
    q        退出向导

为什么不用多个 Screen 堆栈：10 屏共享同一份 Preset 状态，用"单屏 + 重绘正文"实现
更不容易把状态丢在 Screen 栈里；Esc 返回也只是 index-1 再重绘。
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .preset import Preset, discover_models, presets_dir, write_env_file
from .widgets import bar, fmt_bytes

STEPS: list[tuple[str, str, str]] = [
    (
        "preset",
        "预设",
        "从已经保存过的预设开始，或者用默认值重开一份。预设 = 一次运行的完整参数"
        "（模型、上下文、并发、调优、访问方式），保存后下次 Enter 一下就复用。",
    ),
    (
        "model",
        "模型",
        "选择这次要加载的 GGUF。列表来自模型文件夹里的 *.gguf（含 draft/ 子目录）。"
        "换量化档就等于换一套显存与速度：Q4_K_M 5.2 GB / 40 t/s，Q3_K_M 4.3 GB 更省显存但质量略降。",
    ),
    (
        "folder",
        "模型文件夹",
        "服务层会扫描这个目录里的 *.gguf 暴露到 /v1/models；容器里它是挂载点 /models 的宿主侧来源"
        "（compose 里的 MODELS_DIR）。",
    ),
    (
        "context",
        "上下文 / KV 缓存格式",
        "上下文越长越吃显存；KV 量化档决定每 token 的 KV 大小。8 GB 显存实测："
        "32K + q8_0 = 6.0 GB / 40.4 t/s；64K + q8_0 = 6.2 GB / 26.3 t/s（速度掉 35%）；"
        "64K + f16 直接 OOM。",
    ),
    (
        "image",
        "图片（多模态）",
        "llama.cpp 的多模态要额外一个 mmproj 投影文件（vision tower 的 GGUF）。"
        "没有就别开：开了但文件不对，引擎会起不来。",
    ),
    (
        "concurrency",
        "并发",
        "引擎 slot 数（--parallel）与服务层同时在飞的上限。配 kv-unified 时 slot 不额外占 KV，"
        "4 路是纯赚：单流只慢约 20%，聚合吞吐 42 → 110 t/s。服务层并发别超过 slot 数，"
        "否则多出来的请求永远排不到 slot。",
    ),
    (
        "tuning",
        "调优",
        "n_batch / n_ubatch 影响预填充：本机实测 ubatch 1024 比 512 快约 10%，再往上收益消失、"
        "显存线性增长。FlashAttention 在 40 系（sm_89）上有用，关掉更慢也更费显存。",
    ),
    (
        "advanced",
        "高级",
        "服务日志写到哪里。引擎在服务进程内，没有引擎端口、没有引擎可执行文件，"
        "容器里固定是 /var/log/dshinfer/service.log，本机原生跑才需要改。",
    ),
    (
        "access",
        "访问",
        "OpenAI 兼容接口监听在哪里。默认只绑 127.0.0.1（WSL 会把 localhost 转发到 Windows，"
        "所以 Windows 上也能 curl）；绑 0.0.0.0 就是局域网可访问——注意这套接口没有鉴权。",
    ),
    (
        "save",
        "保存预设",
        "把这份配置落成 JSON + 可直接贴进 compose 的 .env 片段，然后启动监视器。",
    ),
]


class HelpScreen(ModalScreen[None]):
    """问号浮层：解释当前这一屏在问什么。"""

    BINDINGS = [
        Binding("escape", "close", "关闭"),
        Binding("q", "close", "关闭"),
        Binding("enter", "close", "关闭"),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"[bold]{self._title}[/]", classes="help-title"),
            Static(self._body, classes="help-body"),
            Static("[dim]Esc / Enter 返回[/]", classes="help-hint"),
            id="help-box",
        )

    def action_close(self) -> None:
        self.dismiss(None)


class WizardApp(App[Preset | None]):
    """向导本体。返回值是 Preset（保存后）或 None（中途退出）。"""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("escape", "back", "返回"),
        Binding("question_mark", "help", "解释"),
        Binding("q", "quit_wizard", "退出"),
    ]

    def __init__(self, preset: Preset | None = None) -> None:
        super().__init__()
        self.preset = preset or Preset.latest()
        self.index = 0
        # 保存页选的是「保存并启动监视器」还是「只保存」
        self.launch_monitor = True

    # ------------------------------------------------------------ 生命周期

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield VerticalScroll(id="body")
        yield Static(
            "↑↓ 选择 · Enter 下一步 · Esc 返回 · ? 解释 · q 退出", id="hintbar"
        )

    def on_mount(self) -> None:
        self._render()

    # ------------------------------------------------------------ 渲染

    def _render(self) -> None:
        key, title, _ = STEPS[self.index]
        self.query_one("#topbar", Static).update(
            f" [bold]dshinfer 向导[/]  "
            f"{bar((self.index + 1) / len(STEPS) * 100, 12)}  "
            f"[bold]第 {self.index + 1}/{len(STEPS)} 步 · {title}[/]"
        )
        body = self.query_one("#body", VerticalScroll)
        body.remove_children()
        self.call_after_refresh(self._mount_step, key)

    def _mount_step(self, key: str) -> None:
        body = self.query_one("#body", VerticalScroll)
        widgets = getattr(self, f"_build_{key}")()
        if widgets:
            body.mount_all(widgets)
        self.call_after_refresh(self._focus_primary)

    def _focus_primary(self) -> None:
        body = self.query_one("#body", VerticalScroll)
        options = body.query(OptionList)
        if options:
            options.first().focus()
            return
        inputs = body.query(Input)
        if inputs:
            inputs.first().focus()

    # ------------------------------------------------------------ 通用片段

    @staticmethod
    def _label(text: str) -> Static:
        return Static(f"[bold]{text}[/]", classes="wizard-label")

    @staticmethod
    def _note(text: str) -> Static:
        return Static(f"[dim]{text}[/]", classes="wizard-note")

    def _options(self, options: list[Option], widget_id: str = "choices") -> OptionList:
        widget = OptionList(*options, id=widget_id)
        return widget

    # ------------------------------------------------------------ 第 1 步：预设

    def _build_preset(self) -> list:
        options = [
            Option(
                f"沿用当前值  ·  {self.preset.model_stem}  ·  {self.preset.n_ctx:,} 上下文",
                id="preset:keep",
            )
        ]
        folder = presets_dir()
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                loaded = Preset.load(path)
            except (OSError, ValueError, TypeError):
                continue
            options.append(
                Option(
                    f"{loaded.name}  ·  {loaded.model_stem}  ·  {loaded.n_ctx:,} 上下文",
                    id=f"preset:{path}",
                )
            )
        options.append(Option("＋ 用默认值新建一份", id="preset:new"))
        return [
            self._label("选择起点"),
            self._note(f"预设目录：{presets_dir()}"),
            self._options(options),
        ]

    def _choose_preset(self, value: str) -> None:
        if value == "preset:new":
            self.preset = Preset.default()
        elif value != "preset:keep":
            path = Path(value.split(":", 1)[1])
            try:
                self.preset = Preset.load(path)
            except (OSError, ValueError, TypeError) as exc:
                self.preset = Preset.default()
                self.notify(f"预设读不出来（{exc}），改用默认值", severity="warning")
        self._goto(1)

    # ------------------------------------------------------------ 第 2 步：模型

    def _build_model(self) -> list:
        models = discover_models(self.preset.models_dir)
        options: list[Option] = []
        for path in models:
            rel = path.relative_to(Path(self.preset.models_dir).expanduser())
            mark = "* " if str(rel) == self.preset.model_file else "  "
            options.append(
                Option(f"{mark}{rel}   {fmt_bytes(path.stat().st_size)}", id=f"model:{rel}")
            )
        if not options:
            options.append(Option("（这个目录里没有 .gguf，先选「手动输入路径」）", id="model:manual"))
        options.append(Option("手动输入路径…", id="model:manual"))
        return [
            self._label("选择模型"),
            self._note(f"来自 {self.preset.models_dir}（* 表示当前选中）"),
            self._options(options),
        ]

    def _choose_model(self, value: str) -> None:
        if value == "model:manual":
            self._replace_with_input(
                "模型文件完整路径",
                self.preset.gguf_path,
                "manual-model",
                self._submit_model_path,
            )
            return
        self.preset.model_file = value.split(":", 1)[1]
        self._goto(2)

    def _submit_model_path(self, raw: str) -> None:
        path = Path(raw.strip().expanduser())
        if not path.is_file():
            self.notify(f"文件不存在：{path}", severity="error")
            return
        self.preset.model_file = str(path)
        self.preset.models_dir = str(path.parent)
        self._goto(2)

    # ------------------------------------------------------------ 第 3 步：文件夹

    def _build_folder(self) -> list:
        return [
            self._label("模型文件夹"),
            self._note("容器里它是 /models 的宿主侧来源；服务层只扫这一层的 *.gguf"),
            Input(value=self.preset.models_dir, id="folder", placeholder="模型目录的绝对路径"),
        ]

    def _submit_folder(self, raw: str) -> None:
        path = Path(raw.strip().expanduser())
        if not path.is_dir():
            self.notify(f"目录不存在：{path}", severity="error")
            return
        self.preset.models_dir = str(path)
        if self.preset.model_file and not Path(self.preset.gguf_path).is_file():
            found = discover_models(path)
            if found:
                self.preset.model_file = found[0].name
        self._goto(3)

    # ------------------------------------------------------------ 第 4 步：上下文

    def _build_context(self) -> list:
        rows = [
            ("ctx:8192:q8_0", "8,192   · q8_0   · 约 5.8 GB 显存 · 短对话最省"),
            ("ctx:16384:q8_0", "16,384  · q8_0   · 约 5.9 GB 显存"),
            ("ctx:32768:q8_0", "32,768  · q8_0   · 约 6.0 GB 显存 · 实测 40.4 t/s · 推荐"),
            ("ctx:65536:q8_0", "65,536  · q8_0   · 约 6.2 GB 显存 · 实测 26.3 t/s（掉 35%）"),
            ("ctx:32768:f16", "32,768  · f16    · 约 6.5 GB 显存 · 质量最好但更费显存"),
        ]
        return [
            self._label("上下文长度与 KV 量化"),
            self._note("数值来自本机 RTX 4060 Laptop 8 GB + Q4_K_M 实测"),
            self._options([Option(label, id=option_id) for option_id, label in rows]),
        ]

    def _choose_context(self, value: str) -> None:
        _, ctx, cache = value.split(":", 2)
        self.preset.n_ctx = int(ctx)
        self.preset.cache_type_k = cache
        self.preset.cache_type_v = cache
        self._goto(4)

    # ------------------------------------------------------------ 第 5 步：图片

    def _build_image(self) -> list:
        options = [Option("不使用图片输入（纯文本，推荐）", id="image:none")]
        root = Path(self.preset.models_dir).expanduser()
        if root.is_dir():
            for path in sorted(root.glob("**/*mmproj*.gguf")):
                options.append(
                    Option(f"使用 {path.name}   {fmt_bytes(path.stat().st_size)}", id=f"image:{path}")
                )
        options.append(Option("手动输入 mmproj 路径…", id="image:manual"))
        current = f"当前：{self.preset.mmproj}" if self.preset.mmproj else "当前：未启用"
        return [
            self._label("图片（多模态投影）"),
            self._note(current),
            self._options(options),
        ]

    def _choose_image(self, value: str) -> None:
        if value == "image:none":
            self.preset.mmproj = ""
            self._goto(5)
            return
        if value == "image:manual":
            self._replace_with_input(
                "mmproj 文件完整路径（留空 = 不用）",
                self.preset.mmproj,
                "manual-mmproj",
                self._submit_mmproj,
            )
            return
        self.preset.mmproj = value.split(":", 1)[1]
        self._goto(5)

    def _submit_mmproj(self, raw: str) -> None:
        raw = raw.strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_file():
                self.notify(f"文件不存在：{path}", severity="error")
                return
            self.preset.mmproj = str(path)
        else:
            self.preset.mmproj = ""
        self._goto(5)

    # ------------------------------------------------------------ 第 6 步：并发

    def _build_concurrency(self) -> list:
        rows = [
            ("conc:1", "1 路 · inproc 引擎一个 context 一次只跑一个 decode，只有这一档"),
        ]
        return [
            self._label("并发路数"),
            self._note(
                f"当前：n_parallel={self.preset.n_parallel} / "
                f"max_concurrency={self.preset.max_concurrency}"
            ),
            self._note("进程内引擎只有一个 context：要真并发得给每个请求配一个 llama_context"),
            self._options([Option(label, id=option_id) for option_id, label in rows]),
        ]

    def _choose_concurrency(self, value: str) -> None:
        self.preset.n_parallel = 1
        self.preset.max_concurrency = 1
        self._goto(6)

    # ------------------------------------------------------------ 第 7 步：调优

    def _build_tuning(self) -> list:
        rows = [
            ("tune:balanced", "均衡 · batch 2048 / ubatch 1024 / FA on / KV 统一 · 推荐"),
            ("tune:conservative", "保守 · batch 512 / ubatch 512 / FA on / KV 统一 · 显存最省"),
            ("tune:prefill", "预填充优先 · batch 4096 / ubatch 2048 · 长 prompt 更快、更吃显存"),
        ]
        return [
            self._label("批处理与加速开关"),
            self._note(
                f"当前：batch {self.preset.n_batch} / ubatch {self.preset.n_ubatch} / "
                f"FA {'on' if self.preset.flash_attn else 'off'}"
            ),
            self._options([Option(label, id=option_id) for option_id, label in rows]),
        ]

    def _choose_tuning(self, value: str) -> None:
        if value == "tune:balanced":
            self.preset.n_batch, self.preset.n_ubatch = 2048, 1024
            self.preset.flash_attn = True
        elif value == "tune:conservative":
            self.preset.n_batch, self.preset.n_ubatch = 512, 512
            self.preset.flash_attn = True
        else:
            self.preset.n_batch, self.preset.n_ubatch = 4096, 2048
            self.preset.flash_attn = True
        self._goto(7)

    # ------------------------------------------------------------ 第 8 步：高级

    def _build_advanced(self) -> list:
        return [
            self._label("高级：服务日志"),
            self._note("监视器的 Logs 视图 tail 这个文件；原生默认 D:\\llm\\runtime\\service.log"),
            Static("服务日志路径", classes="field-label"),
            Input(value=self.preset.service_log_path, id="adv-log"),
            self._note("改完按 Enter 保存并进入下一步"),
        ]

    def _submit_advanced(self, widget_id: str, raw: str) -> None:
        self.preset.service_log_path = raw.strip() or self.preset.service_log_path
        self._goto(8)

    # ------------------------------------------------------------ 第 9 步：访问

    def _build_access(self) -> list:
        rows = [
            ("access:local", "仅本机 127.0.0.1:8000 · 推荐（WSL 会转发到 Windows）"),
            ("access:lan", "0.0.0.0:8000 · 局域网可访问（接口没有鉴权）"),
            ("access:custom", "自定义 host / 端口…"),
        ]
        return [
            self._label("对外访问方式"),
            self._note(f"当前：{self.preset.host}:{self.preset.port}"),
            self._options([Option(label, id=option_id) for option_id, label in rows]),
        ]

    def _choose_access(self, value: str) -> None:
        if value == "access:local":
            self.preset.host = "127.0.0.1"
            self.preset.port = 8000
            self._goto(9)
        elif value == "access:lan":
            self.preset.host = "0.0.0.0"
            self.preset.port = 8000
            self._goto(9)
        else:
            self._replace_with_input(
                "host 与端口，用冒号分隔（例如 0.0.0.0:8000）",
                f"{self.preset.host}:{self.preset.port}",
                "manual-access",
                self._submit_access,
            )

    def _submit_access(self, raw: str) -> None:
        host, _, port = raw.strip().rpartition(":")
        if not host or not port.isdigit():
            self.notify("格式应为 host:port，例如 127.0.0.1:8000", severity="error")
            return
        self.preset.host = host
        self.preset.port = int(port)
        self._goto(9)

    # ------------------------------------------------------------ 第 10 步：保存

    def _build_save(self) -> list:
        p = self.preset
        summary = [
            f"[dim]模型[/]      {p.model_stem}",
            f"[dim]文件夹[/]    {p.models_dir}",
            f"[dim]上下文[/]    {p.n_ctx:,} · KV {p.cache_type_k}/{p.cache_type_v}",
            f"[dim]图片[/]      {p.mmproj or '未启用'}",
            f"[dim]并发[/]      n_parallel={p.n_parallel} · max_concurrency={p.max_concurrency}",
            f"[dim]调优[/]      batch {p.n_batch}/{p.n_ubatch} · FA {'on' if p.flash_attn else 'off'}",
            f"[dim]引擎[/]      {p.engine}（进程内）· 日志 {p.service_log_path}",
            f"[dim]访问[/]      {p.host}:{p.port}",
        ]
        problems = p.problems()
        warns = p.warnings()
        lines = ["\n".join(summary)]
        if problems:
            lines.append("[red]" + "\n".join("✗ " + item for item in problems) + "[/]")
        if warns:
            lines.append("[yellow]" + "\n".join("! " + item for item in warns) + "[/]")
        options = [
            Option("保存并启动监视器", id="save:run"),
            Option("只保存预设（不启动）", id="save:only"),
            Option("返回上一步修改", id="save:back"),
        ]
        return [
            self._label("预设名称"),
            Input(value=p.name, id="preset-name"),
            Static(lines[0], classes="summary"),
            *(Static(line, classes="summary") for line in lines[1:]),
            self._options(options),
        ]

    def _choose_save(self, value: str) -> None:
        if value == "save:back":
            self._goto(8)
            return
        name_field = self.query_one("#preset-name", Input)
        name = name_field.value.strip() or "default"
        self.preset.name = name
        problems = self.preset.problems()
        if problems:
            self.notify("；".join(problems), severity="error", timeout=8)
            return
        try:
            json_path = self.preset.save()
            env_path = write_env_file(self.preset, container=True)
        except OSError as exc:
            self.notify(f"写文件失败：{exc}", severity="error")
            return
        self.launch_monitor = value == "save:run"
        self.notify(f"已保存 {json_path.name} / {env_path.name}", timeout=4)
        self.exit(self.preset)

    # ------------------------------------------------------------ 输入框替换

    def _replace_with_input(
        self,
        label: str,
        initial: str,
        widget_id: str,
        handler,
    ) -> None:
        body = self.query_one("#body", VerticalScroll)
        body.remove_children()
        body.mount_all(
            [
                self._label(label),
                self._note("Enter 确认 · Esc 返回上一屏"),
                Input(value=initial, id=widget_id),
            ]
        )
        self._pending_input = (widget_id, handler)
        self.call_after_refresh(self._focus_primary)

    # ------------------------------------------------------------ 事件

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        value = event.option.id or ""
        kind = value.split(":", 1)[0]
        handlers = {
            "preset": self._choose_preset,
            "model": self._choose_model,
            "ctx": self._choose_context,
            "image": self._choose_image,
            "conc": self._choose_concurrency,
            "tune": self._choose_tuning,
            "access": self._choose_access,
            "save": self._choose_save,
        }
        handler = handlers.get(kind)
        if handler:
            handler(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        widget_id = event.input.id or ""
        pending = getattr(self, "_pending_input", None)
        if pending and pending[0] == widget_id:
            self._pending_input = None
            pending[1](event.value)
            return
        if widget_id == "folder":
            self._submit_folder(event.value)
        elif widget_id.startswith("adv-"):
            self._submit_advanced(widget_id, event.value)
        elif widget_id == "preset-name":
            self.query_one("#choices", OptionList).focus()

    # ------------------------------------------------------------ 动作

    def action_back(self) -> None:
        if self.index == 0:
            self.exit(None)
            return
        self._goto(self.index - 1)

    def action_help(self) -> None:
        _, title, body = STEPS[self.index]
        self.push_screen(HelpScreen(title, body))

    def action_quit_wizard(self) -> None:
        self.exit(None)

    def _next(self) -> None:
        self._goto(self.index + 1)

    def _goto(self, index: int) -> None:
        self.index = max(0, min(len(STEPS) - 1, index))
        self._render()
