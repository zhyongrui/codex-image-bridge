import json
import io
import plistlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/codex-image-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))

from bridge_manager import (
    ManagerError,
    command_uninstall,
    command_preflight,
    is_bridge_url,
    plist_payload,
    provider_base_url,
    python_probe,
    replace_provider_base_url,
    top_level_value,
    validate_upstream,
)


SAMPLE_CONFIG = '''model = "gpt-main"
model_provider = "Example Gateway"

[model_providers."Example Gateway"]
name = "Example"
base_url = "https://gateway.example/openai/" # keep this comment
experimental_bearer_token = "secret-test-value"

[features]
image_generation = true
'''


class ConfigTests(unittest.TestCase):
    def test_reads_active_provider_and_model(self):
        self.assertEqual(top_level_value(SAMPLE_CONFIG, "model"), "gpt-main")
        self.assertEqual(top_level_value(SAMPLE_CONFIG, "model_provider"), "Example Gateway")
        self.assertEqual(
            provider_base_url(SAMPLE_CONFIG, "Example Gateway"),
            "https://gateway.example/openai/",
        )

    def test_replaces_only_provider_base_url_and_preserves_secret(self):
        updated = replace_provider_base_url(
            SAMPLE_CONFIG, "Example Gateway", "http://127.0.0.1:8787/openai/"
        )
        self.assertIn(
            'base_url = "http://127.0.0.1:8787/openai/" # keep this comment', updated
        )
        self.assertIn('experimental_bearer_token = "secret-test-value"', updated)
        self.assertEqual(updated.count("base_url ="), 1)

    def test_rejects_recursive_or_invalid_upstream(self):
        with self.assertRaises(ManagerError):
            validate_upstream("http://127.0.0.1:8787/openai")
        with self.assertRaises(ManagerError):
            validate_upstream("not-a-url")
        self.assertTrue(is_bridge_url("http://localhost:8787/openai/"))


class RuntimeAndServiceTests(unittest.TestCase):
    def test_current_runtime_probe(self):
        result = python_probe(sys.executable)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(tuple(int(value) for value in result[0].split(".")[:2]), (3, 11))

    def test_plist_contains_no_credentials(self):
        payload = plist_payload(
            "/opt/python3",
            Path("/tmp/codex-image-bridge.py"),
            "https://gateway.example/openai",
            "gpt-main",
            "127.0.0.1",
            8787,
            "/openai",
            Path("/tmp/log"),
        )
        parsed = plistlib.loads(payload)
        serialized = json.dumps(parsed)
        self.assertIn("--upstream", parsed["ProgramArguments"])
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("secret", serialized)

    def test_uninstall_restores_only_recorded_provider_url(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            install_dir = home / "image-bridge"
            install_dir.mkdir()
            config = SAMPLE_CONFIG.replace(
                "https://gateway.example/openai/", "http://127.0.0.1:8787/openai/"
            )
            (home / "config.toml").write_text(config)
            plist = home / "service.plist"
            plist.write_text("test")
            state = {
                "provider": "Example Gateway",
                "original_base_url": "https://gateway.example/openai/",
                "bridge_base_url": "http://127.0.0.1:8787/openai/",
                "runtime": sys.executable,
                "plist": str(plist),
            }
            (install_dir / "state.json").write_text(json.dumps(state))
            with mock.patch("bridge_manager.stop_service"):
                command_uninstall(SimpleNamespace(codex_home=str(home)))
            restored = (home / "config.toml").read_text()
            self.assertEqual(
                provider_base_url(restored, "Example Gateway"),
                "https://gateway.example/openai/",
            )
            self.assertFalse(plist.exists())
            self.assertTrue(list(install_dir.glob("state.uninstalled-*.json")))

    def test_preflight_is_json_and_does_not_modify_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / "config.toml"
            config_path.write_text(SAMPLE_CONFIG)
            before = config_path.read_bytes()
            output = io.StringIO()
            args = SimpleNamespace(
                codex_home=str(home),
                upstream="https://gateway.example/openai/",
                provider=None,
                model=None,
                python=sys.executable,
                host="127.0.0.1",
                port=8787,
                mount="/openai",
            )
            with redirect_stdout(output):
                command_preflight(args)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["applicable"], sys.platform == "darwin")
            self.assertFalse(plan["credentials_will_be_persisted"])
            self.assertFalse(plan["network_probe_performed"])
            self.assertEqual(config_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
