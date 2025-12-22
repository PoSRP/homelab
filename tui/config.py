import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TERRAFORM_ENV = REPO_ROOT / "terraform" / ".terraform.env"
PVE_VARS = REPO_ROOT / "terraform" / "envs" / "pve" / "variables.tf"
PVE_STATIC_VARS = REPO_ROOT / "terraform" / "envs" / "pve-static" / "variables.tf"


@dataclass
class DataDisk:
    key: str
    vmid: int
    size: str
    node: str = "pve"


@dataclass
class Machine:
    key: str
    vmid: int
    node: str
    ip: str
    data_disk: DataDisk | None = None
    tags: str = ""
    cores: int = 0
    memory: int = 0
    minimum_memory: int = 0


def load_env() -> None:
    if not TERRAFORM_ENV.exists():
        return
    for line in TERRAFORM_ENV.read_text().splitlines():
        m = re.match(r'export\s+(\w+)="([^"]*)"', line.strip())
        if m:
            os.environ.setdefault(m.group(1), m.group(2))


def _parse_entries(text: str) -> dict[str, dict]:
    """Extract map key → fields from the vm_configs default block."""
    m = re.search(r'default\s*=\s*\{(.+?)\n  \}', text, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)

    entries: dict[str, dict] = {}
    for entry_m in re.finditer(r'\n    (\w+)\s*=\s*\{', block):
        key = entry_m.group(1)
        start = entry_m.end()
        depth, pos = 1, start
        while pos < len(block) and depth > 0:
            if block[pos] == '{':
                depth += 1
            elif block[pos] == '}':
                depth -= 1
            pos += 1
        body = block[start : pos - 1]

        fields: dict[str, str | int] = {}
        for fm in re.finditer(r'\b(\w+)\s*=\s*"([^"]*)"', body):
            fields[fm.group(1)] = fm.group(2)
        for fm in re.finditer(r'^\s*(\w+)\s*=\s*(\d+)\s*$', body, re.MULTILINE):
            if fm.group(1) not in fields:
                fields[fm.group(1)] = int(fm.group(2))
        entries[key] = fields

    return entries


def load_machines() -> list[Machine]:
    load_env()

    pve_entries = _parse_entries(PVE_VARS.read_text())
    static_entries = _parse_entries(PVE_STATIC_VARS.read_text())

    data_disks: dict[str, DataDisk] = {}
    for key, fields in static_entries.items():
        if key.endswith("_data"):
            vm_key = key[:-5]
            data_disks[vm_key] = DataDisk(
                key=key,
                vmid=int(fields["vmid"]),
                size=str(fields.get("size", "?")),
                node=str(fields.get("target_node", "pve")),
            )

    machines: list[Machine] = []
    for key, fields in pve_entries.items():
        ipconfig = str(fields.get("ipconfig", ""))
        ip_m = re.search(r'ip=(\d+\.\d+\.\d+\.\d+)', ipconfig)
        machines.append(Machine(
            key=key,
            vmid=int(fields["vmid"]),
            node=str(fields.get("target_node", "pve")),
            ip=ip_m.group(1) if ip_m else "",
            data_disk=data_disks.get(key),
            tags=str(fields.get("tags", "")),
            cores=int(fields.get("cores", 0)),
            memory=int(fields.get("memory", 0)),
            minimum_memory=int(fields.get("minimum_memory", 0)),
        ))

    return sorted(machines, key=lambda m: m.vmid)
