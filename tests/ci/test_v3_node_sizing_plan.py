import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("sizing", Path(__file__).resolve().parents[2] / "scripts/check-v3-node-sizing-plan.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(step):
    topology, target, action = MODULE.STEPS[step]
    groups = {}
    for key in topology:
        legacy = key in {MODULE.A, MODULE.C}
        groups[key] = {"availability_zone": MODULE.A if key.startswith(MODULE.A) else MODULE.C,
                       "instance_type": "m6i.large" if legacy else "t3a.medium",
                       "launch_template_version": "1" if legacy else None}
    variables = {"expected_account_id": "483175530259", "aws_region": "ap-northeast-2",
                 "cluster_name": MODULE.CLUSTER, "system_node_groups": groups}
    changes = []
    if target:
        attributes = {"cluster_name": MODULE.CLUSTER, "node_group_name": "system-" + target,
                      "instance_types": ["t3a.medium" if action == "create" else "m6i.large"],
                      "scaling_config": [{"desired_size": 1, "min_size": 1, "max_size": 1}]}
        changes.append({"address": MODULE.PREFIX + target + '\"]', "mode": "managed",
                        "change": {"actions": [action], "before": attributes if action == "delete" else None,
                                   "after": attributes if action == "create" else None}})
    if step == "add-a":
        changes.append({"address": "aws_launch_template.system", "mode": "managed", "change": {
            "actions": ["update"], "before": {"credit_specification": [], "metadata_options": [{"http_tokens": "required"}]},
            "after": {"credit_specification": [{"cpu_credits": "unlimited"}], "metadata_options": [{"http_tokens": "required"}]}}})
    return {"variables": {key: {"value": value} for key, value in variables.items()}, "resource_changes": changes}


def receipts(step):
    candidate = "system-" + (MODULE.NEW_A if step == "remove-a" else MODULE.NEW_C)
    return ({"cluster": MODULE.CLUSTER, "account": "483175530259", "storageNodeGroup": candidate,
             "selectedNodeGroups": [candidate, "other"], "crossNodeHttp": True, "serviceDns": True,
             "pvcRemount": True, "encryptedGp3": True, "syntheticVolumeId": "vol-synthetic"},
            {"cleanupComplete": True, "ebsDeletedConfirmed": True, "syntheticVolumeId": "vol-synthetic"})


class PlanScopeTests(unittest.TestCase):
    def test_all_four_steps_and_final(self):
        for step in MODULE.STEPS:
            with self.subTest(step=step):
                kwargs = dict(zip(["verification", "cleanup"], receipts(step))) if step.startswith("remove") else {}
                self.assertTrue(MODULE.inspect_plan(fixture(step), step, **kwargs)["scopePassed"])

    def test_no_delete_before_candidate_and_cleanup_verification(self):
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(fixture("remove-a"), "remove-a")
        verification, cleanup = receipts("remove-c")
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(fixture("remove-a"), "remove-a", verification, cleanup)
        verification, cleanup = receipts("remove-a")
        cleanup["ebsDeletedConfirmed"] = False
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(fixture("remove-a"), "remove-a", verification, cleanup)
        verification, cleanup = receipts("remove-a")
        verification.pop("syntheticVolumeId")
        cleanup.pop("syntheticVolumeId")
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(fixture("remove-a"), "remove-a", verification, cleanup)

    def test_vpc_change_parallel_replacement_and_node_update_rejected(self):
        for address, actions in [("aws_vpc.this", ["update"]),
                                 (MODULE.PREFIX + MODULE.A + '\"]', ["delete", "create"]),
                                 (MODULE.PREFIX + MODULE.C + '\"]', ["update"])]:
            plan = fixture("add-a")
            plan["resource_changes"].append({"address": address, "mode": "managed", "change": {"actions": actions}})
            with self.subTest(address=address), self.assertRaises(ValueError):
                MODULE.inspect_plan(plan, "add-a")

    def test_template_update_cannot_disable_imds_security(self):
        plan = fixture("add-a")
        plan["resource_changes"][1]["change"]["after"]["metadata_options"] = [{"http_tokens": "optional"}]
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(plan, "add-a")

    def test_wrong_environment_unpinned_legacy_and_wrong_size_rejected(self):
        variants = []
        plan = fixture("add-a"); plan["variables"]["expected_account_id"]["value"] = "111111111111"; variants.append(plan)
        plan = fixture("add-a"); plan["variables"]["system_node_groups"]["value"][MODULE.A]["launch_template_version"] = None; variants.append(plan)
        plan = fixture("add-a"); plan["resource_changes"][0]["change"]["after"]["instance_types"] = ["t3.nano"]; variants.append(plan)
        for plan in variants:
            with self.assertRaises(ValueError):
                MODULE.inspect_plan(plan, "add-a")

    def test_final_rejects_even_a_single_mutation(self):
        plan = fixture("final")
        plan["resource_changes"] = copy.deepcopy(fixture("add-c")["resource_changes"])
        with self.assertRaises(ValueError):
            MODULE.inspect_plan(plan, "final")


if __name__ == "__main__":
    unittest.main()
