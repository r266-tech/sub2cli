# sub2cli retirement

Status: retired / end of life on 2026-07-22.

sub2cli is no longer an active client, control plane, configuration source, or
operational dependency. It must not participate in Sub2API server operations,
upstream management, releases, automation, or Codex profile management.

## Disabled surfaces

- Installed desktop app, PATH-visible commands, caches, logs, and clearly owned
  legacy state were moved into a user-private quarantine.
- The CLI, injector, desktop entry point, desktop build, and both installers on
  `main` are fail-closed tombstones that terminate before doing operational
  work.
- Automatic GitHub workflow triggers are disabled.
- The repository must not be suggested to agents for Sub2API or Codex-profile
  work.
- Historical source, tags, releases, and backups are retained only for audit,
  incident response, or an explicitly approved rollback.

Exact local paths and rollback mappings are intentionally kept in a private
quarantine manifest, not in this public repository.

## Boundary

Sub2API is an independent server project. Its runtime and operations must not
read, execute, import, or depend on this repository. Sub2API operations belong
only to the dedicated operations-controller project and its documented flows.

Sub2API development containers and production runtimes are outside this
retirement action. They must not be stopped, reconfigured, or mutated as a side
effect of retiring sub2cli.

## Remaining gated actions

The following are deliberately separate operations and require explicit scope
and verification before execution:

1. Quit ChatGPT/Codex, replace the retired tool's application-profile indirection
   with an independent canonical profile directory, and verify a clean reopen.
2. Rotate credentials present in legacy state or exposed by the retirement
   audit without printing or publishing their values.
3. After rotation and ownership checks, privately quarantine or remove the
   remaining legacy profile/account mappings and ambiguous backups by exact
   path. Shared Codex state must remain untouched.
4. Publish the terminal EOL commit, mark the final release as retired, close
   obsolete collaboration surfaces, and archive the GitHub repository.
5. After active tasks have exited, move local checkouts out of normal project
   roots so they cannot be selected as a current workspace by accident.

None of those gated actions is implied merely by this source-tree change.

## Rollback rule

Rollback is exceptional. It requires an explicit request naming the exact
surface to restore, a fresh review of credential and profile safety, and proof
that Sub2API remains independent. Do not restore the entire quarantine or
re-enable operational code as a convenience.
