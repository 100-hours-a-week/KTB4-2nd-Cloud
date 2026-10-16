import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "monitoring/v2-staging" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
sampler = load("gpu_sampler", "sample-ai-gpu.py")
discovery = load("gpu_discovery", "discover-gpu-state.py")

class GPUObservabilityTest(unittest.TestCase):
    def test_gpu_units_and_unsupported_sample(self):
        lines = sampler.nvidia_metrics("25, 1024, 24576")
        self.assertIn('yeodam_gpu_memory_used_bytes{gpu="0"} 1073741824.0', lines)
        for value in ["N/A, N/A, 24576", "", "101, 1, 2", "0, 1, inf"]:
            with self.assertRaises(ValueError): sampler.nvidia_metrics(value)

    def test_failed_probe_does_not_invent_zero_usage(self):
        with patch.object(sampler, "run", side_effect=OSError), patch.object(sampler.urllib.request, "urlopen", side_effect=OSError):
            text = sampler.collect("10.30.10.162")
        self.assertIn("yeodam_gpu_smi_success 0", text)
        self.assertNotIn("yeodam_gpu_utilization_percent", text)
        self.assertNotIn("yeodam_ai_container_running", text)

    def test_stopped_instance_retains_target_and_state(self):
        result = discovery.target({"InstanceId":"i-00000000000000000", "PrivateIpAddress":"10.30.10.162", "State":{"Name":"stopped"}})
        self.assertEqual(result[0]["labels"]["ec2_state"], "stopped")
        self.assertEqual(result[0]["targets"], ["10.30.10.162:12345"])

    def test_discovery_failure_preserves_last_good_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu.json"
            path.write_text("last-good")
            with patch.object(discovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("aws",25)):
                with self.assertRaises(subprocess.TimeoutExpired): discovery.refresh("i-00000000000000000",path)
            self.assertEqual(path.read_text(),"last-good")

    def test_public_address_rejected(self):
        with self.assertRaises(ValueError):
            discovery.target({"InstanceId":"i-00000000000000000","PrivateIpAddress":"1.1.1.1","State":{"Name":"running"}})

if __name__ == "__main__": unittest.main()
