import re
import shutil
from pathlib import Path

from config import REPO_ROOT

_PVE_VARS = REPO_ROOT / "terraform" / "envs" / "pve" / "variables.tf"
_PVE_STATIC_VARS = REPO_ROOT / "terraform" / "envs" / "pve-static" / "variables.tf"
_INVENTORY = REPO_ROOT / "ansible" / "inventory.ini"
_PLAYBOOKS = REPO_ROOT / "ansible" / "playbooks"


def _set_entry_tags(path: Path, key: str, new_tags: str) -> str:
    """Replace the tags value for a named entry. Returns the old tags string."""
    text = path.read_text()
    entry_re = re.compile(
        r'\n    ' + re.escape(key) + r' = \{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
        re.DOTALL,
    )
    m = entry_re.search(text)
    if not m:
        raise ValueError(f"entry '{key}' not found in {path.name}")
    entry = m.group(0)
    tags_m = re.search(r'\btags\s*=\s*"([^"]*)"', entry)
    old_tags = tags_m.group(1) if tags_m else ""
    new_entry = re.sub(r'(\btags\s*=\s*)"[^"]*"', rf'\1"{new_tags}"', entry)
    path.write_text(text[: m.start()] + new_entry + text[m.end() :])
    return old_tags


def update_machine_tags(key: str, new_main_tags: str, has_data_disk: bool) -> tuple[str, str | None]:
    """
    Write new tags to config files.
    Data disk tags are derived as 'data' + extras (everything that isn't 'vm').
    Returns (old_main_tags, old_data_tags) for rollback.
    """
    old_main = _set_entry_tags(_PVE_VARS, key, new_main_tags)
    old_data: str | None = None
    if has_data_disk:
        extras = [t for t in (t.strip() for t in new_main_tags.split(",")) if t and t != "vm"]
        new_data_tags = ",".join(["data"] + extras)
        old_data = _set_entry_tags(_PVE_STATIC_VARS, f"{key}_data", new_data_tags)
    return old_main, old_data


def rollback_machine_tags(key: str, old_main_tags: str, old_data_tags: str | None) -> None:
    _set_entry_tags(_PVE_VARS, key, old_main_tags)
    if old_data_tags is not None:
        _set_entry_tags(_PVE_STATIC_VARS, f"{key}_data", old_data_tags)


def _set_entry_int(path: Path, key: str, field: str, new_value: int) -> int:
    """Replace an integer field in a named HCL entry. Returns the old value."""
    text = path.read_text()
    entry_re = re.compile(
        r'\n    ' + re.escape(key) + r' = \{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
        re.DOTALL,
    )
    m = entry_re.search(text)
    if not m:
        raise ValueError(f"entry '{key}' not found in {path.name}")
    entry = m.group(0)
    field_m = re.search(r'^\s*' + re.escape(field) + r'\s*=\s*(\d+)\s*$', entry, re.MULTILINE)
    if not field_m:
        raise ValueError(f"field '{field}' not found in entry '{key}'")
    old_value = int(field_m.group(1))
    new_entry = re.sub(
        r'(\b' + re.escape(field) + r'\s*=\s*)\d+',
        rf'\g<1>{new_value}',
        entry,
    )
    path.write_text(text[: m.start()] + new_entry + text[m.end() :])
    return old_value


def update_machine_cores(key: str, new_cores: int) -> int:
    """Write new core count to config. Returns old value for rollback."""
    return _set_entry_int(_PVE_VARS, key, "cores", new_cores)


def rollback_machine_cores(key: str, old_cores: int) -> None:
    _set_entry_int(_PVE_VARS, key, "cores", old_cores)


def update_machine_memory(key: str, new_memory: int, new_minimum_memory: int) -> tuple[int, int]:
    """Write new memory values to config. Returns (old_memory, old_minimum_memory) for rollback."""
    old_memory = _set_entry_int(_PVE_VARS, key, "memory", new_memory)
    old_minimum = _set_entry_int(_PVE_VARS, key, "minimum_memory", new_minimum_memory)
    return old_memory, old_minimum


def rollback_machine_memory(key: str, old_memory: int, old_minimum_memory: int) -> None:
    _set_entry_int(_PVE_VARS, key, "memory", old_memory)
    _set_entry_int(_PVE_VARS, key, "minimum_memory", old_minimum_memory)


def _insert_hcl_entry(text: str, entry: str) -> str:
    """Insert a new HCL map entry before the closing brace of the default block."""
    idx = text.rfind("\n\n  }\n}")
    if idx == -1:
        raise ValueError("cannot locate end of default block in variables.tf")
    # text[:idx] ends right after the last entry's closing brace (no trailing newline).
    # text[idx + 1:] is "\n  }\n}\n" - we skip the first \n of the blank line gap.
    return text[:idx] + "\n\n" + entry + "\n" + text[idx + 1:]


def _join_tags(base: str, extra: str) -> str:
    tags = [base]
    if extra.strip():
        tags += [t for t in re.split(r"[,\s]+", extra.strip()) if t]
    return ",".join(tags)


def add_pve_entry(
    key: str,
    vmid: int,
    memory: int,
    minimum_memory: int,
    cores: int,
    boot_disk_size: str,
    extra_tags: str,
    data_disk: bool,
    data_disk_storage: str = "bulk-zfs",
) -> None:
    name = key.replace("_", "-")
    ip_last = vmid % 1000
    data_vmid = vmid + 300
    tags = _join_tags("vm", extra_tags)

    passthrough_line = ""
    if data_disk:
        passthrough_line = (
            f'\n      passthrough_disk = {{disk_file = "{data_disk_storage}:vm-{data_vmid}-disk-0"}}'
        )

    entry = (
        f"    {key} = {{\n"
        f'      name           = "{name}"\n'
        f'      tags           = "{tags}"\n'
        f"      vmid           = {vmid}\n"
        f"      memory         = {memory}\n"
        f"      minimum_memory = {minimum_memory}\n"
        f"      cores          = {cores}\n"
        f'      boot_disk      = {{size = "{boot_disk_size}", storage = "local-zfs"}}'
        f"{passthrough_line}\n"
        f'      ipconfig       = "ip=192.168.1.{ip_last}/24,gw=192.168.1.1"\n'
        f"    }}"
    )
    _PVE_VARS.write_text(_insert_hcl_entry(_PVE_VARS.read_text(), entry))


def add_pve_static_entry(
    key: str,
    vmid: int,
    extra_tags: str,
    data_disk_size: str,
    data_disk_storage: str = "bulk-zfs",
) -> None:
    name = key.replace("_", "-")
    data_vmid = vmid + 300
    tags = _join_tags("data", extra_tags)

    data_disk_body = f'size = "{data_disk_size}"'
    if data_disk_storage != "bulk-zfs":
        data_disk_body += f', storage = "{data_disk_storage}"'

    entry = (
        f"    {key}_data = {{\n"
        f'      name      = "{name}-data"\n'
        f'      tags      = "{tags}"\n'
        f"      vmid      = {data_vmid}\n"
        f'      data_disk = {{{data_disk_body}}}\n'
        f"    }}"
    )
    _PVE_STATIC_VARS.write_text(_insert_hcl_entry(_PVE_STATIC_VARS.read_text(), entry))


def add_inventory_entry(key: str, vmid: int) -> None:
    ip = f"192.168.1.{vmid % 1000}"
    text = _INVENTORY.read_text()
    _INVENTORY.write_text(text.rstrip() + f"\n\n[{key}]\n{key}_0 ansible_host={ip} ansible_user=ansible\n")


def create_playbook(key: str, data_disk: bool) -> None:
    playbook_dir = _PLAYBOOKS / key
    playbook_dir.mkdir(parents=True, exist_ok=True)

    lvm_block = ""
    if data_disk:
        lvm_block = (
            "\n"
            "    - name: Ensure LVM disk\n"
            "      ansible.builtin.include_tasks: ../../tasks/lvm_mount.yaml\n"
            "      vars:\n"
            "        lvm_vg: data_vg\n"
            "        lvm_lv: data_lv\n"
            "        lvm_pvs: /dev/sdb\n"
            "        lvm_mount: /mnt/data\n"
        )

    content = (
        "---\n"
        f"- name: Setup {key}\n"
        f"  hosts: {key}\n"
        "  become: true\n"
        "  gather_facts: false\n"
        "\n"
        "  tasks:\n"
        "\n"
        "    - name: Wait until ready\n"
        "      ansible.builtin.include_tasks: ../../tasks/wait_until_ready.yaml\n"
        f"{lvm_block}"
        "\n"
        "    # --- Add your tasks here ---\n"
        "\n"
        "    - name: Write ansible run timestamp\n"
        "      ansible.builtin.include_tasks: ../../tasks/ansible_sentinel.yaml\n"
    )
    (playbook_dir / "playbook.yaml").write_text(content)


def _remove_hcl_entry(text: str, key: str) -> str:
    """Remove a named entry from an HCL default map block."""
    pattern = re.compile(
        r'\n\n    ' + re.escape(key) + r' = \{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
        re.DOTALL,
    )
    return pattern.sub('', text)


def remove_pve_entry(key: str) -> None:
    _PVE_VARS.write_text(_remove_hcl_entry(_PVE_VARS.read_text(), key))


def remove_pve_static_entry(key: str) -> None:
    _PVE_STATIC_VARS.write_text(_remove_hcl_entry(_PVE_STATIC_VARS.read_text(), f"{key}_data"))


def remove_inventory_entry(key: str) -> None:
    lines = _INVENTORY.read_text().splitlines(keepends=True)
    result: list[str] = []
    skip = False
    for line in lines:
        if line.strip() == f"[{key}]":
            skip = True
            if result and result[-1].strip() == "":
                result.pop()
            continue
        if skip:
            if line.startswith(f"{key}_"):
                continue
            skip = False
        result.append(line)
    _INVENTORY.write_text("".join(result))


def remove_playbook_dir(key: str) -> None:
    playbook_dir = _PLAYBOOKS / key
    if playbook_dir.exists():
        shutil.rmtree(playbook_dir)


def purge_machine_config(key: str, has_data_disk: bool) -> None:
    remove_pve_entry(key)
    if has_data_disk:
        remove_pve_static_entry(key)
    remove_inventory_entry(key)
    remove_playbook_dir(key)


def create_machine(
    key: str,
    vmid: int,
    memory: int,
    minimum_memory: int,
    cores: int,
    boot_disk_size: str,
    extra_tags: str,
    data_disk: bool,
    data_disk_size: str,
    data_disk_storage: str = "bulk-zfs",
) -> None:
    add_pve_entry(
        key, vmid, memory, minimum_memory, cores, boot_disk_size, extra_tags,
        data_disk, data_disk_storage,
    )
    if data_disk:
        add_pve_static_entry(key, vmid, extra_tags, data_disk_size, data_disk_storage)
    add_inventory_entry(key, vmid)
    create_playbook(key, data_disk)
