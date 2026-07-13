# Upstream reconciler autonomy contract

This contract is consumed by the hourly Codex automation. Routine runs
remain deterministic. Intelligent source maintenance is allowed only for a
confirmed upstream response-schema change.

## Business invariants

- Only the explicitly allowlisted subscriptions may be managed.
- A Sub2API subscription must be active, started, and not expired.
- A new or returning group is not exposed to Relay until a dedicated managed
  probe key completes a real Codex-compatible `/v1/responses` stream.
- An unchanged active group reuses its bound proof and is not probed again.
  Multiplier-only changes recalculate priority without spending inference quota.
- A valid `/v1/models` catalog with no GPT/Codex text model defers the group
  before the paid probe. Missing or broken model catalogs fall through to the
  exact-model probe because some otherwise compatible relays omit this endpoint.
- A failed candidate probe is a normal deferred result. Keep it out of Relay
  and retry it once on the next scheduled run.
- A missing group is quarantined first and deleted only after the configured
  grace period and consecutive complete-scan confirmations. In production the
  intended policy is two hourly confirmations over about two hours.
- Priority tiers are computed only from groups that have passed the dedicated
  compatibility probe. Pending or failed candidates never reserve a tier.
- Qualified metered groups use `ceil(multiplier * 1000)`, with the boundary
  clamped around the configurable `target.subscription_priority` (default
  `40`). Values below `0.04x` stay in `1..39`, exactly `0.04x` shares priority
  `40` with subscriptions, and values above it start at `41`. Thus `0.02x ->
  20`, `0.04x -> 40`, subscription `-> 40`, `0.15x -> 150`, and `1x -> 1000`.
  Equal multipliers share the same priority, and adding another group never
  renumbers existing routes. Changing the one fixed subscription priority also
  moves the matching multiplier boundary.
- Each provider's configured target-account concurrency is enforced for both
  existing and newly created managed accounts, and is included in verification
  and rollback snapshots.
- Never weaken ownership markers, deletion grace, confirmation counts,
  rollback, or secret redaction while repairing an adapter.

## Error decision table

| Error | Scheduled action |
| --- | --- |
| `auth_required`, `credential_missing`, `interactive_auth_required` | Do not edit code or use a browser. The CLI sends a deduplicated Telegram alert. Report and stop. |
| `rate_limited`, `upstream_unavailable` | Leave state unchanged and retry next schedule. Do not edit code. |
| `partial_external_mutation`, `degraded_rollback`, pending recovery | Do not edit code. Run only the documented recovery path, report, and stop. |
| Candidate probe failure | Continue syncing other resources; candidate stays outside Relay and is retried next schedule. |
| `schema_changed` | Enter the bounded repair gate below. |
| Any other error | Fail closed and report. Do not infer schema drift. |

HTTP 404/405, an unexpected success envelope, `unclassified_group`, a missing
multiplier, and any generic parsing or transport failure are not
`schema_changed`. They must never be relabelled to enter maintenance.

## Scheduled orchestration order

Every hourly run first calls `maintenance status`. A `verified` or
`push_pending` gate must finish `maintenance promote` before any new scan; a
`prepared` gate must resume only its recorded worktree; an interrupted
`verifying` or `applying` gate stops and notifies rather than starting another
gate. With no active gate, run the read-only `doctor` command. If `doctor`
succeeds, run the ordinary `apply --yes` workflow and stop; do not enter source
maintenance. If and only if `doctor` returns a validated `schema_changed`, use
`maintenance prepare`, let the repair agent edit only the returned worktree,
then call `maintenance verify` and `maintenance promote`. For every other error,
follow the decision table and never try `apply` first as a schema probe.

```bash
./sub2cli-reconcile-upstreams maintenance status
```

## Bounded schema-repair hard gate

The automation must use the commands below. A prompt description of equivalent
checks is not sufficient and direct `git commit`, `git push`, or `apply --yes`
is not permitted during schema maintenance.

### 1. Prepare

```bash
./sub2cli-reconcile-upstreams maintenance prepare --notify-on-failure
```

When a non-default private config is required, place the global option before
the subcommand:

```bash
./sub2cli-reconcile-upstreams --config "$CONFIG" maintenance prepare --notify-on-failure
```

`prepare` is read-only with respect to upstreams and Relay. It requires:

- local branch `main`, no tracked or unrelated untracked changes, and local
  `HEAD == origin/main` after fetch;
- two consecutive `doctor` observations with the exact same provider, exact
  allowlisted endpoint, and 64-hex sanitized schema fingerprint;
- both observations to remain exactly `schema_changed`; HTTP 404/405 and all
  unclassified errors are rejected;
- `status.pending_recovery == false` and a valid active-resource baseline;
- a second repository SHA/cleanliness check after both observations.

Only then does it create a private manifest plus an isolated
`codex/upstream-schema-repair-<timestamp>` worktree. The JSON result contains
the `gate_id` and the only worktree the repair agent may edit.

### 2. Edit the returned worktree

Inspect through the existing authenticated client without printing or saving
credentials, keys, cookies, account identifiers, or raw response bodies. Make
one narrow adapter repair and synthetic regression evidence. The hard allowlist
is:

- `upstream_reconciler/clients.py`;
- `tests/test_upstream_reconciler.py` (this file must be changed and contain the
  focused regression evidence);
- new JSON fixtures below `tests/fixtures/upstream_schema/`.

No file may be pre-staged, deleted, renamed, or symlinked. Private config,
priority rules, allowlists, deletion policy, scheduler settings, notification
recipients, and unrelated code are outside the repair boundary.

### 3. Verify and apply exactly once

```bash
./sub2cli-reconcile-upstreams maintenance verify \
  --gate-id "$GATE_ID" --notify-on-failure
```

`verify` is one-shot. It rechecks the recorded local/origin SHA CAS, branch,
worktree identity, file allowlist, and added-line secret patterns. It then runs
Python compilation, the focused reconciler suite, the full suite, and
`git diff --check`; reruns the diff and secret checks; and runs `doctor` plus
`plan` from the repair worktree.

The plan is rejected if it deletes or confirms an absent upstream key,
quarantines a resource, or reduces the active-resource baseline. Only after all
checks pass does the gate stage and create exactly one repair commit, record
the apply attempt durably, execute one internally constrained
`apply --yes --maintenance-safe`, and require the fresh plan built inside the
runtime lock to pass the same non-destructive action and active-resource floor
checks before target, quarantine, or deletion mutations. It then requires
`status.pending_recovery == false` with no loss of active resources. A failed
verify cannot be retried under the same gate ID.

### 4. Promote by CAS and ordinary push

```bash
./sub2cli-reconcile-upstreams maintenance promote \
  --gate-id "$GATE_ID" --notify-on-failure
```

`promote` accepts only a verified gate whose worktree is clean and whose branch
contains exactly one allowlisted commit. It fetches again and requires
`origin/main` and local `main` to still equal the recorded start SHA. It then
uses a normal, non-force push of the verified commit to `main`, reads the remote
SHA back, and fast-forwards local `main`. If either main moved, the command
stops without pushing anything. Push intent is persisted as `push_pending`;
after a lost response or process restart, promotion reads the remote SHA first.
It retries a normal push only when remote `main` is still the recorded base,
and only finishes the local fast-forward when remote already equals the exact
candidate commit.

Any pre-push gate failure leaves code unpromoted and blocks further phases for
that gate. `push_pending` is intentionally treated as indeterminate until a
remote SHA read-back proves whether promotion occurred. With
`--notify-on-failure`, the configured Telegram command receives only a sanitized
phase/provider/error summary. A clever diagnosis is not permission to expand
the repair scope or bypass these commands.

This hard gate is an operational guard against mistakes, stale observations,
races, and interrupted runs inside the trusted automation account. It is not a
privilege boundary against a deliberately hostile process running as the same
macOS user; that stronger threat model would require a separately privileged
executor or signing service.
