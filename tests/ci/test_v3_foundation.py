import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("foundation", ROOT / "scripts/check-v3-foundation.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TargetBoundaryTests(unittest.TestCase):
    account = "483175530259"
    cluster = "yeodam-v3-foundation"
    context = "arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation"
    endpoint = "https://foundation.eks.example.test"

    def test_exact_target(self):
        MODULE.validate_target(self.account, self.cluster, self.context, self.endpoint,
                               {"arn": self.context, "endpoint": self.endpoint})

    def test_v2_context_cannot_pass_even_when_endpoint_matches(self):
        with self.assertRaises(ValueError):
            MODULE.validate_target(self.account, "yeodam-v2-staging", self.context,
                                   self.endpoint, {"arn": self.context, "endpoint": self.endpoint})

    def test_context_alias_and_foreign_accounts_rejected(self):
        for context in ["default", "production", self.context.replace(self.account, "111111111111")]:
            with self.subTest(context=context), self.assertRaises(ValueError):
                MODULE.validate_target(self.account, self.cluster, context, self.endpoint,
                                       {"arn": self.context, "endpoint": self.endpoint})

    def test_context_name_does_not_hide_wrong_kube_server(self):
        with self.assertRaises(ValueError):
            MODULE.validate_target(self.account, self.cluster, self.context, "https://other.eks.example.test",
                                   {"arn": self.context, "endpoint": self.endpoint})

    def test_aws_cluster_must_match_too(self):
        with self.assertRaises(ValueError):
            MODULE.validate_target(self.account, self.cluster, self.context, self.endpoint,
                                   {"arn": self.context + "-other", "endpoint": self.endpoint})

    def test_render_needs_no_aws_or_kubeconfig(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check-v3-foundation.py"), "render"],
                                capture_output=True, text=True, env={"PATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        fixture = json.loads(result.stdout)
        self.assertEqual(fixture["kind"], "List")
        # Synthetic-only PVCs must be reclaimable; no production volume references.
        self.assertEqual([item["reclaimPolicy"] for item in fixture["items"]
                          if item["kind"] == "StorageClass"], ["Delete"])
        self.assertNotIn("volumeName", result.stdout)

    def test_mutations_require_explicit_kubeconfig(self):
        for mode in ["check", "cleanup"]:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/check-v3-foundation.py"), mode],
                                    capture_output=True, text=True, env={"PATH": ""})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("isolated V3 file", result.stderr)

    def test_failed_probe_is_diagnosable_without_logging_aws_error_bodies(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="synthetic HTTP probe failed")
        with patch.object(MODULE.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "synthetic HTTP probe failed"):
                MODULE.command(["kubectl", "exec", "synthetic", "--", "wget", "http://fixture"])
            with self.assertRaises(RuntimeError) as failure:
                MODULE.command(["aws", "eks", "describe-cluster"])
            self.assertNotIn("synthetic HTTP probe failed", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
