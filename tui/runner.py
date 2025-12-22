import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from config import REPO_ROOT

_TF_CWD = str(REPO_ROOT / "terraform")
_ANS_CWD = str(REPO_ROOT / "ansible")
_DC = ["docker-compose", "-f", "docker-compose.yml", "run", "--rm", "terraform"]
_DC_ANS = ["docker-compose", "-f", "docker-compose.yml", "run", "--rm"]


@dataclass
class Step:
    cmds: list[list[str]]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)


def _tf_env(terraform_env: str) -> dict[str, str]:
    return {**os.environ, "TERRAFORM_ENV": terraform_env}


def _tf_init() -> list[str]:
    return _DC + ["init", "-upgrade"]


def _vm_targets(key: str) -> list[str]:
    return [
        f'-target=proxmox_vm_qemu.vm["{key}"]',
        f'-target=local_file.vm["{key}"]',
        f'-target=null_resource.upload_cloudinit["{key}"]',
    ]


def _data_targets(key: str) -> list[str]:
    return [f'-target=proxmox_vm_qemu.vm["{key}"]']


def _tf_destroy(targets: list[str]) -> list[str]:
    return _DC + ["destroy", "-auto-approve", "-lock=false"] + targets


def _tf_apply(targets: list[str]) -> list[str]:
    return _DC + ["apply", "-auto-approve", "-lock=false"] + targets


def _cloudinit_cleanup(key: str) -> list[str]:
    return _DC + [
        "destroy", "-auto-approve", "-lock=false",
        f'-target=local_file.vm["{key}"]',
        f'-target=null_resource.upload_cloudinit["{key}"]',
    ]


def tf_destroy_vm(key: str) -> Step:
    return Step(
        cmds=[_tf_init(), _tf_destroy(_vm_targets(key))],
        cwd=_TF_CWD,
        env=_tf_env("pve"),
    )


def tf_destroy_disk(key: str) -> Step:
    return Step(
        cmds=[_tf_init(), _tf_destroy(_data_targets(key))],
        cwd=_TF_CWD,
        env=_tf_env("pve-static"),
    )


def tf_apply_vm(key: str) -> Step:
    return Step(
        cmds=[_tf_init(), _tf_apply(_vm_targets(key)), _cloudinit_cleanup(key)],
        cwd=_TF_CWD,
        env=_tf_env("pve"),
    )


def tf_apply_disk(key: str) -> Step:
    return Step(
        cmds=[_tf_init(), _tf_apply(_data_targets(key))],
        cwd=_TF_CWD,
        env=_tf_env("pve-static"),
    )


def _load_playbook_env(key: str) -> dict[str, str]:
    env_file = REPO_ROOT / "ansible" / "playbooks" / key / f".{key}.env"
    result: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            m = re.match(r'export\s+(\w+)="([^"]*)"', line.strip())
            if m:
                result[m.group(1)] = m.group(2)
    return result


def ansible_run(key: str) -> Step:
    playbook_env = _load_playbook_env(key)
    env_flags = [arg for k, v in playbook_env.items() for arg in ["-e", f"{k}={v}"]]
    return Step(
        cmds=[_DC_ANS + env_flags + ["ansible", "ansible-playbook", "-i", "inventory.ini", f"./playbooks/{key}/playbook.yaml"]],
        cwd=_ANS_CWD,
        env=os.environ.copy(),
    )
