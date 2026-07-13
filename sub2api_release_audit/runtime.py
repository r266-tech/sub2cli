from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


GITHUB_API = "https://api.github.com"
VERSION_RE = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
BUILD_VERSION_RE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z_.-]+)?"
)
GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PRODUCTION_BRANCH = "codex/sub2api-production-baseline"
CANDIDATE_REF_RE = re.compile(
    r"refs/heads/codex/sub2api-v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)-candidate"
)
REMOTE_PROBE = r"""
set -euo pipefail
repo="$1"
container="$2"
candidate_ref="$3"
official_commit="$4"
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
image="$(docker inspect "$container" --format '{{.Config.Image}}')"
image_id="$(docker inspect "$container" --format '{{.Image}}')"
image_version="$(docker image inspect "$image_id" --format '{{with index .Config.Labels "org.opencontainers.image.version"}}{{.}}{{end}}')"
image_revision="$(docker image inspect "$image_id" --format '{{with index .Config.Labels "org.opencontainers.image.revision"}}{{.}}{{end}}')"
branch="$(git -C "$repo" branch --show-current)"
head="$(git -C "$repo" rev-parse HEAD)"
main_head="$(git -C "$repo" rev-parse main)"
candidate_head="$(git -C "$repo" rev-parse --verify "${candidate_ref}^{commit}" 2>/dev/null || true)"
revision_exists=false
revision_is_ancestor=false
official_commit_known=false
deployed_contains_official=false
candidate_contains_official=false
if [[ "$image_revision" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] && git -C "$repo" cat-file -e "${image_revision}^{commit}" 2>/dev/null; then
  revision_exists=true
  if git -C "$repo" merge-base --is-ancestor "$image_revision" "$head"; then
    revision_is_ancestor=true
  fi
fi
if [[ "$official_commit" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] && git -C "$repo" cat-file -e "${official_commit}^{commit}" 2>/dev/null; then
  official_commit_known=true
  if [[ "$revision_exists" == true ]] && git -C "$repo" merge-base --is-ancestor "$official_commit" "$image_revision"; then
    deployed_contains_official=true
  fi
  if [[ -n "$candidate_head" ]] && git -C "$repo" merge-base --is-ancestor "$official_commit" "$candidate_head"; then
    candidate_contains_official=true
  fi
fi
printf 'image\t%s\n' "$image"
printf 'image_id\t%s\n' "$image_id"
printf 'image_version_label\t%s\n' "$image_version"
printf 'image_revision_label\t%s\n' "$image_revision"
printf 'production_branch\t%s\n' "$branch"
printf 'production_head\t%s\n' "$head"
printf 'official_main_head\t%s\n' "$main_head"
printf 'candidate_head\t%s\n' "$candidate_head"
printf 'revision_exists\t%s\n' "$revision_exists"
printf 'revision_is_ancestor\t%s\n' "$revision_is_ancestor"
printf 'official_commit_known\t%s\n' "$official_commit_known"
printf 'deployed_contains_official\t%s\n' "$deployed_contains_official"
printf 'candidate_contains_official\t%s\n' "$candidate_contains_official"
"""


class AuditError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def image_tag(value: str) -> str | None:
    if "@" in value:
        return None
    leaf = value.rsplit("/", 1)[-1]
    _name, separator, tag = leaf.rpartition(":")
    return tag if separator and tag else None


def parse_build_version(value: str) -> tuple[int, int, int] | None:
    match = BUILD_VERSION_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def parse_image_version(value: str) -> tuple[int, int, int] | None:
    tag = image_tag(value)
    return parse_build_version(tag) if tag else None


def version_text(value: tuple[int, int, int] | None) -> str | None:
    return ".".join(str(part) for part in value) if value else None


def _http_json(url: str, *, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sub2cli-sub2api-release-audit/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise AuditError("invalid_release_response", "GitHub returned an invalid object")
    return payload


def latest_release(
    repo: str,
    *,
    timeout: int = 15,
    get_json: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not REPO_RE.fullmatch(repo):
        raise AuditError("invalid_repo", "repository must use owner/name format")
    getter = get_json or (lambda url: _http_json(url, timeout=timeout))
    release = getter(f"{GITHUB_API}/repos/{repo}/releases/latest")
    if release.get("draft") is True or release.get("prerelease") is True:
        raise AuditError("unstable_release", "GitHub latest release is not stable")
    tag = str(release.get("tag_name") or "")
    parsed = parse_version(tag)
    if not tag or parsed is None:
        raise AuditError("invalid_release_tag", "latest release has no semantic version tag")

    ref = getter(f"{GITHUB_API}/repos/{repo}/git/ref/tags/{quote(tag, safe='')}")
    target = ref.get("object")
    if not isinstance(target, dict) or not target.get("sha") or not target.get("type"):
        raise AuditError("invalid_release_ref", "release tag reference is invalid")
    seen_tags: set[str] = set()
    for _ in range(8):
        if target.get("type") == "commit":
            break
        if target.get("type") != "tag" or not target.get("sha"):
            raise AuditError("invalid_release_ref", "release tag does not resolve to a commit")
        tag_sha = str(target["sha"])
        if tag_sha in seen_tags:
            raise AuditError("invalid_release_ref", "release tag chain contains a cycle")
        seen_tags.add(tag_sha)
        tag_object = getter(
            f"{GITHUB_API}/repos/{repo}/git/tags/{quote(tag_sha, safe='')}"
        )
        target = tag_object.get("object")
        if not isinstance(target, dict):
            raise AuditError("invalid_release_ref", "annotated release tag is invalid")
    else:
        raise AuditError("invalid_release_ref", "release tag chain is too deep")
    if target.get("type") != "commit" or not target.get("sha"):
        raise AuditError("invalid_release_ref", "release tag does not resolve to a commit")
    commit = str(target["sha"])
    if not GIT_SHA_RE.fullmatch(commit):
        raise AuditError("invalid_release_ref", "release commit SHA is invalid")

    digest_payload = {
        "tag": tag,
        "commit": commit,
        "published_at": release.get("published_at"),
        "body": release.get("body") or "",
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "tag": tag,
        "version": version_text(parsed),
        "commit": commit,
        "published_at": release.get("published_at"),
        "url": release.get("html_url"),
        "release_digest": f"sha256:{digest}",
    }


def parse_probe_output(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("\t")
        if not separator or not key or key in result:
            raise AuditError("invalid_home_probe", "home probe output is invalid")
        result[key] = value
    required = {
        "image",
        "image_id",
        "image_version_label",
        "image_revision_label",
        "production_branch",
        "production_head",
        "official_main_head",
        "candidate_head",
        "revision_exists",
        "revision_is_ancestor",
        "official_commit_known",
        "deployed_contains_official",
        "candidate_contains_official",
    }
    if set(result) != required:
        raise AuditError("invalid_home_probe", "home probe output is incomplete")
    return result


def probe_home(
    *,
    host: str,
    repo_path: str,
    container: str,
    candidate_ref: str,
    official_commit: str,
    timeout: int = 15,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    for label, value in {
        "host": host,
        "repo_path": repo_path,
        "container": container,
        "candidate_ref": candidate_ref,
        "official_commit": official_commit,
    }.items():
        if not value or "\n" in value or "\r" in value:
            raise AuditError("invalid_probe_config", f"{label} is invalid")
    if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
        raise AuditError("invalid_probe_config", "host is invalid")
    if not 1 <= timeout <= 120:
        raise AuditError("invalid_probe_config", "timeout is invalid")
    if container.startswith("-") or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", container
    ):
        raise AuditError("invalid_probe_config", "container is invalid")
    if not CANDIDATE_REF_RE.fullmatch(candidate_ref):
        raise AuditError("invalid_probe_config", "candidate_ref is invalid")
    if not GIT_SHA_RE.fullmatch(official_commit):
        raise AuditError("invalid_probe_config", "official_commit is invalid")
    remote_command = "bash -s -- " + " ".join(
        shlex.quote(value)
        for value in (repo_path, container, candidate_ref, official_commit)
    )
    try:
        completed = runner(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={min(timeout, 30)}",
                host,
                remote_command,
            ],
            input=REMOTE_PROBE,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError("home_unreachable", "home release probe could not run") from exc
    if completed.returncode != 0:
        raise AuditError("home_probe_failed", "home release probe failed")
    values = parse_probe_output(completed.stdout)
    if values["production_branch"] != PRODUCTION_BRANCH:
        raise AuditError(
            "production_branch_mismatch",
            "runtime repository is not on the production branch",
        )
    tag = image_tag(values["image"])
    parsed = parse_image_version(values["image"])
    label_parsed = parse_build_version(values["image_version_label"])
    if parsed is None or label_parsed is None:
        raise AuditError("deployed_version_unknown", "deployed image version is not verifiable")
    if tag != values["image_version_label"] or parsed != label_parsed:
        raise AuditError("deployed_image_mismatch", "image tag and OCI version label disagree")
    if not IMAGE_ID_RE.fullmatch(values["image_id"]):
        raise AuditError("deployed_image_mismatch", "deployed image ID is invalid")
    if not GIT_SHA_RE.fullmatch(values["image_revision_label"]):
        raise AuditError("deployed_revision_unknown", "deployed image revision label is invalid")
    if values["revision_exists"] != "true" or values["revision_is_ancestor"] != "true":
        raise AuditError(
            "deployed_revision_mismatch",
            "deployed image revision is not an ancestor of the production revision",
        )
    for key in {
        "official_commit_known",
        "deployed_contains_official",
        "candidate_contains_official",
    }:
        if values[key] not in {"true", "false"}:
            raise AuditError("invalid_home_probe", "home probe boolean is invalid")
    for key in {"production_head", "official_main_head"}:
        if not GIT_SHA_RE.fullmatch(values[key]):
            raise AuditError("invalid_home_probe", "home probe revision is invalid")
    if values["candidate_head"] and not GIT_SHA_RE.fullmatch(values["candidate_head"]):
        raise AuditError("invalid_home_probe", "candidate revision is invalid")
    return {
        "image": values["image"],
        "image_id": values["image_id"],
        "version": version_text(parsed),
        "version_label": values["image_version_label"],
        "image_revision": values["image_revision_label"],
        "production_branch": values["production_branch"],
        "production_head": values["production_head"],
        "official_main_head": values["official_main_head"],
        "candidate_ref": candidate_ref,
        "candidate_head": values["candidate_head"] or None,
        "official_commit_known": values["official_commit_known"] == "true",
        "contains_official_release": values["deployed_contains_official"] == "true",
        "candidate_contains_official_release": values["candidate_contains_official"]
        == "true",
    }


def run_audit(
    *,
    repo: str = "Wei-Shaw/sub2api",
    host: str = "home",
    home_repo: str = "/Users/wzr/code/sub2api",
    container: str = "sub2api",
    timeout: int = 15,
) -> dict[str, Any]:
    official = latest_release(repo, timeout=timeout)
    official_version = parse_version(str(official["tag"]))
    assert official_version is not None
    version_slug = ".".join(str(part) for part in official_version)
    candidate_ref = f"refs/heads/codex/sub2api-v{version_slug}-candidate"
    deployed = probe_home(
        host=host,
        repo_path=home_repo,
        container=container,
        candidate_ref=candidate_ref,
        official_commit=str(official["commit"]),
        timeout=timeout,
    )
    deployed_version = parse_version(str(deployed["version"]))
    if deployed_version is None:
        raise AuditError("deployed_version_unknown", "deployed image version is not verifiable")
    if official_version > deployed_version:
        update_available = True
        status = "update_available"
    elif official_version == deployed_version:
        update_available = False
        status = "current"
    else:
        update_available = False
        status = "deployed_ahead"
    if official_version <= deployed_version:
        if not deployed["official_commit_known"]:
            raise AuditError(
                "official_commit_unavailable",
                "official release commit is not available on the runtime host",
            )
        if not deployed["contains_official_release"]:
            raise AuditError(
                "official_revision_mismatch",
                "deployed image does not contain the official release commit",
            )
    if not deployed["candidate_head"]:
        candidate_status = "absent"
    elif not deployed["official_commit_known"]:
        candidate_status = "official_commit_unavailable"
    elif deployed["candidate_contains_official_release"]:
        candidate_status = "based_on_official_release"
    else:
        candidate_status = "stale_or_diverged"
    return {
        "schema": 1,
        "ok": True,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": status,
        "update_available": update_available,
        "requires_attention": status == "deployed_ahead",
        "candidate_status": candidate_status,
        "official": official,
        "deployed": deployed,
    }
