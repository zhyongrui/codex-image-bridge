import json
import io
import plistlib
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/codex-image-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))

from bridge_manager import (
    ManagerError,
    command_install,
    command_uninstall,
    command_preflight,
    copy_runtime_file,
    is_bridge_url,
    launchctl_failure,
    plist_payload,
    provider_base_url,
    python_probe,
    replace_provider_base_url,
    top_level_value,
    validate_upstream,
    windows_task_arguments,
    windows_background_runtime,
    windows_task_payload,
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
        with self.assertRaises(ManagerError):
            validate_upstream("https://token@gateway.example/openai")
        with self.assertRaises(ManagerError):
            validate_upstream("https://gateway.example/openai?token=secret")
        self.assertTrue(is_bridge_url("http://localhost:8787/openai/"))


class RuntimeAndServiceTests(unittest.TestCase):
    @staticmethod
    def install_args(home):
        return SimpleNamespace(
            codex_home=str(home),
            upstream="https://gateway.example/openai/",
            provider=None,
            model=None,
            python=sys.executable,
            host="127.0.0.1",
            port=8787,
            mount="/openai",
        )

    def test_current_runtime_probe(self):
        result = python_probe(sys.executable)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(tuple(int(value) for value in result[0].split(".")[:2]), (3, 11))

    def test_manager_prints_unicode_paths_when_initial_encoding_is_legacy(self):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(SCRIPTS)!r}); "
            "from bridge_manager import configure_stdio; "
            "configure_stdio(); print('Codex 图像桥接测试')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("Codex 图像桥接测试", result.stdout.decode("utf-8"))

    def test_runtime_copy_is_safe_when_manager_reinstalls_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bridge_manager.py"
            script.write_text("test")
            copy_runtime_file(script, script)
            self.assertEqual(script.read_text(), "test")

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

    def test_launchctl_failure_routes_codex_to_scoped_elevation(self):
        result = SimpleNamespace(returncode=5, stderr="Operation not permitted", stdout="")
        message = str(launchctl_failure("bootstrap", result))
        self.assertIn("scoped elevated permissions", message)
        self.assertIn("do not use sudo", message)

    def test_windows_task_runs_at_logon_with_restart_and_no_credentials(self):
        payload = windows_task_payload(
            r"C:\Python312\python.exe",
            Path(r"C:\Users\Example User\.codex\image-bridge\codex_image_bridge.py"),
            "https://gateway.example/openai",
            "gpt-main",
            "127.0.0.1",
            8787,
            "/openai",
            Path(r"C:\Users\Example User\.codex\log\image-bridge.log"),
            "S-1-5-21-1000",
        )
        root = ET.fromstring(payload)
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.findtext("t:Triggers/t:LogonTrigger/t:Enabled", namespaces=namespace), "true")
        self.assertEqual(root.findtext("t:Settings/t:RestartOnFailure/t:Interval", namespaces=namespace), "PT1M")
        self.assertEqual(root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=namespace), "999")
        serialized = payload.decode("utf-16")
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("secret", serialized)

    def test_windows_task_arguments_quote_paths_with_spaces(self):
        arguments = windows_task_arguments(
            Path(r"C:\Users\Example User\bridge.py"),
            "https://gateway.example/openai",
            "gpt-main",
            "127.0.0.1",
            8787,
            "/openai",
            Path(r"C:\Users\Example User\bridge.log"),
        )
        self.assertIn('"C:\\Users\\Example User\\bridge.py"', arguments)
        self.assertIn('"C:\\Users\\Example User\\bridge.log"', arguments)

    def test_windows_task_prefers_windowless_python_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "python.exe"
            background = Path(directory) / "pythonw.exe"
            runtime.write_bytes(b"")
            self.assertEqual(windows_background_runtime(str(runtime)), str(runtime))
            background.write_bytes(b"")
            self.assertEqual(windows_background_runtime(str(runtime)), str(background))

    def test_windows_install_updates_config_and_creates_task_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(SAMPLE_CONFIG)
            with (
                mock.patch("bridge_manager.current_platform", return_value="win32"),
                mock.patch("bridge_manager.windows_task_query", return_value=SimpleNamespace(returncode=1)),
                mock.patch("bridge_manager.windows_user_sid", return_value="S-1-5-21-1000"),
                mock.patch("bridge_manager.install_windows_service"),
                mock.patch("bridge_manager.wait_for_health", return_value=(True, "ok")),
            ):
                command_install(self.install_args(home))
            updated = (home / "config.toml").read_text()
            self.assertEqual(
                provider_base_url(updated, "Example Gateway"),
                "http://127.0.0.1:8787/openai/",
            )
            state = json.loads((home / "image-bridge/state.json").read_text())
            self.assertEqual(state["service_kind"], "task-scheduler")
            self.assertTrue((home / "image-bridge/windows-task.xml").exists())

    def test_windows_install_failure_rolls_back_config_and_new_task(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / "config.toml"
            config_path.write_text(SAMPLE_CONFIG)
            with (
                mock.patch("bridge_manager.current_platform", return_value="win32"),
                mock.patch("bridge_manager.windows_task_query", return_value=SimpleNamespace(returncode=1)),
                mock.patch("bridge_manager.windows_user_sid", return_value="S-1-5-21-1000"),
                mock.patch("bridge_manager.install_windows_service", side_effect=ManagerError("denied")),
                mock.patch("bridge_manager.delete_windows_service") as delete_task,
            ):
                with self.assertRaises(ManagerError):
                    command_install(self.install_args(home))
            self.assertEqual(config_path.read_text(), SAMPLE_CONFIG)
            self.assertFalse((home / "image-bridge/state.json").exists())
            self.assertFalse((home / "image-bridge/windows-task.xml").exists())
            delete_task.assert_called_once_with()

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
            self.assertEqual(plan["applicable"], sys.platform in {"darwin", "win32"})
            self.assertFalse(plan["credentials_will_be_persisted"])
            self.assertFalse(plan["network_probe_performed"])
            self.assertEqual(config_path.read_bytes(), before)

    def test_preflight_reports_windows_as_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(SAMPLE_CONFIG)
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
            output = io.StringIO()
            with mock.patch("bridge_manager.current_platform", return_value="win32"), redirect_stdout(output):
                command_preflight(args)
            plan = json.loads(output.getvalue())
            self.assertTrue(plan["applicable"])
            self.assertEqual(plan["platform"], "win32")
            self.assertTrue(plan["planned_changes"][-1].endswith("windows-task.xml"))


if __name__ == "__main__":
    unittest.main()
