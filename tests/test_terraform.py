"""Infrastructure checks for infra/terraform, run as part of the unit tests.

`terraform validate` needs the Terraform binary and the AWS provider, which
are not available in every environment, so this test parses the HCL directly
and asserts the properties the deployment depends on: a private encrypted
bucket, a dead-lettered queue, and scoped IAM roles with no access keys.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "terraform"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import hcl2
except ImportError:  # pragma: no cover - dev-only dependency
    hcl2 = None


def _unquote(text: str) -> str:
    return text.strip().strip('"')


def _load(filename: str) -> dict[str, Any]:
    return hcl2.loads((TF_DIR / filename).read_text(encoding="utf-8"))


def _blocks(parsed: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    """{ "aws_s3_bucket.media": body } for every block in a section."""
    found: dict[str, dict[str, Any]] = {}
    for entry in parsed.get(section, []):
        for kind, bodies in entry.items():
            for name, body in bodies.items():
                found[f"{_unquote(kind)}.{_unquote(name)}"] = body
    return found


@unittest.skipIf(hcl2 is None, "python-hcl2 is not installed (pip install -r requirements-dev.txt)")
class TerraformStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = _load("main.tf")
        cls.resources = _blocks(cls.main, "resource")
        cls.data = _blocks(cls.main, "data")

    def test_the_configuration_parses(self) -> None:
        self.assertTrue(self.resources)
        self.assertIn("aws_s3_bucket.media", self.resources)

    def test_the_bucket_is_private_encrypted_and_expires_its_objects(self) -> None:
        block = self.resources["aws_s3_bucket_public_access_block.media"]
        for setting in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
            self.assertIn(setting, block)
            self.assertNotIn("false", str(block[setting]), setting)

        sse = self.resources["aws_s3_bucket_server_side_encryption_configuration.media"]
        self.assertIn("AES256", str(sse["rule"]))

        rules = self.resources["aws_s3_bucket_lifecycle_configuration.media"]["rule"]
        prefixes = [str(rule.get("filter")) for rule in rules]
        self.assertEqual(len(rules), 2, prefixes)
        self.assertIn("uploads/", str(prefixes))
        self.assertIn("jobs/", str(prefixes))
        for rule in rules:
            self.assertIn("Enabled", str(rule.get("status")))
            self.assertIn("expiration", rule)

    def test_the_upload_page_can_reach_the_bucket(self) -> None:
        cors = self.resources["aws_s3_bucket_cors_configuration.media"]
        self.assertIn("POST", str(cors["cors_rule"]))
        self.assertIn("var.web_origin", str(cors["cors_rule"]))

    def test_the_queue_dead_letters_after_three_attempts(self) -> None:
        self.assertIn("aws_sqs_queue.jobs", self.resources)
        self.assertIn("aws_sqs_queue.dead_letter", self.resources)
        redrive = str(self.resources["aws_sqs_queue.jobs"]["redrive_policy"])
        self.assertIn("aws_sqs_queue.dead_letter.arn", redrive)
        self.assertIn("var.max_receive_count", redrive)

    def test_api_and_worker_roles_exist(self) -> None:
        for role in ("aws_iam_role.api", "aws_iam_role.worker",
                     "aws_iam_role_policy.api", "aws_iam_role_policy.worker"):
            self.assertIn(role, self.resources)

    def _statements(self, name: str) -> list[dict[str, Any]]:
        document = self.data[f"aws_iam_policy_document.{name}"]
        return [statement for statement in document.get("statement", []) if isinstance(statement, dict)]

    def test_the_roles_are_least_privilege(self) -> None:
        for name in ("api", "worker"):
            statements = self._statements(name)
            self.assertTrue(statements, name)
            for statement in statements:
                actions = str(statement.get("actions"))
                resources = str(statement.get("resources"))
                self.assertNotIn('"*"', actions, f"{name}: wildcard action")
                self.assertNotIn('"*"', resources, f"{name}: wildcard resource")
                if "s3:" in actions:
                    self.assertIn("aws_s3_bucket.media", resources + str(statement.get("condition", "")))

        api_actions = " ".join(str(s.get("actions")) for s in self._statements("api"))
        self.assertNotIn("sqs:ReceiveMessage", api_actions, "the API must not consume jobs")
        self.assertNotIn("s3:DeleteObject", api_actions)
        worker_actions = " ".join(str(s.get("actions")) for s in self._statements("worker"))
        self.assertIn("sqs:ChangeMessageVisibility", worker_actions, "the heartbeat needs this")

    def test_no_access_key_and_no_default_ec2_instance(self) -> None:
        for kind in self.resources:
            self.assertNotIn("aws_iam_user", kind)
            self.assertNotIn("aws_iam_access_key", kind)
            self.assertNotIn("aws_instance", kind)
        for name in ("main.tf", "variables.tf"):
            text = (TF_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("AKIA", text)


class TerraformFormattingTests(unittest.TestCase):
    """The checks of `terraform fmt` that do not need the binary."""

    def test_files_use_spaces_and_end_with_a_newline(self) -> None:
        for name in ("main.tf", "variables.tf"):
            text = (TF_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("\t", text, f"{name} uses a tab")
            self.assertTrue(text.endswith("\n"), f"{name} does not end with a newline")
            for number, line in enumerate(text.splitlines(), 1):
                self.assertEqual(line, line.rstrip(), f"{name}:{number} has trailing whitespace")

    def test_arguments_are_aligned_like_terraform_fmt_wants(self) -> None:
        for name in ("main.tf", "variables.tf"):
            text = (TF_DIR / name).read_text(encoding="utf-8").splitlines()
            group: list[tuple[int, int, str]] = []

            def flush() -> None:
                if len(group) > 1:
                    columns = {column for _indent, column, _line in group}
                    self.assertEqual(len(columns), 1, f"{name}: unaligned arguments near {group[0][2]!r}")
                group.clear()

            for line in text:
                stripped = line.strip()
                indent = len(line) - len(line.lstrip())
                key, sep, rest = stripped.partition("=")
                is_argument = (
                    sep == "="
                    and not stripped.startswith(("#", "//", "/*"))
                    and not rest.startswith("=")
                    and key.strip().replace("_", "").isalnum()
                    and not rest.lstrip().startswith(("{", "["))  # a block or a multi-line list opens its own group
                    and (not group or group[0][0] == indent)
                )
                if is_argument:
                    group.append((indent, indent + len(key), line))
                    continue
                flush()
            flush()


if __name__ == "__main__":
    unittest.main()
