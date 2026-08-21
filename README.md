# system stack

[![LICENSE: MIT](https://img.shields.io/github/license/rylorin/system)](https://raw.githubusercontent.com/rylorin/system/master/LICENSE)
[![GitHub contributors](https://img.shields.io/github/contributors/rylorin/system)](https://github.com/rylorin/system/graphs/contributors)

## About this project

Core Docker Swarm services for my projects, plus a Python-based toolkit to
back up, upload, install and manage stacks from the command line.
The `bin/stackctl.py` tool lives here but can operate on **any** stack by
running it from that stack's directory (stack name = basename of the CWD).

Requires: `python3`, `pyyaml` (`pip install pyyaml`).

## Services

### bind

DNS server (sameersbn/bind).

### smtp

Postfix relay (rylorin/postfix-relay).

### websites

Reverse proxy and static file server (Caddy 2.11.4).

## Commands

All commands are wrappers around `bin/stackctl.py` and accept the same flags.
Run `bin/stackctl.py <cmd> --help` for details.

| Wrapper              | Action                                                                                                                       |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `bin/download`       | Back up the stack: rsync the whole tree, capture docker state, configs and named volumes into `backup/<timestamp>/`          |
| `bin/upload`         | Push the stack to the remote (rsync) and run `bin/postupload` if present                                                     |
| `bin/install`        | Install on a manager: load configs, build volumes, configure NFS, deploy the stack. Pass `worker` to skip configs/NFS/deploy |
| `bin/backup-volumes` | Back up named volumes into the local `volumes/` tree (for stacks using volume files)                                         |
| `bin/postinstall`    | Stack-specific post-install hook, called at the end of `install`                                                             |

Common flags:

- `--remote root@host` override the remote set in `stack.yaml`
- `--dry-run` show commands without executing them

## Hierarchy

```
bin/                  stackctl.py + wrappers + stack-specific hooks
configs/              docker config source files (Caddyfile, virtual, …)
volumes/              files that populate a named Docker volume on install
volumes_nfs/          NFS-shared data wrapped in a "local" Docker volume
backup/               (gitignored) snapshots produced by download
docker-compose.yml    stack specification (secrets via ${VAR} from .env)
stack.yaml            per-stack config (remote, nfs_nodes, …)
.env                  secrets, NOT committed (.env.example shows the format)
requirements.txt      pyyaml (optional, but required for full compose parsing)
```

## Backup (`download`)

`bin/download` creates `backup/<YYYYMMDD-HHMMSS>/` containing:

- `docker-state.json` swarm info + every service inspect
- `configs/<name>/data` decoded docker config content
- `volumes/<name>/` content of named volumes not bound to the repo tree
- `rebuild.sh` aide-mémoire for restoring on a fresh node
- `backup.log` full log of the backup session

Rotation is controlled by `backup_keep` in `stack.yaml` (default 7).

## Rebuild on a fresh VPS

1. Push the repo: `bin/upload --remote root@new-host`
2. On the host: `cd /root/<stack> && bin/install manager`
3. Restore named volumes and configs from the `backup/` snapshot
   (see `backup/<ts>/rebuild.sh` for reference commands)

## Secrets

`ROOT_PASSWORD` and other sensitive values are referenced as `${ROOT_PASSWORD}`
in `docker-compose.yml` and sourced from `.env` (gitignored). Copy
`.env.example` to `.env` and fill in the real values.

## License

MIT
