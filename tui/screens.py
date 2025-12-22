import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Input, RichLog, Static, Switch

from config import Machine
from create import (
    create_machine,
    purge_machine_config,
    rollback_machine_cores,
    rollback_machine_memory,
    rollback_machine_tags,
    update_machine_cores,
    update_machine_memory,
    update_machine_tags,
)
from checks import VmInfo, get_vm_info, ping
from proxmox import NodeInfo, get_node_info, get_vm_status, reboot_vm, shutdown_vm, start_vm, set_vm_config, set_vm_tags
from runner import Step, ansible_run, tf_apply_disk, tf_apply_vm, tf_destroy_disk, tf_destroy_vm

STEPS_STANDARD: dict[str, str] = {
    "shutdown": "Shutdown",
    "destroy":  "Terraform Destroy",
    "apply":    "Terraform Apply",
    "ansible":  "Ansible",
}

STEPS_STANDARD_WITH_DISK: dict[str, str] = {
    "shutdown":   "Shutdown",
    "destroy":    "Terraform Destroy",
    "apply_disk": "Apply Disk",
    "apply":      "Terraform Apply",
    "ansible":    "Ansible",
}

STEPS_FULL: dict[str, str] = {
    "shutdown":     "Shutdown",
    "destroy_vm":   "Destroy VM",
    "destroy_disk": "Destroy Disk",
    "apply_disk":   "Apply Disk",
    "apply_vm":     "Apply VM",
    "ansible":      "Ansible",
}

STEPS_PURGE: dict[str, str] = {
    "destroy_vm":    "Destroy VM",
    "remove_config": "Remove Config",
}

STEPS_PURGE_WITH_DISK: dict[str, str] = {
    "destroy_vm":    "Destroy VM",
    "destroy_disk":  "Destroy Disk",
    "remove_config": "Remove Config",
}


class ConfirmScreen(ModalScreen[str]):
    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 56;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #dialog Static {
        text-align: center;
        width: 100%;
    }
    #hint {
        margin-top: 2;
        color: $text-muted;
    }
    #full-hint {
        margin-top: 1;
        color: $warning;
    }
    """
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No"),
        Binding("f", "full_redeploy", "Full Redeploy"),
    ]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"Redeploy  [bold]{self._machine.key}[/bold]?")
            yield Static("The VM will be destroyed and recreated.\nData disk is not affected.")
            yield Static("\\[Y] confirm    \\[N] cancel", id="hint")
            if self._machine.data_disk:
                yield Static("\\[F] full redeploy including data disk", id="full-hint")

    def action_confirm(self) -> None:
        self.dismiss("standard")

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_full_redeploy(self) -> None:
        if not self._machine.data_disk:
            return

        def _on_danger(confirmed: bool) -> None:
            if confirmed:
                self.dismiss("full")

        d = self._machine.data_disk
        self.app.push_screen(DangerScreen(  # type: ignore[attr-defined]
            title="⚠   DESTRUCTIVE OPERATION   ⚠",
            info=(
                f"VM [bold]{self._machine.key}[/bold]  (VMID {self._machine.vmid})\n"
                f"Data disk [bold]{d.key}[/bold]  (VMID {d.vmid}, {d.size})\n"
                "will be [bold]permanently destroyed[/bold] and recreated."
            ),
            warning="ALL DATA ON THE DISK WILL BE LOST.",
        ), _on_danger)


class DangerScreen(ModalScreen[bool]):
    CSS = """
    DangerScreen {
        align: center middle;
    }
    #danger-dialog {
        width: 62;
        height: auto;
        padding: 2 4;
        border: thick $error;
        background: $surface;
    }
    #danger-title {
        text-align: center;
        width: 100%;
        color: $error;
        text-style: bold;
    }
    #danger-info {
        text-align: center;
        width: 100%;
        margin-top: 1;
    }
    #danger-warning {
        text-align: center;
        width: 100%;
        margin-top: 1;
        color: $error;
        text-style: bold;
    }
    #destroy-input {
        margin-top: 2;
    }
    #danger-hint {
        margin-top: 1;
        text-align: center;
        width: 100%;
        color: $text-muted;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, info: str, warning: str) -> None:
        super().__init__()
        self._title = title
        self._info = info
        self._warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="danger-dialog"):
            yield Static(self._title, id="danger-title")
            yield Static(self._info, id="danger-info")
            yield Static(self._warning, id="danger-warning")
            yield Input(placeholder="Type  DESTROY  to confirm", id="destroy-input")
            yield Static("\\[Enter] confirm    \\[Esc] cancel", id="danger-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == "DESTROY":
            self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def _step_text(state: str, label: str) -> Text:
    icons = {"waiting": "○", "running": "●", "done": "✓", "failed": "✗"}
    styles = {"waiting": "dim", "running": "bold yellow", "done": "bold green", "failed": "bold red"}
    icon = icons.get(state, "○")
    style = styles.get(state, "dim")
    t = Text()
    t.append(f" {icon}  ", style=style)
    t.append(label, style=style)
    return t


class DeployScreen(Screen):
    CSS = """
    #steps {
        height: 3;
        border-bottom: solid $surface-lighten-1;
        padding: 0 1;
    }
    #steps Static {
        width: 1fr;
        content-align: center middle;
        height: 3;
    }
    RichLog {
        height: 1fr;
        padding: 0 1;
    }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, machine: Machine, full_redeploy: bool = False) -> None:
        super().__init__()
        self._machine = machine
        self._full_redeploy = full_redeploy
        if full_redeploy:
            self._steps = STEPS_FULL
        elif machine.data_disk:
            self._steps = STEPS_STANDARD_WITH_DISK
        else:
            self._steps = STEPS_STANDARD
        self._proc: asyncio.subprocess.Process | None = None
        self._logfile = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="steps"):
            for step_id, label in self._steps.items():
                yield Static(_step_text("waiting", label), id=f"step_{step_id}")
        yield RichLog(id="log", highlight=False, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        mode = "Full Redeploy" if self._full_redeploy else "Redeploying"
        self.title = f"{mode}  {self._machine.key}"
        self.sub_title = ""
        log_path = f"/tmp/proxmox-{self._machine.key}.log"
        self._logfile = open(log_path, "w")
        self.query_one(RichLog).write(Text(f"Log: {log_path}", style="dim"))
        self.run_worker(self._run_deploy(), exclusive=True)

    def on_unmount(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        if self._logfile:
            self._logfile.close()
            self._logfile = None

    def action_close(self) -> None:
        self.app.pop_screen()

    def _set_step(self, step_id: str, state: str) -> None:
        self.query_one(f"#step_{step_id}", Static).update(
            _step_text(state, self._steps[step_id])
        )

    def _log(self, text: str, style: str = "") -> None:
        self.query_one(RichLog).write(Text(text, style=style) if style else text)
        if self._logfile:
            self._logfile.write(text + "\n")
            self._logfile.flush()

    async def _run_cmd(self, cmd: list[str], cwd: str, env: dict[str, str]) -> bool:
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        log = self.query_one(RichLog)
        async for raw in self._proc.stdout:  # type: ignore[union-attr]
            line = raw.decode(errors="replace").rstrip()
            log.write(Text.from_ansi(line))
            if self._logfile:
                self._logfile.write(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line) + "\n")
                self._logfile.flush()
        await self._proc.wait()
        rc = self._proc.returncode
        self._proc = None
        return rc == 0

    async def _run_step(self, step: Step) -> bool:
        for cmd in step.cmds:
            if not await self._run_cmd(cmd, step.cwd, step.env):
                return False
        return True

    async def _run_deploy(self) -> None:
        key = self._machine.key

        # Graceful shutdown if the VM is reachable
        self._set_step("shutdown", "running")
        self._log("\n── Shutdown ──", style="bold")
        is_up = await ping(self._machine.ip)
        if is_up:
            ok = await shutdown_vm(self._machine.vmid, self._machine.node)
            if not ok:
                self._set_step("shutdown", "failed")
                self._log("\nShutdown signal failed - press Esc to close.", style="bold red")
                return
            self._log("Waiting for VM to stop…")
            for _ in range(30):
                await asyncio.sleep(2)
                status = await get_vm_status(self._machine.vmid, self._machine.node)
                if status in ("stopped", "not found"):
                    break
            else:
                self._set_step("shutdown", "failed")
                self._log("\nVM did not stop within 60s - press Esc to close.", style="bold red")
                return
        self._set_step("shutdown", "done")

        if self._full_redeploy:
            disk_key = self._machine.data_disk.key  # type: ignore[union-attr]
            plan: list[tuple[str, Step]] = [
                ("destroy_vm",   tf_destroy_vm(key)),
                ("destroy_disk", tf_destroy_disk(disk_key)),
                ("apply_disk",   tf_apply_disk(disk_key)),
                ("apply_vm",     tf_apply_vm(key)),
                ("ansible",      ansible_run(key)),
            ]
        elif self._machine.data_disk:
            disk_key = self._machine.data_disk.key
            plan = [
                ("destroy",    tf_destroy_vm(key)),
                ("apply_disk", tf_apply_disk(disk_key)),
                ("apply",      tf_apply_vm(key)),
                ("ansible",    ansible_run(key)),
            ]
        else:
            plan = [
                ("destroy", tf_destroy_vm(key)),
                ("apply",   tf_apply_vm(key)),
                ("ansible", ansible_run(key)),
            ]

        for step_id, step in plan:
            self._set_step(step_id, "running")
            self._log(f"\n── {self._steps[step_id]} ──", style="bold")
            if await self._run_step(step):
                self._set_step(step_id, "done")
            else:
                self._set_step(step_id, "failed")
                self._log("\nDeploy failed - press Esc to close.", style="bold red")
                return

        self._log("\nDone - press Esc to close.", style="bold green")
        self.app._poll_all()  # type: ignore[attr-defined]


class AddMachineScreen(Screen):
    CSS = """
    #form-scroll {
        height: 1fr;
        padding: 2 4;
    }
    #form-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .row {
        height: 3;
        align: left middle;
    }
    .label {
        width: 18;
        content-align: left middle;
        height: 3;
    }
    .inp {
        width: 26;
    }
    .hint {
        width: 1fr;
        content-align: left middle;
        height: 3;
        color: $text-muted;
        padding-left: 1;
    }
    #data-size-row {
        display: none;
    }
    #form-error {
        color: $error;
        margin-top: 1;
        height: auto;
    }
    #form-success {
        color: $success;
        margin-top: 1;
        height: auto;
        display: none;
    }
    #form-actions {
        margin-top: 2;
        color: $text-muted;
    }
    """
    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit"),
        Binding("escape", "close", "Cancel"),
    ]

    def __init__(self, machines: list[Machine]) -> None:
        super().__init__()
        self._existing = machines
        self._created = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form-scroll"):
            yield Static("Add Machine", id="form-title")
            with Horizontal(classes="row"):
                yield Static("Name", classes="label")
                yield Input(placeholder="my_machine", id="inp-name", classes="inp")
                yield Static("a-z and _ only", classes="hint")
            with Horizontal(classes="row"):
                yield Static("VMID", classes="label")
                yield Input(placeholder="8220", id="inp-vmid", classes="inp")
                yield Static("", id="vmid-hint", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Max memory (MB)", classes="label")
                yield Input(placeholder="2048", id="inp-memory", classes="inp")
                yield Static("", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Min memory (MB)", classes="label")
                yield Input(placeholder="512", id="inp-balloon", classes="inp")
                yield Static("balloon floor", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Cores", classes="label")
                yield Input(placeholder="2", id="inp-cores", classes="inp")
                yield Static("", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Boot disk size", classes="label")
                yield Input(placeholder="16G", id="inp-boot-size", classes="inp")
                yield Static("e.g. 16G, 32G", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Extra tags", classes="label")
                yield Input(placeholder="optional", id="inp-tags", classes="inp")
                yield Static("added to 'vm'", classes="hint")
            with Horizontal(classes="row"):
                yield Static("Data disk", classes="label")
                yield Switch(id="data-disk-switch", value=False)
            with Horizontal(classes="row", id="data-size-row"):
                yield Static("Data disk size", classes="label")
                yield Input(placeholder="512G", id="inp-data-size", classes="inp")
                yield Static("e.g. 512G, 1T", classes="hint")
            yield Static("", id="form-error")
            yield Static("", id="form-success")
            yield Static("\\[Ctrl+S] submit    \\[Esc] cancel", id="form-actions")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Add Machine"
        self.sub_title = ""
        self.query_one("#inp-name", Input).focus()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.query_one("#data-size-row").display = event.value

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "inp-vmid":
            try:
                vmid = int(event.value.strip())
                ip_last = vmid % 1000
                if 201 <= ip_last <= 254:
                    self.query_one("#vmid-hint", Static).update(
                        f"→ 192.168.1.{ip_last}  /  data {vmid + 300}"
                    )
                    return
            except ValueError:
                pass
            self.query_one("#vmid-hint", Static).update("")

    def _error(self, msg: str) -> None:
        self.query_one("#form-error", Static).update(msg)

    def action_submit(self) -> None:
        if self._created:
            return

        name = self.query_one("#inp-name", Input).value.strip()
        vmid_raw = self.query_one("#inp-vmid", Input).value.strip()
        memory_raw = self.query_one("#inp-memory", Input).value.strip()
        balloon_raw = self.query_one("#inp-balloon", Input).value.strip()
        cores_raw = self.query_one("#inp-cores", Input).value.strip()
        boot_size = self.query_one("#inp-boot-size", Input).value.strip()
        extra_tags = self.query_one("#inp-tags", Input).value.strip()
        has_data = self.query_one("#data-disk-switch", Switch).value
        data_size = self.query_one("#inp-data-size", Input).value.strip()

        if not name:
            self._error("Name is required.")
            return
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            self._error("Name must start with a-z and contain only a-z and _.")
            return
        if name in {m.key for m in self._existing}:
            self._error(f"Name '{name}' is already in use.")
            return

        try:
            vmid = int(vmid_raw)
        except ValueError:
            self._error("VMID must be an integer.")
            return
        if vmid < 100:
            self._error("VMID must be >= 100.")
            return
        ip_last = vmid % 1000
        if ip_last < 201 or ip_last > 254:
            self._error(f"VMID {vmid} maps to IP .{ip_last} - must be 201-254 (.200 is the host, .255 is max).")
            return
        if vmid in {m.vmid for m in self._existing}:
            self._error(f"VMID {vmid} is already in use.")
            return
        if has_data:
            data_vmid = vmid + 300
            if data_vmid in {m.data_disk.vmid for m in self._existing if m.data_disk}:
                self._error(f"Data VMID {data_vmid} is already in use.")
                return

        try:
            memory = int(memory_raw)
            assert memory >= 16
        except (ValueError, AssertionError):
            self._error("Memory must be >= 16 MB.")
            return

        try:
            balloon = int(balloon_raw)
            assert 16 <= balloon <= memory
        except (ValueError, AssertionError):
            self._error(f"Min memory must be >= 16 MB and <= max memory ({memory} MB).")
            return

        try:
            cores = int(cores_raw)
            assert cores >= 1
        except (ValueError, AssertionError):
            self._error("Cores must be >= 1.")
            return

        if not boot_size:
            self._error("Boot disk size is required (e.g. 16G).")
            return
        if has_data and not data_size:
            self._error("Data disk size is required (e.g. 512G).")
            return

        try:
            create_machine(
                key=name,
                vmid=vmid,
                memory=memory,
                minimum_memory=balloon,
                cores=cores,
                boot_disk_size=boot_size,
                extra_tags=extra_tags,
                data_disk=has_data,
                data_disk_size=data_size,
            )
        except Exception as exc:
            self._error(f"Failed: {exc}")
            return

        self._created = True
        self._error("")
        success = self.query_one("#form-success", Static)
        success.display = True
        success.update(f"✓  '{name}' created - press Esc to close.")
        self.query_one("#form-actions", Static).update("")

    def action_close(self) -> None:
        self.dismiss(self._created)


class PurgeScreen(Screen):
    CSS = """
    #steps {
        height: 3;
        border-bottom: solid $surface-lighten-1;
        padding: 0 1;
    }
    #steps Static {
        width: 1fr;
        content-align: center middle;
        height: 3;
    }
    RichLog {
        height: 1fr;
        padding: 0 1;
    }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine
        self._steps = STEPS_PURGE_WITH_DISK if machine.data_disk else STEPS_PURGE
        self._proc: asyncio.subprocess.Process | None = None
        self._logfile = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="steps"):
            for step_id, label in self._steps.items():
                yield Static(_step_text("waiting", label), id=f"step_{step_id}")
        yield RichLog(id="log", highlight=False, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Purging  {self._machine.key}"
        self.sub_title = ""
        log_path = f"/tmp/proxmox-{self._machine.key}.log"
        self._logfile = open(log_path, "w")
        self.query_one(RichLog).write(Text(f"Log: {log_path}", style="dim"))
        self.run_worker(self._run_purge(), exclusive=True)

    def on_unmount(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        if self._logfile:
            self._logfile.close()
            self._logfile = None

    def action_close(self) -> None:
        self.app.pop_screen()

    def _set_step(self, step_id: str, state: str) -> None:
        self.query_one(f"#step_{step_id}", Static).update(
            _step_text(state, self._steps[step_id])
        )

    def _log(self, text: str, style: str = "") -> None:
        self.query_one(RichLog).write(Text(text, style=style) if style else text)
        if self._logfile:
            self._logfile.write(text + "\n")
            self._logfile.flush()

    async def _run_cmd(self, cmd: list[str], cwd: str, env: dict[str, str]) -> bool:
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        log = self.query_one(RichLog)
        async for raw in self._proc.stdout:  # type: ignore[union-attr]
            line = raw.decode(errors="replace").rstrip()
            log.write(Text.from_ansi(line))
            if self._logfile:
                self._logfile.write(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line) + "\n")
                self._logfile.flush()
        await self._proc.wait()
        rc = self._proc.returncode
        self._proc = None
        return rc == 0

    async def _run_step(self, step: Step) -> bool:
        for cmd in step.cmds:
            if not await self._run_cmd(cmd, step.cwd, step.env):
                return False
        return True

    async def _run_purge(self) -> None:
        key = self._machine.key

        plan: list[tuple[str, Step]] = [("destroy_vm", tf_destroy_vm(key))]
        if self._machine.data_disk:
            plan.append(("destroy_disk", tf_destroy_disk(self._machine.data_disk.key)))

        for step_id, step in plan:
            self._set_step(step_id, "running")
            self._log(f"\n── {self._steps[step_id]} ──", style="bold")
            if not await self._run_step(step):
                self._set_step(step_id, "failed")
                self._log("\nFailed - press Esc to close.", style="bold red")
                return
            self._set_step(step_id, "done")

        self._set_step("remove_config", "running")
        self._log("\n── Remove Config ──", style="bold")
        try:
            purge_machine_config(key, self._machine.data_disk is not None)
            self._set_step("remove_config", "done")
        except Exception as exc:
            self._set_step("remove_config", "failed")
            self._log(f"\nConfig removal failed: {exc}", style="bold red")
            self._log("\nPress Esc to close.", style="bold red")
            return

        self._log("\nPurge complete - press Esc to close.", style="bold green")


# ── host info helpers ────────────────────────────────────────────────────────

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_uptime(seconds: int) -> str:
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _bar(ratio: float, width: int = 24) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    color = "green" if ratio < 0.75 else ("yellow" if ratio < 0.90 else "red")
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (width - filled)}[/dim]"


def _render_node_info(info: NodeInfo) -> str:
    lines: list[str] = []

    lines.append(
        f"[dim]Node[/dim]    {info.node}"
        f"    [dim]Uptime[/dim]  {_fmt_uptime(info.uptime)}"
        f"    [dim]PVE[/dim]  {info.pve_version}"
    )
    lines.append(f"[dim]Kernel[/dim]  {info.kernel_version}")

    # CPU
    lines.append("")
    lines.append("[bold]CPU[/bold]")
    lines.append(f"  [dim]Model[/dim]    {info.cpu_model}")
    mhz = f" @ {float(info.cpu_mhz) / 1000:.2f} GHz" if info.cpu_mhz else ""
    lines.append(f"  [dim]Cores[/dim]    {info.cpu_cores} cores / {info.cpu_threads} threads{mhz}")
    lines.append(
        f"  [dim]Usage[/dim]    {info.cpu_usage * 100:.1f}%  {_bar(info.cpu_usage)}"
    )
    la = info.load_avg
    lines.append(f"  [dim]Load avg[/dim] {la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}")

    # Memory
    lines.append("")
    lines.append("[bold]Memory[/bold]")
    mem_pct = info.mem_used / info.mem_total if info.mem_total else 0.0
    lines.append(f"  [dim]Total[/dim]    {_fmt_bytes(info.mem_total)}")
    lines.append(
        f"  [dim]Used[/dim]     {_fmt_bytes(info.mem_used)}  ({mem_pct * 100:.1f}%)  {_bar(mem_pct)}"
    )
    lines.append(f"  [dim]Free[/dim]     {_fmt_bytes(info.mem_total - info.mem_used)}")
    if info.swap_total:
        swap_pct = info.swap_used / info.swap_total
        lines.append(
            f"  [dim]Swap[/dim]     {_fmt_bytes(info.swap_used)} / {_fmt_bytes(info.swap_total)}"
            f"  ({swap_pct * 100:.1f}%)"
        )

    # Storage
    if info.storages:
        lines.append("")
        lines.append("[bold]Storage[/bold]")
        for st in info.storages:
            used_pct = st.used / st.total if st.total else 0.0
            alloc_pct = st.allocated / st.total if st.total else 0.0
            lines.append(f"  [dim]{st.name}[/dim]")
            lines.append(
                f"    [dim]Total[/dim]      {_fmt_bytes(st.total)}"
            )
            lines.append(
                f"    [dim]Used[/dim]       {_fmt_bytes(st.used)}  ({used_pct * 100:.1f}%)  {_bar(used_pct)}"
            )
            lines.append(
                f"    [dim]Allocated[/dim]  {_fmt_bytes(st.allocated)}  ({alloc_pct * 100:.1f}%)  {_bar(alloc_pct)}"
            )
            lines.append(f"    [dim]Avail[/dim]      {_fmt_bytes(st.avail)}")

    # VMs
    lines.append("")
    lines.append("[bold]Virtual Machines[/bold]")
    stopped = info.vm_total - info.vm_running
    lines.append(
        f"  {info.vm_total} total"
        f"    [green]{info.vm_running} running[/green]"
        f"    [dim]{stopped} stopped[/dim]"
    )

    return "\n".join(lines)


class HostInfoScreen(ModalScreen):
    CSS = """
    HostInfoScreen { align: center middle; }
    #panel {
        width: 74;
        height: auto;
        max-height: 90vh;
        padding: 1 3;
        border: solid $accent;
        background: $surface;
        overflow-y: auto;
    }
    #panel-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #panel-hint {
        text-align: center;
        color: $text-muted;
        margin-top: 2;
    }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, node: str = "pve") -> None:
        super().__init__()
        self._node = node

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static("Proxmox Host", id="panel-title")
            yield Static("[dim]Loading…[/dim]", id="panel-content")
            yield Static("\\[R] refresh    \\[Esc] close    auto-refresh 10s", id="panel-hint")

    def on_mount(self) -> None:
        self.run_worker(self._fetch(), exclusive=True, group="host_info")
        self.set_interval(10, self.action_refresh)

    def action_refresh(self) -> None:
        self.query_one("#panel-content", Static).update("[dim]Loading…[/dim]")
        self.run_worker(self._fetch(), exclusive=True, group="host_info")

    def action_close(self) -> None:
        self.dismiss(None)

    async def _fetch(self) -> None:
        info = await get_node_info(self._node)
        if info is None:
            self.query_one("#panel-content", Static).update(
                "[bold red]Failed to fetch host info.[/bold red]"
            )
            return
        self.query_one("#panel-content", Static).update(_render_node_info(info))


def _render_vm_info(machine: "Machine", info: VmInfo) -> str:
    lines: list[str] = []
    lines.append(
        f"[dim]Host[/dim]  {machine.key}  ({machine.ip})"
        f"    [dim]Uptime[/dim]  {_fmt_uptime(int(info.uptime))}"
    )

    lines.append("")
    lines.append("[bold]CPU[/bold]")
    lines.append(f"  [dim]Cores[/dim]    {info.cpus}")
    la = info.load_avg
    lines.append(f"  [dim]Load avg[/dim] {la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}")

    lines.append("")
    lines.append("[bold]Memory[/bold]")
    mem_pct = info.mem_used / info.mem_total if info.mem_total else 0.0
    lines.append(f"  [dim]Total[/dim]    {_fmt_bytes(info.mem_total)}")
    lines.append(
        f"  [dim]Used[/dim]     {_fmt_bytes(info.mem_used)}  ({mem_pct * 100:.1f}%)  {_bar(mem_pct)}"
    )
    lines.append(f"  [dim]Free[/dim]     {_fmt_bytes(info.mem_total - info.mem_used)}")
    if info.swap_total:
        swap_pct = info.swap_used / info.swap_total
        lines.append(
            f"  [dim]Swap[/dim]     {_fmt_bytes(info.swap_used)} / {_fmt_bytes(info.swap_total)}"
            f"  ({swap_pct * 100:.1f}%)"
        )

    if info.disks:
        lines.append("")
        lines.append("[bold]Disk[/bold]")
        for disk in info.disks:
            used_pct = disk.used / disk.total if disk.total else 0.0
            lines.append(f"  [dim]{disk.mount}[/dim]")
            lines.append(f"    [dim]Total[/dim]  {_fmt_bytes(disk.total)}")
            lines.append(
                f"    [dim]Used[/dim]   {_fmt_bytes(disk.used)}  ({used_pct * 100:.1f}%)  {_bar(used_pct)}"
            )
            lines.append(f"    [dim]Avail[/dim]  {_fmt_bytes(disk.avail)}")

    return "\n".join(lines)


class VmInfoScreen(ModalScreen):
    CSS = """
    VmInfoScreen { align: center middle; }
    #panel {
        width: 74;
        height: auto;
        max-height: 90vh;
        padding: 1 3;
        border: solid $accent;
        background: $surface;
        overflow-y: auto;
    }
    #panel-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #panel-hint {
        text-align: center;
        color: $text-muted;
        margin-top: 2;
    }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, machine: "Machine") -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static(f"VM  {self._machine.key}", id="panel-title")
            yield Static("[dim]Loading…[/dim]", id="panel-content")
            yield Static("\\[R] refresh    \\[Esc] close    auto-refresh 10s", id="panel-hint")

    def on_mount(self) -> None:
        self.run_worker(self._fetch(), exclusive=True, group="vm_info")
        self.set_interval(10, self.action_refresh)

    def action_refresh(self) -> None:
        self.query_one("#panel-content", Static).update("[dim]Loading…[/dim]")
        self.run_worker(self._fetch(), exclusive=True, group="vm_info")

    def action_close(self) -> None:
        self.dismiss(None)

    async def _fetch(self) -> None:
        info = await get_vm_info(self._machine.ip)
        if info is None:
            self.query_one("#panel-content", Static).update(
                "[bold red]Failed to fetch VM info.[/bold red]"
            )
            return
        self.query_one("#panel-content", Static).update(_render_vm_info(self._machine, info))


class EditTagsScreen(ModalScreen[bool]):
    CSS = """
    EditTagsScreen { align: center middle; }
    #tags-dialog {
        width: 60;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #tags-dialog Static { width: 100%; }
    #tags-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }
    #tags-format {
        margin-top: 1;
        color: $text-muted;
    }
    #tags-status { margin-top: 1; height: auto; }
    #tags-actions {
        margin-top: 2;
        text-align: center;
        color: $text-muted;
    }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine
        self._updated = False

    def compose(self) -> ComposeResult:
        extra = ",".join(
            t for t in (t.strip() for t in self._machine.tags.split(","))
            if t and t != "vm"
        )
        with Vertical(id="tags-dialog"):
            yield Static(f"Edit tags - [bold]{self._machine.key}[/bold]", id="tags-title")
            yield Input(value=extra, id="tags-input")
            yield Static("[dim]extra tags, comma-separated[/dim]", id="tags-format")
            yield Static("", id="tags-status")
            yield Static("\\[Enter] apply    \\[Esc] cancel", id="tags-actions")

    def on_mount(self) -> None:
        inp = self.query_one("#tags-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._apply(event.value.strip()), exclusive=True)

    def _set_status(self, msg: str, style: str = "") -> None:
        self.query_one("#tags-status", Static).update(
            f"[{style}]{msg}[/{style}]" if style else msg
        )

    async def _apply(self, raw: str) -> None:
        extras = [t for t in (t.strip() for t in raw.split(",")) if t and t != "vm"]
        new_main = ",".join(["vm"] + extras)
        new_data = _derive_data_tags(new_main) if self._machine.data_disk else None

        # 1. Update config files
        try:
            old_main, old_data = update_machine_tags(
                self._machine.key, new_main, self._machine.data_disk is not None
            )
        except Exception as exc:
            self._set_status(f"Config update failed: {exc}", "bold red")
            return

        # 2. Update live VMs (None = not deployed yet, treated as success)
        d = self._machine.data_disk
        res_main = await set_vm_tags(self._machine.vmid, self._machine.node, new_main)
        res_data = (
            await set_vm_tags(d.vmid, d.node, new_data)  # type: ignore[arg-type]
            if d and new_data
            else None
        )

        failed = [
            label
            for label, res in ((self._machine.key, res_main), (d.key if d else None, res_data))
            if label and res is False
        ]

        if failed:
            # 3. Roll back config
            rb_ok = True
            try:
                rollback_machine_tags(self._machine.key, old_main, old_data)
            except Exception:
                rb_ok = False

            # Best-effort revert any successful API calls
            if res_main is True:
                await set_vm_tags(self._machine.vmid, self._machine.node, old_main)
            if res_data is True and d and old_data:
                await set_vm_tags(d.vmid, d.node, old_data)

            note = "Config rolled back." if rb_ok else "Config rollback failed - check files."
            self._set_status(
                f"API failed for: {', '.join(failed)}. {note}", "bold red"
            )
            return

        self._updated = True
        self._set_status("Tags updated.", "bold green")
        self.query_one("#tags-actions", Static).update("\\[Esc] close")

    def action_cancel(self) -> None:
        self.dismiss(self._updated)


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen { align: center middle; }
    #help-dialog {
        width: 48;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #help-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .help-row {
        height: 1;
    }
    .help-key {
        width: 6;
        color: $accent;
    }
    .help-desc {
        width: 1fr;
        color: $text;
    }
    #help-hint {
        margin-top: 2;
        text-align: center;
        color: $text-muted;
    }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    _COMMANDS = [
        ("a",   "Add machine"),
        ("d",   "Redeploy"),
        ("s",   "Start"),
        ("o",   "Shut down"),
        ("r",   "Reboot"),
        ("t",   "Edit tags"),
        ("c",   "Edit cores"),
        ("m",   "Edit memory"),
        ("p",   "Purge machine"),
        ("v",   "VM info"),
        ("i",   "Host info"),
        ("q",   "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("Commands", id="help-title")
            for key, desc in self._COMMANDS:
                with Horizontal(classes="help-row"):
                    yield Static(key, classes="help-key")
                    yield Static(desc, classes="help-desc")
            yield Static("\\[Esc] close", id="help-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class RebootConfirmScreen(ModalScreen[bool]):
    CSS = """
    RebootConfirmScreen {
        align: center middle;
    }
    #reboot-dialog {
        width: 46;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #reboot-dialog Static {
        text-align: center;
        width: 100%;
    }
    #reboot-hint {
        margin-top: 2;
        color: $text-muted;
    }
    #reboot-status {
        margin-top: 1;
        height: auto;
    }
    """
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No"),
    ]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with Vertical(id="reboot-dialog"):
            yield Static(f"Reboot  [bold]{self._machine.key}[/bold]?")
            yield Static("\\[Y] confirm    \\[N] cancel", id="reboot-hint")
            yield Static("", id="reboot-status")

    def action_confirm(self) -> None:
        self.query_one("#reboot-hint", Static).update("")
        self.query_one("#reboot-status", Static).update("[bold yellow]Rebooting…[/bold yellow]")
        self.run_worker(self._reboot(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _reboot(self) -> None:
        ok = await reboot_vm(self._machine.vmid, self._machine.node)
        status = self.query_one("#reboot-status", Static)
        if ok:
            status.update("[bold green]Reboot triggered.[/bold green]")
        else:
            status.update("[bold red]Reboot failed (VM may not be running).[/bold red]")
        self.query_one("#reboot-hint", Static).update("\\[Esc] close")
        self.BINDINGS = [Binding("escape", "cancel", "Close")]  # type: ignore[assignment]


class StartConfirmScreen(ModalScreen[bool]):
    CSS = """
    StartConfirmScreen {
        align: center middle;
    }
    #start-dialog {
        width: 46;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #start-dialog Static {
        text-align: center;
        width: 100%;
    }
    #start-hint {
        margin-top: 2;
        color: $text-muted;
    }
    #start-status {
        margin-top: 1;
        height: auto;
    }
    """
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No"),
    ]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with Vertical(id="start-dialog"):
            yield Static(f"Start  [bold]{self._machine.key}[/bold]?")
            yield Static("\\[Y] confirm    \\[N] cancel", id="start-hint")
            yield Static("", id="start-status")

    def action_confirm(self) -> None:
        self.query_one("#start-hint", Static).update("")
        self.query_one("#start-status", Static).update("[bold yellow]Starting…[/bold yellow]")
        self.run_worker(self._start(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _start(self) -> None:
        ok = await start_vm(self._machine.vmid, self._machine.node)
        status = self.query_one("#start-status", Static)
        if ok:
            status.update("[bold green]Start triggered.[/bold green]")
        else:
            status.update("[bold red]Start failed (VM may already be running).[/bold red]")
        self.query_one("#start-hint", Static).update("\\[Esc] close")
        self.BINDINGS = [Binding("escape", "cancel", "Close")]  # type: ignore[assignment]


class ShutdownConfirmScreen(ModalScreen[bool]):
    CSS = """
    ShutdownConfirmScreen {
        align: center middle;
    }
    #shutdown-dialog {
        width: 46;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #shutdown-dialog Static {
        text-align: center;
        width: 100%;
    }
    #shutdown-hint {
        margin-top: 2;
        color: $text-muted;
    }
    #shutdown-status {
        margin-top: 1;
        height: auto;
    }
    """
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "No"),
    ]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with Vertical(id="shutdown-dialog"):
            yield Static(f"Shut down  [bold]{self._machine.key}[/bold]?")
            yield Static("\\[Y] confirm    \\[N] cancel", id="shutdown-hint")
            yield Static("", id="shutdown-status")

    def action_confirm(self) -> None:
        self.query_one("#shutdown-hint", Static).update("")
        self.query_one("#shutdown-status", Static).update("[bold yellow]Shutting down…[/bold yellow]")
        self.run_worker(self._shutdown(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _shutdown(self) -> None:
        ok = await shutdown_vm(self._machine.vmid, self._machine.node)
        status = self.query_one("#shutdown-status", Static)
        if ok:
            status.update("[bold green]Shutdown triggered.[/bold green]")
        else:
            status.update("[bold red]Shutdown failed (VM may not be running).[/bold red]")
        self.query_one("#shutdown-hint", Static).update("\\[Esc] close")
        self.BINDINGS = [Binding("escape", "cancel", "Close")]  # type: ignore[assignment]


def _derive_data_tags(main_tags: str) -> str:
    extras = [t.strip() for t in main_tags.split(",") if t.strip() and t.strip() != "vm"]
    return ",".join(["data"] + extras)


class EditCoresScreen(ModalScreen[bool]):
    CSS = """
    EditCoresScreen { align: center middle; }
    #cores-dialog {
        width: 56;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #cores-dialog Static { width: 100%; }
    #cores-title { text-align: center; text-style: bold; margin-bottom: 1; }
    #cores-status { margin-top: 1; height: auto; }
    #cores-actions { margin-top: 2; text-align: center; color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine
        self._updated = False
        self._applied = False

    def compose(self) -> ComposeResult:
        with Vertical(id="cores-dialog"):
            yield Static(f"Edit cores - [bold]{self._machine.key}[/bold]", id="cores-title")
            yield Input(value=str(self._machine.cores) if self._machine.cores else "", id="cores-input")
            yield Static("", id="cores-status")
            yield Static("\\[Enter] apply    \\[Esc] cancel", id="cores-actions")

    def on_mount(self) -> None:
        inp = self.query_one("#cores-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._applied:
            self.run_worker(self._apply(event.value.strip()), exclusive=True)

    def on_key(self, event) -> None:  # type: ignore[override]
        if self._applied and event.key == "y":
            event.stop()
            self._applied = False
            self.run_worker(self._reboot(), exclusive=True)

    def _set_status(self, msg: str, style: str = "") -> None:
        self.query_one("#cores-status", Static).update(
            f"[{style}]{msg}[/{style}]" if style else msg
        )

    async def _apply(self, raw: str) -> None:
        try:
            cores = int(raw)
            assert cores >= 1
        except (ValueError, AssertionError):
            self._set_status("Cores must be an integer >= 1.", "bold red")
            return

        try:
            old_cores = update_machine_cores(self._machine.key, cores)
        except Exception as exc:
            self._set_status(f"Config update failed: {exc}", "bold red")
            return

        res = await set_vm_config(self._machine.vmid, self._machine.node, cores=cores)
        if res is False:
            try:
                rollback_machine_cores(self._machine.key, old_cores)
            except Exception:
                pass
            self._set_status("API update failed - config rolled back.", "bold red")
            return

        self._updated = True
        self._applied = True
        self.query_one("#cores-input", Input).disabled = True
        self._set_status("✓ Cores updated.", "bold green")
        self.query_one("#cores-actions", Static).update(
            "\\[Y] reboot now    \\[Esc] close"
        )

    async def _reboot(self) -> None:
        self._set_status("Rebooting…", "bold yellow")
        ok = await reboot_vm(self._machine.vmid, self._machine.node)
        if ok:
            self._set_status("Reboot triggered.", "bold green")
        else:
            self._set_status("Reboot failed (VM may not be running).", "bold red")

    def action_close(self) -> None:
        self.dismiss(self._updated)


class EditMemoryScreen(ModalScreen[bool]):
    CSS = """
    EditMemoryScreen { align: center middle; }
    #mem-dialog {
        width: 60;
        height: auto;
        padding: 2 4;
        border: solid $accent;
        background: $surface;
    }
    #mem-dialog Static { width: 100%; }
    #mem-title { text-align: center; text-style: bold; margin-bottom: 1; }
    .mem-row { height: 3; align: left middle; }
    .mem-label { width: 20; content-align: left middle; height: 3; }
    .mem-inp { width: 16; }
    #mem-status { margin-top: 1; height: auto; }
    #mem-actions { margin-top: 2; text-align: center; color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine
        self._updated = False
        self._applied = False

    def compose(self) -> ComposeResult:
        with Vertical(id="mem-dialog"):
            yield Static(f"Edit memory - [bold]{self._machine.key}[/bold]", id="mem-title")
            with Horizontal(classes="mem-row"):
                yield Static("Max memory (MB)", classes="mem-label")
                yield Input(
                    value=str(self._machine.memory) if self._machine.memory else "",
                    id="mem-max-input",
                    classes="mem-inp",
                )
            with Horizontal(classes="mem-row"):
                yield Static("Min memory (MB)", classes="mem-label")
                yield Input(
                    value=str(self._machine.minimum_memory) if self._machine.minimum_memory else "",
                    id="mem-min-input",
                    classes="mem-inp",
                )
            yield Static("", id="mem-status")
            yield Static("\\[Enter] apply    \\[Esc] cancel", id="mem-actions")

    def on_mount(self) -> None:
        inp = self.query_one("#mem-max-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._applied:
            return
        if event.input.id == "mem-max-input":
            self.query_one("#mem-min-input", Input).focus()
        else:
            self._submit()

    def on_key(self, event) -> None:  # type: ignore[override]
        if self._applied and event.key == "y":
            event.stop()
            self._applied = False
            self.run_worker(self._reboot(), exclusive=True)

    def _submit(self) -> None:
        self.run_worker(
            self._apply(
                self.query_one("#mem-max-input", Input).value.strip(),
                self.query_one("#mem-min-input", Input).value.strip(),
            ),
            exclusive=True,
        )

    def _set_status(self, msg: str, style: str = "") -> None:
        self.query_one("#mem-status", Static).update(
            f"[{style}]{msg}[/{style}]" if style else msg
        )

    async def _apply(self, max_raw: str, min_raw: str) -> None:
        try:
            memory = int(max_raw)
            assert memory >= 16
        except (ValueError, AssertionError):
            self._set_status("Max memory must be >= 16 MB.", "bold red")
            return

        try:
            minimum = int(min_raw)
            assert 16 <= minimum <= memory
        except (ValueError, AssertionError):
            self._set_status(f"Min memory must be >= 16 MB and <= max ({memory} MB).", "bold red")
            return

        try:
            old_memory, old_minimum = update_machine_memory(self._machine.key, memory, minimum)
        except Exception as exc:
            self._set_status(f"Config update failed: {exc}", "bold red")
            return

        res = await set_vm_config(
            self._machine.vmid, self._machine.node,
            memory=memory, balloon=minimum,
        )
        if res is False:
            try:
                rollback_machine_memory(self._machine.key, old_memory, old_minimum)
            except Exception:
                pass
            self._set_status("API update failed - config rolled back.", "bold red")
            return

        self._updated = True
        self._applied = True
        self.query_one("#mem-max-input", Input).disabled = True
        self.query_one("#mem-min-input", Input).disabled = True
        self._set_status("✓ Memory updated.", "bold green")
        self.query_one("#mem-actions", Static).update(
            "\\[Y] reboot now    \\[Esc] close"
        )

    async def _reboot(self) -> None:
        self._set_status("Rebooting…", "bold yellow")
        ok = await reboot_vm(self._machine.vmid, self._machine.node)
        if ok:
            self._set_status("Reboot triggered.", "bold green")
        else:
            self._set_status("Reboot failed (VM may not be running).", "bold red")

    def action_close(self) -> None:
        self.dismiss(self._updated)
