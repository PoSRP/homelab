import asyncio
import os
from dataclasses import dataclass, field

import httpx


def _headers() -> dict[str, str]:
    token_id = os.environ.get("TF_VAR_proxmox_api_token_id", "")
    token = os.environ.get("TF_VAR_proxmox_api_token", "")
    return {"Authorization": f"PVEAPIToken={token_id}={token}"}


def _base_url() -> str:
    return os.environ.get("TF_VAR_proxmox_api_url", "").rstrip("/")


async def get_vm_status(vmid: int, node: str) -> str:
    url = _base_url()
    if not url:
        return "no config"

    endpoint = f"{url}/nodes/{node}/qemu/{vmid}/status/current"
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            resp = await client.get(endpoint, headers=_headers())
            resp.raise_for_status()
            return resp.json()["data"]["status"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return "not found"
        return "error"
    except Exception:
        return "error"


@dataclass
class StorageInfo:
    name: str
    storage_type: str
    total: int      # bytes - pool capacity
    used: int       # bytes - physically written
    avail: int      # bytes - pool free space
    allocated: int  # bytes - sum of provisioned disk sizes


@dataclass
class NodeInfo:
    node: str
    pve_version: str
    kernel_version: str
    uptime: int           # seconds
    cpu_model: str
    cpu_cores: int
    cpu_threads: int
    cpu_mhz: str
    cpu_usage: float      # 0.0-1.0
    load_avg: tuple[float, float, float]
    mem_total: int        # bytes
    mem_used: int         # bytes
    swap_total: int       # bytes
    swap_used: int        # bytes
    storages: list[StorageInfo] = field(default_factory=list)
    vm_total: int = 0
    vm_running: int = 0


async def set_vm_tags(vmid: int, node: str, tags_csv: str) -> bool | None:
    """
    Set tags on a live VM.
    Returns True on success, None if the VM doesn't exist yet, False on error.
    """
    url = _base_url()
    if not url:
        return False

    api_tags = tags_csv.replace(",", ";")
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            resp = await client.put(
                f"{url}/nodes/{node}/qemu/{vmid}/config",
                data={"tags": api_tags},
            )
            resp.raise_for_status()
            return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        return False
    except Exception:
        return False


async def set_vm_config(vmid: int, node: str, **fields: int | str) -> bool | None:
    """
    Set arbitrary config fields on a live VM.
    Returns True on success, None if the VM doesn't exist yet, False on error.
    """
    url = _base_url()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            resp = await client.put(
                f"{url}/nodes/{node}/qemu/{vmid}/config",
                data={k: str(v) for k, v in fields.items()},
            )
            resp.raise_for_status()
            return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        return False
    except Exception:
        return False


async def start_vm(vmid: int, node: str) -> bool:
    url = _base_url()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            resp = await client.post(f"{url}/nodes/{node}/qemu/{vmid}/status/start")
            resp.raise_for_status()
            return True
    except Exception:
        return False


async def shutdown_vm(vmid: int, node: str) -> bool:
    url = _base_url()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            resp = await client.post(f"{url}/nodes/{node}/qemu/{vmid}/status/shutdown")
            resp.raise_for_status()
            return True
    except Exception:
        return False


async def reboot_vm(vmid: int, node: str) -> bool:
    url = _base_url()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            resp = await client.post(f"{url}/nodes/{node}/qemu/{vmid}/status/reboot")
            resp.raise_for_status()
            return True
    except Exception:
        return False


async def get_node_info(node: str) -> NodeInfo | None:
    url = _base_url()
    if not url:
        return None

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0, headers=_headers()) as client:
            # Round 1: node status, storage list, VM list
            node_resp, storage_resp, qemu_resp = await asyncio.gather(
                client.get(f"{url}/nodes/{node}/status"),
                client.get(f"{url}/nodes/{node}/storage"),
                client.get(f"{url}/nodes/{node}/qemu"),
            )
            node_resp.raise_for_status()
            storage_resp.raise_for_status()
            qemu_resp.raise_for_status()

            s = node_resp.json()["data"]
            storages_raw = storage_resp.json()["data"]
            vms_raw = qemu_resp.json()["data"]

            # Only ZFS pools - filters out plain `local` (dir type) and similar
            zfs_pools = [
                st for st in storages_raw
                if st.get("type") == "zfspool"
                and st.get("active", 0)
                and st.get("total", 0) > 0
            ]

            # Round 2: content listing for each pool to calculate allocated size
            content_resps = await asyncio.gather(
                *[
                    client.get(f"{url}/nodes/{node}/storage/{st['storage']}/content")
                    for st in zfs_pools
                ],
                return_exceptions=True,
            )
    except Exception:
        return None

    storages = []
    for st, content_resp in zip(zfs_pools, content_resps):
        allocated = 0
        if not isinstance(content_resp, Exception):
            try:
                content_resp.raise_for_status()  # type: ignore[union-attr]
                allocated = sum(
                    v.get("size", 0) for v in content_resp.json()["data"]  # type: ignore[union-attr]
                )
            except Exception:
                pass
        storages.append(StorageInfo(
            name=st["storage"],
            storage_type="zfspool",
            total=st.get("total", 0),
            used=st.get("used", 0),
            avail=st.get("avail", 0),
            allocated=allocated,
        ))
    storages.sort(key=lambda st: st.name)

    la = s.get("loadavg", ["0", "0", "0"])

    return NodeInfo(
        node=node,
        pve_version=s.get("pveversion", ""),
        kernel_version=s.get("kversion", ""),
        uptime=s.get("uptime", 0),
        cpu_model=s.get("cpuinfo", {}).get("model", ""),
        cpu_cores=s.get("cpuinfo", {}).get("cores", 0),
        cpu_threads=s.get("cpuinfo", {}).get("cpus", 0),
        cpu_mhz=s.get("cpuinfo", {}).get("mhz", ""),
        cpu_usage=float(s.get("cpu", 0.0)),
        load_avg=(float(la[0]), float(la[1]), float(la[2])),
        mem_total=s.get("memory", {}).get("total", 0),
        mem_used=s.get("memory", {}).get("used", 0),
        swap_total=s.get("swap", {}).get("total", 0),
        swap_used=s.get("swap", {}).get("used", 0),
        storages=storages,
        vm_total=len(vms_raw),
        vm_running=sum(1 for v in vms_raw if v.get("status") == "running"),
    )
