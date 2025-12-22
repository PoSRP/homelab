import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
_ANSIBLE_KEY = str(REPO_ROOT / "ssh" / "id_ed25519_vm_ansible")

_VM_INFO_SCRIPT = b"""
import json, os, subprocess
up = float(open('/proc/uptime').read().split()[0])
la = [float(x) for x in open('/proc/loadavg').read().split()[:3]]
cpus = os.cpu_count()
mem = {}
for line in open('/proc/meminfo'):
    parts = line.split()
    if len(parts) >= 2:
        mem[parts[0].rstrip(':')] = int(parts[1]) * 1024
df_raw = subprocess.check_output(
    ['df', '-B1', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs',
     '--output=size,used,avail,target'],
    stderr=subprocess.DEVNULL,
).decode().splitlines()[1:]
disks = []
for line in df_raw:
    p = line.split(None, 3)
    if len(p) == 4:
        try:
            disks.append({'size': int(p[0]), 'used': int(p[1]), 'avail': int(p[2]), 'target': p[3].strip()})
        except ValueError:
            pass
print(json.dumps({
    'up': up, 'cpus': cpus, 'la': la,
    'mt': mem.get('MemTotal', 0),
    'mu': mem.get('MemTotal', 0) - mem.get('MemAvailable', 0),
    'st': mem.get('SwapTotal', 0),
    'su': mem.get('SwapTotal', 0) - mem.get('SwapFree', 0),
    'disks': disks,
}))
"""


@dataclass
class VmDiskInfo:
    mount: str
    total: int
    used: int
    avail: int


@dataclass
class VmInfo:
    uptime: float
    cpus: int
    load_avg: tuple[float, float, float]
    mem_total: int
    mem_used: int
    swap_total: int
    swap_used: int
    disks: list[VmDiskInfo] = field(default_factory=list)


def _ssh_base(ip: str) -> list[str]:
    return [
        "ssh",
        "-i", _ANSIBLE_KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        f"ansible@{ip}",
    ]


async def ping(ip: str) -> bool:
    if not ip:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


async def get_sentinel(ip: str) -> str | None:
    if not ip:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_base(ip),
            "cat /etc/ansible_last_run",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return stdout.decode().strip()
    except Exception:
        pass
    return None


async def get_vm_info(ip: str) -> VmInfo | None:
    if not ip:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_base(ip),
            "python3 -",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(input=_VM_INFO_SCRIPT), timeout=15)
        if proc.returncode == 0:
            d = json.loads(stdout.decode())
            return VmInfo(
                uptime=d["up"],
                cpus=d["cpus"],
                load_avg=(d["la"][0], d["la"][1], d["la"][2]),
                mem_total=d["mt"],
                mem_used=d["mu"],
                swap_total=d["st"],
                swap_used=d["su"],
                disks=[
                    VmDiskInfo(mount=x["target"], total=x["size"], used=x["used"], avail=x["avail"])
                    for x in d["disks"]
                ],
            )
    except Exception:
        pass
    return None
