#!/usr/bin/env python3
"""Install, diagnose, and uninstall Codex Image Bridge."""

import argparse
import ast
import csv
import datetime
import glob
import json
import os
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


LABEL = "com.codex.image-bridge"
WINDOWS_TASK_NAME = "Codex Image Bridge"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DEFAULT_PORT = 8787
DEFAULT_MOUNT = "/openai"
MIN_PYTHON = (3, 11)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ManagerError(RuntimeError):
    pass


def current_platform() -> str:
    return sys.platform


def codex_home(value: Optional[str]) -> Path:
    return Path(value).expanduser().resolve() if value else Path.home() / ".codex"


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_toml_scalar(raw: str) -> str:
    value = raw.strip()
    quote = value[:1]
    if quote not in {'"', "'"}:
        return value.split("#", 1)[0].strip()
    escaped = False
    for index in range(1, len(value)):
        char = value[index]
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            return str(ast.literal_eval(value[: index + 1]))
        escaped = False
    raise ManagerError("unterminated quoted TOML value")


def top_level_value(text: str, key: str) -> Optional[str]:
    assignment = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.+)$")
    in_section = False
    for line in text.splitlines():
        if re.match(r"^\s*\[", line):
            in_section = True
        if not in_section:
            match = assignment.match(line)
            if match:
                return parse_toml_scalar(match.group(1))
    return None


def provider_section(line: str) -> Optional[str]:
    match = re.match(r"^\s*\[model_providers\.(.+)\]\s*(?:#.*)?$", line)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        return parse_toml_scalar(raw)
    except (ManagerError, ValueError, SyntaxError):
        return raw


def provider_base_url(text: str, provider: str) -> Optional[str]:
    active = False
    for line in text.splitlines():
        section = provider_section(line)
        if section is not None:
            active = section == provider
            continue
        if active and re.match(r"^\s*\[", line):
            active = False
        if active:
            match = re.match(r"^\s*base_url\s*=\s*(.+)$", line)
            if match:
                return parse_toml_scalar(match.group(1))
    return None


def replace_provider_base_url(text: str, provider: str, new_url: str) -> str:
    lines = text.splitlines(keepends=True)
    active = False
    replaced = False
    value_pattern = r'(?:"(?:\\.|[^"\\])*"|\'[^\']*\')'
    assignment = re.compile(r"^(\s*base_url\s*=\s*)" + value_pattern + r"(\s*(?:#.*)?)(\r?\n?)$")
    for index, line in enumerate(lines):
        section = provider_section(line.rstrip("\r\n"))
        if section is not None:
            active = section == provider
            continue
        if active and re.match(r"^\s*\[", line):
            active = False
        if active:
            match = assignment.match(line)
            if match:
                lines[index] = match.group(1) + json.dumps(new_url) + match.group(2) + match.group(3)
                replaced = True
                break
    if not replaced:
        raise ManagerError("could not find base_url in [model_providers.%s]" % provider)
    return "".join(lines)


def is_bridge_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


def validate_upstream(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ManagerError("upstream must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManagerError("upstream URL cannot contain credentials, a query, or a fragment")
    if (parsed.hostname or "") in LOOPBACK_HOSTS:
        raise ManagerError("upstream cannot point back to the local bridge")
    return normalized


def python_probe(path: str) -> Optional[Tuple[str, str]]:
    code = "import json,ssl,sys; print(json.dumps([list(sys.version_info[:3]),ssl.OPENSSL_VERSION]))"
    try:
        result = subprocess.run([path, "-c", code], text=True, capture_output=True, timeout=10)
        if result.returncode:
            return None
        version, ssl_version = json.loads(result.stdout.strip())
        if tuple(version[:2]) < MIN_PYTHON:
            return None
        return ".".join(str(part) for part in version), str(ssl_version)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def runtime_candidates(explicit: Optional[str]) -> Sequence[str]:
    candidates: List[str] = []
    if explicit:
        candidates.append(str(Path(explicit).expanduser()))
    environment = os.environ.get("CODEX_IMAGE_BRIDGE_PYTHON")
    if environment:
        candidates.append(str(Path(environment).expanduser()))
    candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(
        sorted(
            glob.glob(
                str(Path.home() / ".cache/codex-runtimes/*/dependencies/python/bin/python3")
            ),
            reverse=True,
        )
    )
    candidates.extend(
        sorted(
            glob.glob(
                str(Path.home() / ".cache/codex-runtimes/*/dependencies/python/python.exe")
            ),
            reverse=True,
        )
    )
    candidates.extend(
        sorted(
            glob.glob(
                str(Path.home() / ".cache/codex-runtimes/*/dependencies/python/Scripts/python.exe")
            ),
            reverse=True,
        )
    )
    candidates.extend(["/opt/homebrew/bin/python3", "/usr/local/bin/python3"])
    return list(dict.fromkeys(candidates))


def find_runtime(explicit: Optional[str]) -> Tuple[str, str, str]:
    for candidate in runtime_candidates(explicit):
        probe = python_probe(candidate)
        if probe:
            return str(Path(candidate).resolve()), probe[0], probe[1]
    raise ManagerError(
        "Python 3.11 or newer with SSL support was not found; install Python 3.12 "
        "or pass --python /absolute/path/to/python3"
    )


def legacy_upstream(plist_path: Path) -> Optional[str]:
    try:
        with plist_path.open("rb") as handle:
            arguments = plistlib.load(handle).get("ProgramArguments", [])
        index = arguments.index("--upstream")
        return str(arguments[index + 1])
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
        return None


def load_state(path: Path) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    temporary.write_bytes(content)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def copy_runtime_file(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        return
    shutil.copy2(source, destination)


def run_command(arguments: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(arguments), text=True, capture_output=True, check=check)


def launch_domain() -> str:
    return "gui/%d" % os.getuid()


def stop_service() -> None:
    run_command(["launchctl", "bootout", launch_domain() + "/" + LABEL], check=False)


def windows_task_command() -> str:
    return shutil.which("schtasks.exe") or shutil.which("schtasks") or "schtasks.exe"


def windows_user_sid() -> str:
    result = run_command(["whoami.exe", "/user", "/fo", "csv", "/nh"], check=False)
    if result.returncode:
        raise ManagerError("could not determine the current Windows user SID: " + result.stderr.strip())
    try:
        row = next(csv.reader([result.stdout.strip()]))
        sid = row[-1].strip()
    except (csv.Error, IndexError, StopIteration):
        raise ManagerError("could not parse the current Windows user SID")
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid, flags=re.IGNORECASE):
        raise ManagerError("unexpected Windows user SID returned by whoami")
    return sid


def windows_task_arguments(
    script: Path, upstream: str, model: str, host: str, port: int, mount: str, log_path: Path
) -> str:
    return subprocess.list2cmdline(
        [
            str(script),
            "--upstream",
            upstream,
            "--model",
            model,
            "--host",
            host,
            "--port",
            str(port),
            "--mount",
            mount,
            "--log-file",
            str(log_path),
        ]
    )


def windows_task_payload(
    runtime: str,
    script: Path,
    upstream: str,
    model: str,
    host: str,
    port: int,
    mount: str,
    log_path: Path,
    user_sid: str,
) -> bytes:
    ET.register_namespace("", TASK_XML_NAMESPACE)
    root = ET.Element("{%s}Task" % TASK_XML_NAMESPACE, {"version": "1.4"})
    registration = ET.SubElement(root, "{%s}RegistrationInfo" % TASK_XML_NAMESPACE)
    ET.SubElement(registration, "{%s}Description" % TASK_XML_NAMESPACE).text = (
        "Runs the per-user Codex Image Bridge on loopback."
    )
    triggers = ET.SubElement(root, "{%s}Triggers" % TASK_XML_NAMESPACE)
    trigger = ET.SubElement(triggers, "{%s}LogonTrigger" % TASK_XML_NAMESPACE)
    ET.SubElement(trigger, "{%s}Enabled" % TASK_XML_NAMESPACE).text = "true"
    ET.SubElement(trigger, "{%s}UserId" % TASK_XML_NAMESPACE).text = user_sid
    principals = ET.SubElement(root, "{%s}Principals" % TASK_XML_NAMESPACE)
    principal = ET.SubElement(principals, "{%s}Principal" % TASK_XML_NAMESPACE, {"id": "Author"})
    ET.SubElement(principal, "{%s}UserId" % TASK_XML_NAMESPACE).text = user_sid
    ET.SubElement(principal, "{%s}LogonType" % TASK_XML_NAMESPACE).text = "InteractiveToken"
    ET.SubElement(principal, "{%s}RunLevel" % TASK_XML_NAMESPACE).text = "LeastPrivilege"
    settings = ET.SubElement(root, "{%s}Settings" % TASK_XML_NAMESPACE)
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, "{%s}%s" % (TASK_XML_NAMESPACE, key)).text = value
    restart = ET.SubElement(settings, "{%s}RestartOnFailure" % TASK_XML_NAMESPACE)
    ET.SubElement(restart, "{%s}Interval" % TASK_XML_NAMESPACE).text = "PT1M"
    ET.SubElement(restart, "{%s}Count" % TASK_XML_NAMESPACE).text = "999"
    actions = ET.SubElement(root, "{%s}Actions" % TASK_XML_NAMESPACE, {"Context": "Author"})
    execute = ET.SubElement(actions, "{%s}Exec" % TASK_XML_NAMESPACE)
    ET.SubElement(execute, "{%s}Command" % TASK_XML_NAMESPACE).text = runtime
    ET.SubElement(execute, "{%s}Arguments" % TASK_XML_NAMESPACE).text = windows_task_arguments(
        script, upstream, model, host, port, mount, log_path
    )
    ET.SubElement(execute, "{%s}WorkingDirectory" % TASK_XML_NAMESPACE).text = str(script.parent)
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def windows_task_query(xml: bool = False) -> subprocess.CompletedProcess:
    arguments = [windows_task_command(), "/Query", "/TN", WINDOWS_TASK_NAME]
    if xml:
        arguments.append("/XML")
    return run_command(arguments, check=False)


def stop_windows_service() -> None:
    run_command([windows_task_command(), "/End", "/TN", WINDOWS_TASK_NAME], check=False)


def delete_windows_service() -> None:
    stop_windows_service()
    run_command([windows_task_command(), "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"], check=False)


def install_windows_service(task_path: Path) -> None:
    stop_windows_service()
    result = run_command(
        [windows_task_command(), "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(task_path), "/F"],
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "exit status %d" % result.returncode
        raise ManagerError(
            "Windows Task Scheduler registration failed: %s. If Codex sandboxing denied "
            "this operation, rerun the same installer with scoped elevated permissions; "
            "do not use an Administrator shell unless Codex cannot request approval." % detail
        )
    result = run_command([windows_task_command(), "/Run", "/TN", WINDOWS_TASK_NAME], check=False)
    if result.returncode:
        raise ManagerError(
            "Windows Task Scheduler could not start the bridge: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def launchctl_failure(operation: str, result: subprocess.CompletedProcess) -> ManagerError:
    detail = result.stderr.strip() or result.stdout.strip() or "exit status %d" % result.returncode
    return ManagerError(
        "launchctl %s failed: %s. If Codex sandboxing denied this operation, rerun the "
        "same installer with scoped elevated permissions to register the current user's "
        "LaunchAgent; do not use sudo." % (operation, detail)
    )


def start_service(plist_path: Path) -> None:
    stop_service()
    result = run_command(["launchctl", "bootstrap", launch_domain(), str(plist_path)], check=False)
    if result.returncode:
        raise launchctl_failure("bootstrap", result)
    result = run_command(["launchctl", "kickstart", "-k", launch_domain() + "/" + LABEL], check=False)
    if result.returncode:
        raise launchctl_failure("kickstart", result)


def plist_payload(
    runtime: str, script: Path, upstream: str, model: str, host: str, port: int, mount: str, log_dir: Path
) -> bytes:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            runtime,
            str(script),
            "--upstream",
            upstream,
            "--model",
            model,
            "--host",
            host,
            "--port",
            str(port),
            "--mount",
            mount,
        ],
        "WorkingDirectory": str(script.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / "image-bridge.log"),
        "StandardErrorPath": str(log_dir / "image-bridge.error.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def validate_config(runtime: str, path: Path) -> None:
    code = "import pathlib,tomllib,sys; tomllib.loads(pathlib.Path(sys.argv[1]).read_text())"
    result = run_command([runtime, "-c", code, str(path)], check=False)
    if result.returncode:
        raise ManagerError("updated Codex config failed TOML validation: " + result.stderr.strip())


def command_install(args: argparse.Namespace) -> None:
    platform_name = current_platform()
    if platform_name not in {"darwin", "win32"}:
        raise ManagerError("automatic service installation currently supports macOS and Windows")
    home = codex_home(args.codex_home)
    config_path = home / "config.toml"
    if not config_path.exists():
        raise ManagerError("Codex config not found: %s" % config_path)
    config_text = config_path.read_text()
    provider = args.provider or top_level_value(config_text, "model_provider")
    if not provider:
        raise ManagerError("model_provider is not set in Codex config")
    current_url = provider_base_url(config_text, provider)
    if not current_url:
        raise ManagerError("active provider %s has no base_url" % provider)
    model = args.model or top_level_value(config_text, "model")
    if not model:
        raise ManagerError("model is not set; pass --model explicitly")

    install_dir = home / "image-bridge"
    state_path = install_dir / "state.json"
    state = load_state(state_path)
    service_path = (
        Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if platform_name == "darwin"
        else install_dir / "windows-task.xml"
    )
    previous_service = service_path.read_bytes() if service_path.exists() else None
    if platform_name == "win32":
        task_exists = windows_task_query().returncode == 0
        if task_exists and not state and previous_service is None:
            raise ManagerError(
                "a Windows Scheduled Task named %r already exists but is not managed by this "
                "installation; remove or rename it before installing" % WINDOWS_TASK_NAME
            )
    previous_state = state_path.read_bytes() if state_path.exists() else None
    legacy = legacy_upstream(service_path) if platform_name == "darwin" else None
    inferred = args.upstream or state.get("original_base_url") or legacy
    if not inferred and not is_bridge_url(current_url):
        inferred = current_url
    if not isinstance(inferred, str):
        raise ManagerError("current base_url already points to a bridge; pass the original URL with --upstream")
    upstream = validate_upstream(inferred)
    runtime, runtime_version, ssl_version = find_runtime(args.python)
    mount = "/" + args.mount.strip("/") if args.mount.strip("/") else ""
    local_host = "[%s]" % args.host if ":" in args.host else args.host
    bridge_url = "http://%s:%d%s/" % (local_host, args.port, mount)

    source_script = Path(__file__).with_name("codex_image_bridge.py")
    if not source_script.exists():
        raise ManagerError("codex_image_bridge.py must be next to bridge_manager.py")
    install_dir.mkdir(parents=True, exist_ok=True)
    log_dir = home / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    installed_script = install_dir / "codex_image_bridge.py"
    installed_manager = install_dir / "bridge_manager.py"
    copy_runtime_file(source_script, installed_script)
    copy_runtime_file(Path(__file__), installed_manager)
    os.chmod(installed_script, 0o755)
    os.chmod(installed_manager, 0o755)

    original_base_url = state.get("original_base_url") if is_bridge_url(current_url) else current_url
    if not isinstance(original_base_url, str):
        original_base_url = upstream
    backup_path: Optional[Path] = None
    config_changed = current_url != bridge_url
    if config_changed:
        backup_path = home / ("config.toml.image-bridge-backup-" + timestamp())
        shutil.copy2(config_path, backup_path)
        os.chmod(backup_path, 0o600)
        updated = replace_provider_base_url(config_text, provider, bridge_url)
        atomic_write(config_path, updated.encode("utf-8"), config_path.stat().st_mode & 0o777)
        try:
            validate_config(runtime, config_path)
        except Exception:
            atomic_write(config_path, config_text.encode("utf-8"), config_path.stat().st_mode & 0o777)
            raise

    if platform_name == "darwin":
        service_payload = plist_payload(
            runtime, installed_script, upstream, model, args.host, args.port, mount, log_dir
        )
    else:
        service_payload = windows_task_payload(
            runtime,
            installed_script,
            upstream,
            model,
            args.host,
            args.port,
            mount,
            log_dir / "image-bridge.log",
            windows_user_sid(),
        )
    atomic_write(service_path, service_payload, 0o644)
    state_payload = {
        "schema": 2,
        "provider": provider,
        "original_base_url": original_base_url,
        "bridge_base_url": bridge_url,
        "upstream": upstream,
        "model": model,
        "runtime": runtime,
        "runtime_version": runtime_version,
        "ssl": ssl_version,
        "service_definition": str(service_path),
        "service_kind": "launchd" if platform_name == "darwin" else "task-scheduler",
        "backup": str(backup_path) if backup_path else state.get("backup"),
        "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_write(state_path, json.dumps(state_payload, indent=2).encode("utf-8") + b"\n")
    try:
        if platform_name == "darwin":
            start_service(service_path)
        else:
            install_windows_service(service_path)
        parsed_bridge = urllib.parse.urlsplit(bridge_url)
        health_url = "%s://%s/__codex_image_bridge__/health" % (
            parsed_bridge.scheme,
            parsed_bridge.netloc,
        )
        healthy, health_detail = wait_for_health(health_url, timeout_seconds=15)
        if not healthy:
            raise ManagerError("bridge did not become healthy after service start: " + health_detail)
    except Exception:
        if config_changed:
            atomic_write(config_path, config_text.encode("utf-8"), config_path.stat().st_mode & 0o777)
        if previous_state is not None:
            atomic_write(state_path, previous_state)
        elif state_path.exists():
            state_path.unlink()
        if platform_name == "win32":
            delete_windows_service()
        if previous_service is not None:
            atomic_write(service_path, previous_service, 0o644)
        elif service_path.exists():
            service_path.unlink()
        try:
            if platform_name == "darwin" and previous_service is not None:
                start_service(service_path)
            elif platform_name == "win32" and previous_service is not None:
                install_windows_service(service_path)
        except Exception:
            pass
        raise
    print("Installed Codex Image Bridge")
    print("  provider: %s" % provider)
    print("  upstream: %s" % upstream)
    print("  local URL: %s" % bridge_url)
    print("  runtime: Python %s (%s)" % (runtime_version, runtime))
    print("Run: %s doctor" % installed_manager)


def command_preflight(args: argparse.Namespace) -> None:
    platform_name = current_platform()
    home = codex_home(args.codex_home)
    config_path = home / "config.toml"
    if not config_path.exists():
        raise ManagerError("Codex config not found: %s" % config_path)
    config_text = config_path.read_text()
    provider = args.provider or top_level_value(config_text, "model_provider")
    if not provider:
        raise ManagerError("model_provider is not set in Codex config")
    current_url = provider_base_url(config_text, provider)
    if not current_url:
        raise ManagerError("active provider %s has no base_url" % provider)
    model = args.model or top_level_value(config_text, "model")
    install_dir = home / "image-bridge"
    state_path = install_dir / "state.json"
    state = load_state(state_path)
    service_path = (
        Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if platform_name == "darwin"
        else install_dir / "windows-task.xml"
    )
    legacy = legacy_upstream(service_path) if platform_name == "darwin" else None
    inferred = args.upstream or state.get("original_base_url") or legacy
    if not inferred and not is_bridge_url(current_url):
        inferred = current_url
    upstream = validate_upstream(inferred) if isinstance(inferred, str) else None
    runtime_error: Optional[str] = None
    runtime: Optional[str] = None
    runtime_version: Optional[str] = None
    ssl_version: Optional[str] = None
    try:
        runtime, runtime_version, ssl_version = find_runtime(args.python)
    except ManagerError as error:
        runtime_error = str(error)
    mount = "/" + args.mount.strip("/") if args.mount.strip("/") else ""
    local_host = "[%s]" % args.host if ":" in args.host else args.host
    bridge_url = "http://%s:%d%s/" % (local_host, args.port, mount)
    plan = {
        "applicable": platform_name in {"darwin", "win32"} and upstream is not None and runtime is not None,
        "platform": platform_name,
        "active_provider": provider,
        "model": model,
        "current_base_url": current_url,
        "upstream": upstream,
        "already_installed": bool(state) and current_url == bridge_url,
        "bridge_base_url": bridge_url,
        "runtime": {
            "found": runtime is not None,
            "path": runtime,
            "version": runtime_version,
            "ssl": ssl_version,
            "error": runtime_error,
        },
        "planned_changes": [
            str(config_path),
            str(install_dir),
            str(service_path),
        ],
        "credentials_will_be_persisted": False,
        "network_probe_performed": False,
        "note": (
            "Preflight is read-only and does not prove that the upstream supports the "
            "Responses image_generation tool."
        ),
    }
    print(json.dumps(plan, indent=2))


def health_check(url: str) -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
        return response.status == 200, "version=%s Python=%s" % (
            payload.get("version", "unknown"), payload.get("python", "unknown")
        )
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
        return False, str(error)


def wait_for_health(url: str, timeout_seconds: float) -> Tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    detail = "health check timed out"
    while time.monotonic() < deadline:
        healthy, detail = health_check(url)
        if healthy:
            return True, detail
        time.sleep(0.2)
    return False, detail


def tls_check(url: str) -> Tuple[bool, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return True, "not required for HTTP upstream"
    port = parsed.port or 443
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=8) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname) as secured:
                certificate = secured.getpeercert()
                subject = dict(item[0] for item in certificate.get("subject", []))
                return True, "%s; certificate CN=%s" % (secured.version(), subject.get("commonName", "unknown"))
    except (OSError, ssl.SSLError) as error:
        return False, str(error)


def command_doctor(args: argparse.Namespace) -> None:
    platform_name = current_platform()
    home = codex_home(args.codex_home)
    state_path = home / "image-bridge/state.json"
    state = load_state(state_path)
    checks: List[Tuple[str, bool, str]] = []
    checks.append(("state", bool(state), str(state_path)))
    runtime = str(state.get("runtime", ""))
    runtime_result = python_probe(runtime) if runtime else None
    checks.append(("runtime", runtime_result is not None, ("Python " + runtime_result[0]) if runtime_result else runtime or "missing"))
    config_path = home / "config.toml"
    try:
        text = config_path.read_text()
        provider = str(state.get("provider") or top_level_value(text, "model_provider") or "")
        current = provider_base_url(text, provider) or "missing"
        expected = str(state.get("bridge_base_url", ""))
        checks.append(("Codex config", bool(expected) and current == expected, "base_url=" + current))
    except OSError as error:
        checks.append(("Codex config", False, str(error)))
    bridge_url = str(state.get("bridge_base_url", "http://127.0.0.1:8787/openai/"))
    parsed = urllib.parse.urlsplit(bridge_url)
    health_url = "%s://%s/__codex_image_bridge__/health" % (parsed.scheme, parsed.netloc)
    healthy, health_detail = wait_for_health(health_url, timeout_seconds=3)
    checks.append(("bridge health", healthy, health_detail))
    upstream = str(state.get("upstream", ""))
    tls_ok, tls_detail = tls_check(upstream) if upstream else (False, "upstream missing from state")
    checks.append(("upstream TLS", tls_ok, tls_detail))
    if platform_name == "darwin":
        service = run_command(["launchctl", "print", launch_domain() + "/" + LABEL], check=False)
        checks.append(("LaunchAgent", service.returncode == 0, "loaded" if service.returncode == 0 else "not loaded"))
    elif platform_name == "win32":
        service = windows_task_query()
        checks.append(
            (
                "Scheduled Task",
                service.returncode == 0,
                "registered" if service.returncode == 0 else "not registered",
            )
        )
    failed = False
    for name, okay, detail in checks:
        print("[OK]   %s: %s" % (name, detail) if okay else "[FAIL] %s: %s" % (name, detail))
        failed = failed or not okay
    if failed:
        raise ManagerError("one or more diagnostic checks failed")


def command_uninstall(args: argparse.Namespace) -> None:
    platform_name = current_platform()
    home = codex_home(args.codex_home)
    install_dir = home / "image-bridge"
    state_path = install_dir / "state.json"
    state = load_state(state_path)
    if not state:
        raise ManagerError("installation state not found: %s" % state_path)
    config_path = home / "config.toml"
    text = config_path.read_text()
    provider = str(state.get("provider", ""))
    current = provider_base_url(text, provider)
    expected = str(state.get("bridge_base_url", ""))
    original = str(state.get("original_base_url", ""))
    if current == expected and original:
        backup = home / ("config.toml.image-bridge-uninstall-backup-" + timestamp())
        shutil.copy2(config_path, backup)
        os.chmod(backup, 0o600)
        updated = replace_provider_base_url(text, provider, original)
        runtime = str(state.get("runtime", ""))
        atomic_write(config_path, updated.encode("utf-8"), config_path.stat().st_mode & 0o777)
        try:
            if python_probe(runtime):
                validate_config(runtime, config_path)
        except Exception:
            atomic_write(config_path, text.encode("utf-8"), config_path.stat().st_mode & 0o777)
            raise
        print("Restored %s base_url to %s" % (provider, original))
    else:
        print("Config was not changed: current base_url no longer matches the installed bridge URL")
    if platform_name == "darwin":
        stop_service()
    elif platform_name == "win32":
        delete_windows_service()
    default_service = (
        Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if platform_name == "darwin"
        else install_dir / "windows-task.xml"
    )
    service_path = Path(str(state.get("service_definition") or state.get("plist") or default_service))
    if service_path.exists():
        service_path.unlink()
    archived = install_dir / ("state.uninstalled-" + timestamp() + ".json")
    os.replace(state_path, archived)
    print("Uninstalled service; recovery state retained at %s" % archived)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", help="Codex home directory (default: ~/.codex)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install", help="install or upgrade the bridge")
    install.add_argument("--upstream", help="original provider base URL; inferred on first install")
    install.add_argument("--provider", help="provider name; defaults to active model_provider")
    install.add_argument("--model", help="Responses model; defaults to the active model")
    install.add_argument("--python", help="Python 3.11+ executable for the service")
    install.add_argument("--host", default="127.0.0.1", choices=sorted(LOOPBACK_HOSTS))
    install.add_argument("--port", type=int, default=DEFAULT_PORT)
    install.add_argument("--mount", default=DEFAULT_MOUNT)
    install.set_defaults(handler=command_install)
    preflight = subparsers.add_parser("preflight", help="print a read-only JSON installation plan")
    preflight.add_argument("--upstream", help="original provider base URL; inferred when possible")
    preflight.add_argument("--provider", help="provider name; defaults to active model_provider")
    preflight.add_argument("--model", help="Responses model; defaults to the active model")
    preflight.add_argument("--python", help="Python 3.11+ executable for the service")
    preflight.add_argument("--host", default="127.0.0.1", choices=sorted(LOOPBACK_HOSTS))
    preflight.add_argument("--port", type=int, default=DEFAULT_PORT)
    preflight.add_argument("--mount", default=DEFAULT_MOUNT)
    preflight.set_defaults(handler=command_preflight)
    doctor = subparsers.add_parser("doctor", help="check runtime, config, service, and upstream TLS")
    doctor.set_defaults(handler=command_doctor)
    uninstall = subparsers.add_parser("uninstall", help="stop the service and safely restore base_url")
    uninstall.set_defaults(handler=command_uninstall)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except ManagerError as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
