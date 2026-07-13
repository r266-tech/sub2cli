from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

from sub2api_release_audit.cli import main as cli_main
from sub2api_release_audit.runtime import (
    AuditError,
    latest_release,
    parse_image_version,
    parse_probe_output,
    parse_version,
    probe_home,
    run_audit,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
IMAGE_ID = "sha256:" + "d" * 64


def probe_output(
    *,
    image: str = f"sub2api-local:0.1.151-babata.{SHA_A[:12]}",
    version_label: str = f"0.1.151-babata.{SHA_A[:12]}",
    revision: str = SHA_A,
    candidate_head: str = SHA_C,
    official_known: str = "true",
    deployed_contains: str = "false",
    candidate_contains: str = "true",
) -> str:
    fields = {
        "image": image,
        "image_id": IMAGE_ID,
        "image_version_label": version_label,
        "image_revision_label": revision,
        "production_branch": "codex/sub2api-production-baseline",
        "production_head": SHA_B,
        "official_main_head": SHA_C,
        "candidate_head": candidate_head,
        "revision_exists": "true",
        "revision_is_ancestor": "true",
        "official_commit_known": official_known,
        "deployed_contains_official": deployed_contains,
        "candidate_contains_official": candidate_contains,
    }
    return "\n".join(f"{key}\t{value}" for key, value in fields.items())


def deployed(version: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "image": f"sub2api-local:{version}-babata.{SHA_A[:12]}",
        "version": version,
        "candidate_head": SHA_C,
        "official_commit_known": True,
        "contains_official_release": False,
        "candidate_contains_official_release": True,
    }
    result.update(overrides)
    return result


class ReleaseAuditTests(unittest.TestCase):
    def test_version_parsing_is_strict_and_uses_only_image_tag(self) -> None:
        self.assertEqual(parse_version("v0.1.152"), (0, 1, 152))
        self.assertIsNone(parse_version("release-v0.1.152-extra"))
        self.assertEqual(
            parse_image_version(
                "registry.1.2.3.example:5000/team/sub2api:0.1.151-babata.abc"
            ),
            (0, 1, 151),
        )
        self.assertIsNone(parse_image_version("registry.1.2.3.example:5000/sub2api:latest"))

    def test_latest_release_peels_nested_annotated_tags(self) -> None:
        responses = {
            "https://api.github.com/repos/Wei-Shaw/sub2api/releases/latest": {
                "tag_name": "v0.1.152",
                "published_at": "2026-07-13T02:52:49Z",
                "html_url": "https://example.test/release",
                "body": "notes",
                "draft": False,
                "prerelease": False,
            },
            "https://api.github.com/repos/Wei-Shaw/sub2api/git/ref/tags/v0.1.152": {
                "object": {"type": "tag", "sha": "tag-one"}
            },
            "https://api.github.com/repos/Wei-Shaw/sub2api/git/tags/tag-one": {
                "object": {"type": "tag", "sha": "tag-two"}
            },
            "https://api.github.com/repos/Wei-Shaw/sub2api/git/tags/tag-two": {
                "object": {"type": "commit", "sha": SHA_A}
            },
        }
        result = latest_release("Wei-Shaw/sub2api", get_json=responses.__getitem__)
        self.assertEqual(result["version"], "0.1.152")
        self.assertEqual(result["commit"], SHA_A)
        self.assertTrue(result["release_digest"].startswith("sha256:"))

    def test_latest_release_accepts_lightweight_tag(self) -> None:
        responses = {
            "https://api.github.com/repos/Wei-Shaw/sub2api/releases/latest": {
                "tag_name": "v0.1.152",
                "draft": False,
                "prerelease": False,
            },
            "https://api.github.com/repos/Wei-Shaw/sub2api/git/ref/tags/v0.1.152": {
                "object": {"type": "commit", "sha": SHA_A}
            },
        }
        result = latest_release("Wei-Shaw/sub2api", get_json=responses.__getitem__)
        self.assertEqual(result["commit"], SHA_A)

    def test_probe_output_requires_exact_unique_contract(self) -> None:
        output = probe_output()
        self.assertEqual(parse_probe_output(output)["image_id"], IMAGE_ID)
        with self.assertRaises(AuditError):
            parse_probe_output(output + "\nimage\tduplicate")

    def test_home_probe_binds_image_labels_and_source_revision(self) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=probe_output(), stderr=""
            )
        )
        result = probe_home(
            host="home",
            repo_path="/Users/wzr/code/sub2api",
            container="sub2api",
            candidate_ref="refs/heads/codex/sub2api-v0.1.152-candidate",
            official_commit=SHA_C,
            runner=runner,
        )
        self.assertEqual(result["version"], "0.1.151")
        self.assertEqual(result["image_revision"], SHA_A)
        self.assertTrue(result["candidate_contains_official_release"])
        self.assertIn("docker inspect", runner.call_args.kwargs["input"])
        self.assertIn('docker image inspect "$image_id"', runner.call_args.kwargs["input"])
        self.assertNotIn(
            'docker inspect "$container" --format \'{{with index .Config.Labels',
            runner.call_args.kwargs["input"],
        )

    def test_home_probe_rejects_unknown_or_mismatched_image(self) -> None:
        for output, code in (
            (
                probe_output(image="sub2api-local:latest", version_label="latest"),
                "deployed_version_unknown",
            ),
            (
                probe_output(version_label="0.1.150-babata.abc"),
                "deployed_image_mismatch",
            ),
        ):
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            )
            with self.subTest(code=code), self.assertRaises(AuditError) as raised:
                probe_home(
                    host="home",
                    repo_path="/Users/wzr/code/sub2api",
                    container="sub2api",
                    candidate_ref="refs/heads/codex/sub2api-v0.1.152-candidate",
                    official_commit=SHA_C,
                    runner=runner,
                )
            self.assertEqual(raised.exception.code, code)

    def test_home_probe_rejects_ssh_option_destination(self) -> None:
        with self.assertRaises(AuditError) as raised:
            probe_home(
                host="-G",
                repo_path="/Users/wzr/code/sub2api",
                container="sub2api",
                candidate_ref="refs/heads/codex/sub2api-v0.1.152-candidate",
                official_commit=SHA_C,
            )
        self.assertEqual(raised.exception.code, "invalid_probe_config")

    def test_audit_reports_update_and_collision_free_candidate_ref(self) -> None:
        with mock.patch(
            "sub2api_release_audit.runtime.latest_release",
            return_value={"tag": "v0.1.152", "version": "0.1.152", "commit": SHA_C},
        ), mock.patch(
            "sub2api_release_audit.runtime.probe_home",
            return_value=deployed("0.1.151"),
        ) as probe:
            result = run_audit()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["candidate_status"], "based_on_official_release")
        self.assertEqual(
            probe.call_args.kwargs["candidate_ref"],
            "refs/heads/codex/sub2api-v0.1.152-candidate",
        )

    def test_audit_current_and_ahead_require_official_ancestry(self) -> None:
        official = {"tag": "v0.1.152", "version": "0.1.152", "commit": SHA_C}
        for version, expected in (("0.1.152", "current"), ("0.2.0", "deployed_ahead")):
            with mock.patch(
                "sub2api_release_audit.runtime.latest_release", return_value=official
            ), mock.patch(
                "sub2api_release_audit.runtime.probe_home",
                return_value=deployed(version, contains_official_release=True),
            ):
                result = run_audit()
            self.assertEqual(result["status"], expected)
        self.assertTrue(result["requires_attention"])

        with mock.patch(
            "sub2api_release_audit.runtime.latest_release", return_value=official
        ), mock.patch(
            "sub2api_release_audit.runtime.probe_home",
            return_value=deployed("0.1.152", contains_official_release=False),
        ), self.assertRaises(AuditError) as raised:
            run_audit()
        self.assertEqual(raised.exception.code, "official_revision_mismatch")

    def test_unknown_version_is_nonzero_at_cli_boundary(self) -> None:
        with mock.patch(
            "sub2api_release_audit.cli.run_audit",
            side_effect=AuditError(
                "deployed_version_unknown", "deployed image version is not verifiable"
            ),
        ), redirect_stderr(io.StringIO()) as stderr:
            exit_code = cli_main([])
        self.assertEqual(exit_code, 2)
        self.assertIn('"deployed_version_unknown"', stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
