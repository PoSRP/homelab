import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from checks import get_sentinel, ping
from config import DataDisk, Machine, load_machines
from proxmox import get_vm_status
from screens import AddMachineScreen, ConfirmScreen, DeployScreen, DangerScreen, EditCoresScreen, EditMemoryScreen, EditTagsScreen, HelpScreen, HostInfoScreen, PurgeScreen, RebootConfirmScreen, ShutdownConfirmScreen, StartConfirmScreen, VmInfoScreen

# Leading space on every cell gives a consistent left margin within each column,
# so adjacent columns don't visually bleed into each other.
_PAD = " "


def _vm_cell(status: str) -> Text:
    if status == "running":
        return Text(_PAD + "●", style="bold green")
    if status == "stopped":
        return Text(_PAD + "●", style="yellow")
    return Text(_PAD + "●", style="bold red")


def _disk_status_cell(disk: DataDisk | None, status: str) -> Text:
    if disk is None:
        return Text(_PAD + "-", style="dim")
    color = "bold green" if status == "stopped" else "bold red"
    return Text(_PAD + "●", style=color)


def _ping_cell(up: bool) -> Text:
    return Text(_PAD + "up", style="green") if up else Text(_PAD + "down", style="bold red")


def _sentinel_cell(value: str | None) -> Text:
    if value:
        return Text(_PAD + value)
    return Text(_PAD + "never", style="dim")


class StatusApp(App):
    TITLE = "Proxmox"
    COMMANDS = frozenset()
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Horizontal { height: 1fr; }
    DataTable  { width: 1fr; }
    #arrow {
        width: 3;
        content-align: center middle;
        color: $accent;
    }
    """
    BINDINGS = [
        Binding("a", "add_machine", "Add",      show=False),
        Binding("d", "deploy",      "Redeploy", show=False),
        Binding("s", "start",       "Start",     show=False),
        Binding("o", "shutdown",    "Shut down", show=False),
        Binding("r", "reboot",      "Reboot",   show=False),
        Binding("t", "edit_tags",   "Tags",     show=False),
        Binding("c", "edit_cores",  "Cores",    show=False),
        Binding("m", "edit_memory", "Memory",   show=False),
        Binding("p", "purge",       "Purge",    show=False),
        Binding("v", "vm_info",     "VM info",  show=False),
        Binding("i", "host_info",   "Host info", show=False),
        Binding("q", "quit",        "Quit",     show=False),
        Binding("?", "help",        "Commands"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(zebra_stripes=True, cursor_type="row")
            yield Static("", id="arrow")
        yield Footer()

    def on_mount(self) -> None:
        self._machines = load_machines()
        t = self.query_one(DataTable)
        # Two leading spaces on headers so columns breathe even when content is narrow.
        t.add_column(_PAD + "Machine", key="machine")
        t.add_column(_PAD + "VMID", key="vmid")
        t.add_column(_PAD + "VM", key="vm")
        t.add_column(_PAD + "Data VM", key="disk_vm")
        t.add_column(_PAD + "Data Size", key="disk_size")
        t.add_column(_PAD + "Ping", key="ping")
        t.add_column(_PAD + "Ansible Last Run", key="ansible")
        for m in self._machines:
            d = m.data_disk
            t.add_row(
                _PAD + m.key,
                _PAD + str(m.vmid),
                Text(_PAD + "…", style="dim"),
                Text(_PAD + "…", style="dim") if d else Text(_PAD + "-", style="dim"),
                Text(_PAD + d.size if d else _PAD + "-", style="" if d else "dim"),
                Text(_PAD + "…", style="dim"),
                Text(_PAD + "…", style="dim"),
                key=m.key,
            )
        self.watch(t, "scroll_x", lambda _: self._update_arrow())
        self.set_interval(15, self._poll_all)
        self._poll_all()

    def on_resize(self) -> None:
        self.call_after_refresh(self._update_arrow)

    def _update_arrow(self) -> None:
        t = self.query_one(DataTable)
        arrow = self.query_one("#arrow", Static)
        arrow.update("→" if t.scroll_x < t.max_scroll_x else "")

    def _poll_all(self) -> None:
        for m in self._machines:
            self.run_worker(self._poll(m), exclusive=True, group=f"poll_{m.key}")

    def action_add_machine(self) -> None:
        def _on_done(created: bool) -> None:
            if created:
                self._reload_table()
        self.push_screen(AddMachineScreen(self._machines), _on_done)  # type: ignore[attr-defined]

    def _reload_table(self) -> None:
        self._machines = load_machines()
        t = self.query_one(DataTable)
        t.clear()
        for m in self._machines:
            d = m.data_disk
            t.add_row(
                _PAD + m.key,
                _PAD + str(m.vmid),
                Text(_PAD + "…", style="dim"),
                Text(_PAD + "…", style="dim") if d else Text(_PAD + "-", style="dim"),
                Text(_PAD + d.size if d else _PAD + "-", style="" if d else "dim"),
                Text(_PAD + "…", style="dim"),
                Text(_PAD + "…", style="dim"),
                key=m.key,
            )
        self._poll_all()

    def action_edit_tags(self) -> None:
        t = self.query_one(DataTable)  # type: ignore[attr-defined]
        if t.cursor_row >= len(self._machines):
            return
        machine = self._machines[t.cursor_row]

        def _on_done(updated: bool) -> None:
            if updated:
                self._reload_table()

        self.push_screen(EditTagsScreen(machine), _on_done)  # type: ignore[attr-defined]

    def action_edit_cores(self) -> None:
        t = self.query_one(DataTable)  # type: ignore[attr-defined]
        if t.cursor_row >= len(self._machines):
            return
        machine = self._machines[t.cursor_row]

        def _on_done(updated: bool) -> None:
            if updated:
                self._reload_table()

        self.push_screen(EditCoresScreen(machine), _on_done)  # type: ignore[attr-defined]

    def action_edit_memory(self) -> None:
        t = self.query_one(DataTable)  # type: ignore[attr-defined]
        if t.cursor_row >= len(self._machines):
            return
        machine = self._machines[t.cursor_row]

        def _on_done(updated: bool) -> None:
            if updated:
                self._reload_table()

        self.push_screen(EditMemoryScreen(machine), _on_done)  # type: ignore[attr-defined]

    def action_vm_info(self) -> None:
        t = self.query_one(DataTable)
        if t.cursor_row >= len(self._machines):
            return
        self.push_screen(VmInfoScreen(self._machines[t.cursor_row]))

    def action_host_info(self) -> None:
        node = self._machines[0].node if self._machines else "pve"
        self.push_screen(HostInfoScreen(node))  # type: ignore[attr-defined]

    def action_purge(self) -> None:
        t = self.query_one(DataTable)  # type: ignore[attr-defined]
        if t.cursor_row >= len(self._machines):
            return
        machine = self._machines[t.cursor_row]
        d = machine.data_disk
        if d:
            info = (
                f"VM [bold]{machine.key}[/bold]  (VMID {machine.vmid})\n"
                f"Data disk [bold]{d.key}[/bold]  (VMID {d.vmid}, {d.size})\n"
                "will be [bold]permanently deleted[/bold]."
            )
        else:
            info = (
                f"VM [bold]{machine.key}[/bold]  (VMID {machine.vmid})\n"
                "will be [bold]permanently deleted[/bold]."
            )

        def _on_confirmed(confirmed: bool) -> None:
            if confirmed:
                self.push_screen(PurgeScreen(machine), lambda _: self._reload_table())  # type: ignore[attr-defined]

        self.push_screen(DangerScreen(  # type: ignore[attr-defined]
            title="⚠   PURGE MACHINE   ⚠",
            info=info,
            warning="ALL CONFIG AND DATA WILL BE PERMANENTLY DELETED.",
        ), _on_confirmed)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())  # type: ignore[attr-defined]

    def action_start(self) -> None:
        t = self.query_one(DataTable)
        if t.cursor_row >= len(self._machines):
            return
        self.push_screen(StartConfirmScreen(self._machines[t.cursor_row]))  # type: ignore[attr-defined]

    def action_shutdown(self) -> None:
        t = self.query_one(DataTable)
        if t.cursor_row >= len(self._machines):
            return
        self.push_screen(ShutdownConfirmScreen(self._machines[t.cursor_row]))  # type: ignore[attr-defined]

    def action_reboot(self) -> None:
        t = self.query_one(DataTable)
        if t.cursor_row >= len(self._machines):
            return
        self.push_screen(RebootConfirmScreen(self._machines[t.cursor_row]))  # type: ignore[attr-defined]

    def action_deploy(self) -> None:
        t = self.query_one(DataTable)  # type: ignore[attr-defined]
        if t.cursor_row >= len(self._machines):
            return
        machine = self._machines[t.cursor_row]

        def _on_confirm(result: str) -> None:
            if result == "standard":
                self.push_screen(DeployScreen(machine))  # type: ignore[attr-defined]
            elif result == "full":
                self.push_screen(DeployScreen(machine, full_redeploy=True))  # type: ignore[attr-defined]

        self.push_screen(ConfirmScreen(machine), _on_confirm)  # type: ignore[attr-defined]

    async def _poll(self, m: Machine) -> None:
        async def noop() -> str:
            return "-"

        disk_coro = (
            get_vm_status(m.data_disk.vmid, m.data_disk.node)
            if m.data_disk
            else noop()
        )

        vm_status, disk_status, is_up, sentinel = await asyncio.gather(
            get_vm_status(m.vmid, m.node),
            disk_coro,
            ping(m.ip),
            get_sentinel(m.ip),
        )

        t = self.query_one(DataTable)
        t.update_cell(m.key, "vm", _vm_cell(vm_status))
        t.update_cell(m.key, "disk_vm", _disk_status_cell(m.data_disk, disk_status))
        t.update_cell(m.key, "ping", _ping_cell(is_up))
        t.update_cell(m.key, "ansible", _sentinel_cell(sentinel))
        self.call_after_refresh(self._update_arrow)


if __name__ == "__main__":
    StatusApp().run()
