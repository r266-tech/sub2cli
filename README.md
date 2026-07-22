# sub2cli (retired)

> [!IMPORTANT]
> **End of life: 2026-07-22.** Do not install, run, restore, or use sub2cli.

This repository is retained only as historical source for audit, incident
response, and explicitly approved rollback. It is not a supported product and
must not be an operational dependency of any current project.

## Hard boundary

- sub2cli is not part of Sub2API.
- Do not route Sub2API server, upstream, release, automation, credential, or
  Codex-profile work to this repository.
- Do not run the desktop app, CLI, injector, response proxy, route pool,
  provider-slot tooling, or bootstrap scripts.
- `install.sh` and `install.ps1` on `main` intentionally terminate before
  modifying the machine.
- Historical tags, releases, screenshots, and source are not recommendations
  to reinstall the project.

Sub2API operations belong only to the dedicated operations-controller project
and its documented, independently verified flows.

## For agents

If the current request does not explicitly name sub2cli retirement, rollback,
security, archive, or history, leave this repository and locate the project the
user actually named. Do not infer a Sub2API task from the historical source.

See [RETIREMENT.md](RETIREMENT.md) for the isolation status, quarantine
boundary, remaining gated actions, and rollback rule. Previous product
documentation remains available in Git history.
