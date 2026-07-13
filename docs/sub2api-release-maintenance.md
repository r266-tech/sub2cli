# Sub2API release maintenance contract

The current workstation is the release-control plane. The home Mac is the
runtime host and keeps only watchdog and backup jobs.

## Discovery

Run the read-only audit instead of trusting the admin UI or an image tag alone:

```bash
./sub2cli-check-sub2api-release
```

The JSON result verifies the official stable GitHub release tag and peeled
commit. On the home Mac it also binds the running image ID and tag to the OCI
version/revision labels, verifies that the image revision belongs to the
production Git history, and reports whether the version-specific candidate
contains the official release commit.

Only `current` and `update_available` are routine outcomes. An unknown or
mismatched image/revision fails with a non-zero exit. `deployed_ahead` is a
successful observation but has `requires_attention: true`; hold all automated
actions until it is explained.

## New-release gate

Never use `latest` or the admin dashboard's immediate-update action for the
custom deployment. A newer release must be prepared from its immutable tag in
an isolated worktree. Review release notes, migrations, and the range against
the custom production branch; retire patches already upstream and preserve the
remaining local contracts. Candidate branches use the full semantic version,
for example `codex/sub2api-v0.1.152-candidate`, so future minor versions cannot
collide.

Before production promotion require a recoverable database backup, migration
test on a restored copy, backend/frontend/ops tests, immutable candidate image,
candidate health, public settings, administrator authentication, account-pool
concurrency, model requests, panic/log checks, and a tested rollback target.
Production promotion remains a separate explicit gate and may change only the
application container.
