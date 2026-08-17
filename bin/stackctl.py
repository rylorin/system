#!/usr/bin/env python3
"""stackctl - manage a docker swarm stack repository.

Subcommands mirror the historical shell tools but operate on the *current*
working directory (stack name = basename of the CWD):

  download        rsync the stack down and capture a full reconstructable backup
  upload          rsync the stack up (and run a remote postupload hook)
  install         on a manager: load configs, build volumes, deploy the stack
  backup_volumes  copy local volumes/ tree into named docker volumes

The tool lives in the `system` repo but can be invoked from the directory of
any other stack. Configuration is read from ./stack.yaml (per-stack) with the
remote optionally overridden on the command line.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import typing as t
from pathlib import Path

CONFIG_NAME = "stack.yaml"
COMPOSE_NAME = "docker-compose.yml"
EXCLUDES_FILE = "etc/excludes"
BACKUP_DIR = "backup"
DEFAULT_RSYNC_IMAGE = "eeacms/rsync:latest"
DEFAULT_BACKUP_KEEP = 7
ENV_FILE = ".env"
_VENV_REL = [".venv", "venv"]


def _run_relaunch() -> None:
    """Re-execute via the repo virtualenv when PyYAML is missing on plain python3."""
    if os.environ.get("STACKCTL_RELAUNCHED"):
        return
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve().parent.parent
    for name in _VENV_REL:
        for candidate in (here / name, Path.cwd() / name):
            py = candidate / "bin" / "python"
            if py.exists():
                os.environ["STACKCTL_RELAUNCHED"] = "1"
                os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def _ex_safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# ---------------------------------------------------------------------------
# YAML / dotenv parsing
# ---------------------------------------------------------------------------


def _dotenv(path: Path) -> dict:
    env: dict = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit(
            "error: PyYAML is required to parse " + str(path) +
            "\n  Install it with:  pip install pyyaml"
        )
    return yaml.safe_load(path.read_text()) or {}


_EXPAND = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


def _expand(env: dict, value):
    if isinstance(value, str):
        def repl(m):
            name, default = m.group(1), m.group(3)
            if name in env and env[name] != "":
                return env[name]
            return default if default is not None else ""

        return _EXPAND.sub(repl, value)
    if isinstance(value, list):
        return [_expand(env, item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(env, item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Config:
    def __init__(self, stack_dir: Path, remote: t.Optional[str] = None):
        self.stack_dir = stack_dir
        self.env = _dotenv(stack_dir / ENV_FILE)
        cfg: dict = {}
        cfg_path = stack_dir / CONFIG_NAME
        if cfg_path.exists():
            cfg = _load_yaml(cfg_path) or {}
        
        self.stack_name = str(cfg.get("stack_name") or stack_dir.name)
        self.remote = remote or cfg.get("remote")
        self.rsync_image = cfg.get("rsync_image") or DEFAULT_RSYNC_IMAGE
        try:
            self.backup_keep = int(cfg.get("backup_keep") or DEFAULT_BACKUP_KEEP)
        except (TypeError, ValueError):
            self.backup_keep = DEFAULT_BACKUP_KEEP
        self.nfs_nodes = [str(n) for n in (cfg.get("nfs_nodes") or [])]
        ex = cfg.get("excludes") or []
        self.excludes = [str(e) for e in ex] if isinstance(ex, list) else []

    @property
    def remote_dir(self) -> str:
        return f"/root/{self.stack_name}"


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.errors: list = []

    def local(self, cmd: t.Sequence[str], check: bool = True) -> t.Tuple[int, str]:
        if self.dry_run:
            print("[dry-run] " + " ".join(cmd))
            return 0, ""
        try:
            proc = subprocess.run(list(cmd), capture_output=True, text=True)
        except FileNotFoundError as exc:
            self.errors.append(f"missing binary: {exc}")
            return 127, ""
        if check and proc.returncode != 0:
            self.errors.append(" ".join(cmd) + f" (rc={proc.returncode})")
        return proc.returncode, proc.stdout or ""

    def remote(self, remote: str, shell: str, check: bool = True):
        return self.local(["ssh", remote, shell], check=check)


def _stack_excludes(cfg: Config) -> t.List[str]:
    ef = cfg.stack_dir / EXCLUDES_FILE
    if ef.exists():
        return [ln.strip() for ln in ef.read_text().splitlines() if ln.strip()]
    return []


# ---------------------------------------------------------------------------
# Compose parsing
# ---------------------------------------------------------------------------


def load_compose(cfg: Config) -> dict:
    path = cfg.stack_dir / COMPOSE_NAME
    if not path.exists():
        return {}
    return _expand(cfg.env, _load_yaml(path))


def referenced_configs(doc: dict) -> t.List[str]:
    services = (doc or {}).get("services", {}) or {}
    result: t.List[str] = []
    seen = set()
    for svc in services.values():
        for item in svc.get("configs") or []:
            name = item if isinstance(item, str) else item.get("source") or item.get("target")
            if isinstance(name, str) and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def config_docker_names(doc: dict, state: dict, env: dict) -> t.Dict[str, str]:
    """Map compose config name -> actual docker config name (from active Swarm state if possible)."""
    mapping: t.Dict[str, str] = {}
    
    # 1. Start with defaults from compose file (expanded with local .env)
    decl = (doc or {}).get("configs", {}) or {}
    for key, spec in decl.items():
        if isinstance(spec, dict) and spec.get("name"):
            mapping[key] = _expand(env, str(spec["name"]))
        else:
            mapping[key] = key

    # 2. Override with TRUTH from swarm state (no guessing needed)
    services = (doc or {}).get("services", {}) or {}
    swarm_services = state.get("services", {})
    
    for svc_name, svc_decl in services.items():
        swarm_svc = None
        for s_name, s_data in swarm_services.items():
            if s_name.endswith(f"_{svc_name}") or s_name == svc_name:
                swarm_svc = s_data
                break
        if not swarm_svc or not isinstance(swarm_svc, dict):
            continue
            
        active_configs = swarm_svc.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Configs") or []
        
        for item in svc_decl.get("configs") or []:
            source = None
            target = None
            if isinstance(item, str):
                source = item
                target = f"/{item}"
            elif isinstance(item, dict):
                source = item.get("source")
                target = item.get("target") or f"/{source}"
                
            if source and target:
                for ac in active_configs:
                    if ac.get("File", {}).get("Name") == target:
                        mapping[source] = ac.get("ConfigName")
                        break
                        
    return mapping


def referenced_volumes(doc: dict) -> t.List[str]:
    result: t.List[str] = []
    seen = set()
    for svc in ((doc or {}).get("services", {}) or {}).values():
        for item in svc.get("volumes") or []:
            src = None
            if isinstance(item, dict) and item.get("type") == "volume":
                src = item.get("source")
            elif isinstance(item, str) and ":" in item and not item.startswith("/"):
                src = item.split(":", 1)[0]
            if isinstance(src, str) and src and src not in seen:
                seen.add(src)
                result.append(src)
    return result


def _local_volume_mounts(doc: dict) -> t.Dict[str, str]:
    """Return {host_path: container_path} for bind-mounts that map a local path."""
    mounts: t.Dict[str, str] = {}
    for svc in ((doc or {}).get("services", {}) or {}).values():
        for item in svc.get("volumes") or []:
            dh = None
            if isinstance(item, dict) and item.get("type") == "bind":
                dh = (item.get("source"), item.get("target"))
            elif isinstance(item, str) and ":" in item:
                src = item.split(":", 1)[0]
                # host path starts with / or . or ~ => real bind mount
                if src.startswith("/") or src.startswith(".") or src.startswith("~"):
                    tgt = item.split(":", 1)[1].split(",")[0] if ":" in item else ""
                    dh = (src, tgt)
            if dh and dh[0]:
                mounts.setdefault(dh[0], dh[1] or "")
    return mounts


# ---------------------------------------------------------------------------
# Volume sync routines (shared by download / install / backup-volumes)
# ---------------------------------------------------------------------------


def volume_sync_down(cfg: Config, remote: str, vol: str, dst: Path, run: Runner) -> int:
    shell = (
        f"docker run --rm --volume {vol}:/src --volume '{dst}':/dst "
        f"{cfg.rsync_image} rsync --archive --delete /src/ /dst >/dev/null"
    )
    rc, _ = run.remote(remote, shell, check=False)
    return rc


def volume_sync_up(cfg: Config, run: Runner, vol: str, local_dir: Path) -> int:
    cmd = [
        "docker", "run", "--rm",
        "--volume", f"{local_dir}:/src",
        "--volume", f"{vol}:/dst",
        "--name", "rsync",
        cfg.rsync_image,
        "rsync", "--archive", "--delete", "--chmod=Da+rwx,Fa+rw", "--chown=1001",
        "/src/", "/dst/",
    ]
    rc, _ = run.local(cmd, check=False)
    return rc


def volume_backup(cfg: Config, run: Runner, vol: str, local_dir: Path) -> int:
    cmd = [
        "docker", "run", "--rm",
        "--volume", f"{vol}:/src",
        "--volume", f"{local_dir}:/dst",
        "--name", "rsync",
        cfg.rsync_image,
        "rsync", "--archive", "--delete", "/src/", "/dst/",
    ]
    rc, _ = run.local(cmd, check=False)
    return rc


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def cmd_download(cfg: Config, args):
    run = Runner(dry_run=args.dry_run)
    if not cfg.remote:
        sys.exit("error: no remote set (stack.yaml `remote` or --remote)")
    remote = cfg.remote

    doc = load_compose(cfg)
    confs = referenced_configs(doc)
    vols = referenced_volumes(doc)

    if run.dry_run:
        print(f"would back up stack '{cfg.stack_name}' from {remote}")
        print("configs to capture:", confs)
        print("volumes to capture:", vols)
        print("tree rsync:", not args.skip)
        return

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = cfg.stack_dir / BACKUP_DIR / ts
    snap.mkdir(parents=True, exist_ok=True)
    log_path = snap / "backup.log"

    def info(line, log=None):
        print("[b] " + line)
        if log:
            log.write(line + "\n")

    log = open(log_path, "w")
    info(f"== backup {ts} of stack {cfg.stack_name} (remote {remote}) ==", log)

    # 1) whole-tree rsync (compose, .env, configs/, volumes/, volumes_nfs/)
    if not args.skip:
        run.local(
            ["rsync", "--archive", "--update", "--compress", "--verbose"]
            + [f"--exclude={e}" for e in ([".git", ".DS_Store", "backup"] + cfg.excludes + _stack_excludes(cfg))]
            + [f"{remote}:{cfg.remote_dir}/", "."],
            check=False,
        )

    # 2) docker state
    state: dict = {"stack": cfg.stack_name, "timestamp": ts,
                   "volumes": vols, "configs": confs, "services": {}}
    for label, shell in [
        ("swarm", "docker info --format '{{json .}}'"),
        ("nodes", "docker node ls --format '{{json .}}'"),
        ("networks", "docker network ls --format '{{json .}}'"),
    ]:
        rc, out = run.remote(remote, shell, check=False)
        if rc == 0 and out.strip():
            state[label] = out

    rc, out = run.remote(
        remote, f"docker stack services {cfg.stack_name} --format '{{{{.Name}}}}'", check=False)
    for name in (ln.strip() for ln in (out or "").splitlines() if ln.strip()):
        rc2, so = run.remote(
            remote, f"docker service inspect {name} --format '{{{{json .}}}}'", check=False)
        if rc2 == 0 and so.strip():
            try:
                state["services"][name] = json.loads(so)
            except json.JSONDecodeError:
                state["services"][name] = so

    (snap / "docker-state.json").write_text(json.dumps(state, indent=2))

    # 2.5) Extract env vars and update local .env
    compose_text = (cfg.stack_dir / COMPOSE_NAME).read_text() if (cfg.stack_dir / COMPOSE_NAME).exists() else ""
    required_vars = set(m.group(1) for m in _EXPAND.finditer(compose_text))
    
    if required_vars and not args.dry_run:
        env_updates = {}
        for svc_data in state.get("services", {}).values():
            if not isinstance(svc_data, dict):
                continue
            spec_env = svc_data.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Env") or []
            for item in spec_env:
                if "=" in item:
                    k, v = item.split("=", 1)
                    if k in required_vars:
                        env_updates[k] = v
        
        if env_updates:
            env_file = cfg.stack_dir / ENV_FILE
            current_env = _dotenv(env_file)
            changed = False
            for k, v in env_updates.items():
                if current_env.get(k) != v:
                    current_env[k] = v
                    changed = True
            
            if changed:
                lines = []
                for k in sorted(current_env.keys()):
                    lines.append(f"{k}={current_env[k]}")
                env_file.write_text("\n".join(lines) + "\n")
                info(f"  updated {ENV_FILE} with {len(env_updates)} active variables from swarm", log)

            # Update .env.example
            example_file = cfg.stack_dir / f"{ENV_FILE}.example"
            example_env = _dotenv(example_file)
            example_changed = False
            for k in required_vars:
                if k not in example_env:
                    example_env[k] = "CHANGE_ME"
                    example_changed = True
            if example_changed:
                lines = []
                for k in sorted(example_env.keys()):
                    lines.append(f"{k}={example_env[k]}")
                example_file.write_text("\n".join(lines) + "\n")

    # 3) docker configs
    current_env = _dotenv(cfg.stack_dir / ENV_FILE)
    docker_names = config_docker_names(doc, state, current_env)
    for cname in confs:
        real = docker_names.get(cname, cname)
        rc, out = run.remote(
            remote, f"docker config inspect {real} --format '{{{{json .}}}}'", check=False)
        if rc != 0 or not out.strip():
            info(f"  config {cname} ({real}): not retrievable", log)
            continue
        try:
            parsed = json.loads(out)
            if isinstance(parsed, list):
                parsed = parsed[0]
            data = base64.b64decode(parsed["Spec"]["Data"])
        except Exception as e:
            info(f"  config {cname}: decode failed ({e})", log)
            continue
        cdir = snap / "configs" / _ex_safe(cname)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "data").write_bytes(data)
        info(f"  config {cname}: {len(data)} bytes saved", log)

    # 4) named volumes bound to a file-system path are already covered by rsync;
    #    independent/named volumes are copied down
    local_mounts = _local_volume_mounts(doc)
    for vol in vols:
        if vol in local_mounts:
            info(f"  volume {vol}: bound to {local_mounts[vol] or 'container'} (covered by rsync)", log)
            continue
        vdir = snap / "volumes" / _ex_safe(vol)
        vdir.mkdir(parents=True, exist_ok=True)
        rc = volume_sync_down(cfg, remote, vol, vdir, run)
        info(f"  volume {vol}: sync rc={rc}", log)

    _write_rebuild(snap, cfg, remote, vols, confs)
    log.close()

    if run.errors and not args.dry_run:
        print(f"[b] {len(run.errors)} command(s) failed - backup may be incomplete")
        sys.exit(1)
    if not args.dry_run and not args.skip_rotation:
        rotate_backups(cfg.stack_dir, cfg.backup_keep)


def _write_rebuild(snap: Path, cfg: Config, remote: str, vols, confs):
    lines = [
        "#!/bin/bash",
        f"# Rebuild stack `{cfg.stack_name}` on a fresh node from this backup.",
        "# 1) put the stack repo in place (bin/upload, bin/install on the manager),",
        "# 2) restore volumes into the local volumes/ tree (backup-volumes),",
        "remote=" + (remote or ""),
        "# 3) recreate docker configs:",
    ]
    for c in confs:
        lines.append(f"  docker config create {c} {snap}/configs/{_ex_safe(c)}/data 2>/dev/null || true")
    lines.append("")
    lines.append("# 4) create named volumes and deploy:")
    for v in vols:
        lines.append(f"  docker volume create {v} 2>/dev/null || true")
    lines.append(f"  docker stack deploy -c {COMPOSE_NAME} {cfg.stack_name}")
    (snap / "rebuild.sh").write_text("\n".join(lines))
    (snap / "rebuild.sh").chmod(0o755)


def rotate_backups(stack_dir: Path, keep: int):
    if keep <= 0:
        return
    pattern = re.compile(r"^\d{8}-\d{6}$")
    snaps = sorted(
        p for p in (stack_dir / BACKUP_DIR).iterdir()
        if p.is_dir() and pattern.match(p.name)
    )
    while len(snaps) > keep:
        shutil.rmtree(snaps.pop(0))


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def cmd_upload(cfg: Config, args):
    run = Runner(dry_run=args.dry_run)
    if not cfg.remote:
        sys.exit("error: no remote configured")
    excludes = [".git", ".DS_Store", "backup"] + cfg.excludes
    rc, _ = run.local(
        ["rsync", "-r", "--update", "--compress", "--verbose"]
        + [f"--exclude={e}" for e in excludes]
        + [".", f"{cfg.remote}:{cfg.remote_dir}/"], check=False)
    if not args.dry_run and rc == 0:
        run.remote(cfg.remote,
                   f"cd {cfg.remote_dir} && [ -x ./bin/postupload ] && ./bin/postupload")
    if run.errors and not args.dry_run:
        sys.exit(1)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def cmd_install(cfg: Config, args):
    run = Runner(dry_run=args.dry_run)
    is_manager = args.role == "manager"
    only = any([args.configs_only, args.volumes_only, args.nfs_only, args.deploy_only])
    if (args.configs_only or not only) and is_manager:
        _install_configs(cfg, run)
    if args.volumes_only or not only:
        _install_volumes(cfg, run)
    if (args.nfs_only or not only) and is_manager:
        _install_nfs(cfg, run)
    if (args.deploy_only or not only) and is_manager:
        _deploy(cfg, run)
    if run.errors and not args.dry_run:
        sys.exit(1)


def _install_configs(cfg: Config, run: Runner):
    for conf in sorted((cfg.stack_dir / "configs").glob("*")):
        if conf.is_file():
            if run.dry_run:
                print(f"  [dry-run] docker config create {conf.name}")
                continue
            rc, _ = run.local(["docker", "config", "create", conf.name, str(conf)], check=False)
            if rc == 0:
                print(f"  config {conf.name} created")


def _install_volumes(cfg: Config, run: Runner):
    for d in sorted((cfg.stack_dir / "volumes").glob("*")):
        if d.is_dir():
            vol = f"{cfg.stack_name}_{d.name}"
            rc = volume_sync_up(cfg, run, vol, d)
            if not run.dry_run:
                print(f"  volume {vol}: rc={rc}")


def _install_nfs(cfg: Config, run: Runner):
    _, host_out = run.local(["hostname", "--fqdn"], check=False)
    host = host_out.strip() or "localhost"
    exports_path = Path("/etc/exports")
    for d in sorted((cfg.stack_dir / "volumes_nfs").glob("*")):
        if not d.is_dir():
            continue
        name = f"{d.name}_nfs"
        if exports_path.exists():
            # strip any previous entry for this export path
            lines = [ln for ln in exports_path.read_text().splitlines()
                     if str(d) not in ln]
            if run.dry_run:
                print(f"  [dry-run] update /etc/exports (dedupe {d})")
            else:
                if cfg.nfs_nodes:
                    entries = " ".join(
                        f"{w}(rw,all_squash,no_subtree_check,anonuid=0)"
                        for w in cfg.nfs_nodes
                    )
                    lines.append(f"{d}\t{entries}")
                exports_path.write_text("\n".join(lines) + "\n")
        opt = f"o=addr={host},rw,noatime,rsize=8192,wsize=8192,tcp,timeo=14,nfsvers=3,soft"
        rc, _ = run.local(
            ["docker", "volume", "create", "--driver", "local",
             "--opt", "type=nfs", "--opt", opt,
             "--opt", f"device={host}:{str(d)}", name], check=False)
        if not run.dry_run:
            print(f"  nfs volume {name}: rc={rc}")
        # worker-side volume command is printed for reference
        print(f"  on workers: docker volume create --driver local --opt type=nfs "
              f"--opt \"o=addr={host},rw,noatime,rsize=8192,wsize=8192,tcp,timeo=14,nfsvers=3,soft\" "
              f"--opt \"device={host}:{d}\" {name}")
    if exports_path.exists() and not run.dry_run:
        run.local(["exportfs", "-a"], check=False)


def _deploy(cfg: Config, run: Runner):
    compose = cfg.stack_dir / COMPOSE_NAME
    if compose.exists():
        run.local(["docker", "stack", "deploy", "-c", str(compose), cfg.stack_name])
    else:
        print(f"  no {COMPOSE_NAME} found")


# ---------------------------------------------------------------------------
# backup_volumes
# ---------------------------------------------------------------------------


def cmd_backup_volumes(cfg: Config, args):
    run = Runner(dry_run=args.dry_run)
    for d in sorted((cfg.stack_dir / "volumes").glob("*")):
        if d.is_dir():
            vol = f"{cfg.stack_name}_{d.name}"
            print(f"save volume {vol}")
            volume_backup(cfg, run, vol, d)
            run.local(["docker", "rm", "rsync"], check=False)
    if run.errors and not args.dry_run:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="stackctl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, **kw):
        # `--remote` / `--dry-run` are accepted on every subcommand.
        sp.add_argument("--remote", help="override remote host (e.g. root@host)")
        sp.add_argument("--dry-run", action="store_true", help="print commands without running")
        sp.add_argument("--dryrun", action="store_true", help=argparse.SUPPRESS)

    d = sub.add_parser("download", help="back up the stack (rsync + docker state)")
    add_common(d)
    d.add_argument("--skip", action="store_true", help="skip the tree rsync")
    d.add_argument("--skip-rotation", action="store_true", help="keep all snapshots")
    d.set_defaults(func=cmd_download)

    u = sub.add_parser("upload", help="push the stack to the remote")
    add_common(u)
    u.set_defaults(func=cmd_upload)

    i = sub.add_parser("install", help="install on the manager")
    add_common(i)
    i.add_argument("role", nargs="?", choices=["manager", "worker"], default="manager",
                   help="swarm role to install for (default: manager)")
    i.add_argument("--configs-only", action="store_true")
    i.add_argument("--volumes-only", action="store_true")
    i.add_argument("--nfs-only", action="store_true")
    i.add_argument("--deploy-only", action="store_true")
    i.set_defaults(func=cmd_install)

    b = sub.add_parser("backup-volumes", help="back up named volumes to volumes/")
    add_common(b)
    b.set_defaults(func=cmd_backup_volumes)

    return p


def main(argv=None):
    _run_relaunch()
    args = build_parser().parse_args(argv)
    root = Path.cwd()
    cfg = Config(root, args.remote)
    if args.dry_run:
        print("dry-run: operating on stack '{}' in {}".format(cfg.stack_name, root))
    args.func(cfg, args)


if __name__ == "__main__":
    main()