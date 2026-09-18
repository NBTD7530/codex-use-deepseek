#!/usr/bin/env python3
"""Transactional installer helpers for the Codex model router."""

import argparse
import errno
import hmac
import json
import os
import plistlib
import pty
import pwd
import re
import select
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


TOP_LEVEL_REPLACED_KEYS = frozenset(
    {
        "model",
        "model_provider",
        "model_catalog_json",
        "preferred_auth_method",
        "forced_login_method",
    }
)
REMOVED_PROVIDER_SECTIONS = frozenset(
    {"[model_providers.deepseek]", "[model_providers.local_router]"}
)
ROUTER_PROVIDER_BLOCK = '''[model_providers.local_router]
name = "OpenAI + DeepSeek"
base_url = "http://127.0.0.1:17890"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
'''
KEYCHAIN_SERVICE = "codex-model-router.deepseek"
LAUNCH_AGENT_LABEL = "com.codex.model-router"
LEGACY_DEEPSEEK_SLUGS = frozenset({"deepseek-v4-flash"})
DEFAULT_MODEL = "gpt-5.6-sol"
TOP_LEVEL_MODEL_ASSIGNMENT = re.compile(r"^\s*model\s*=\s*([\"'])(.*?)\1\s*$")


class InstallError(RuntimeError):
    """Raised when installation cannot complete without risking user config."""


@dataclass(frozen=True)
class InstallLayout:
    home: Path
    project_root: Path

    @property
    def codex_home(self):
        return self.home / ".codex"

    @property
    def config(self):
        return self.codex_home / "config.toml"

    @property
    def model_cache(self):
        return self.codex_home / "models_cache.json"

    @property
    def merged_catalog(self):
        return self.codex_home / "models-router.json"

    @property
    def legacy_catalog(self):
        return self.codex_home / "models.json"

    @property
    def install_dir(self):
        return self.codex_home / "model-router"

    @property
    def installed_router(self):
        return self.install_dir / "codex_model_router.py"

    @property
    def launch_agent(self):
        return self.home / "Library/LaunchAgents" / (LAUNCH_AGENT_LABEL + ".plist")

    @property
    def deepseek_catalog(self):
        return self.project_root / "config" / "deepseek-models.json"

    @property
    def source_router(self):
        return self.project_root / "src" / "codex_model_router.py"


def merge_catalog(cache, deepseek):
    """Merge current official entries with authoritative DeepSeek entries."""
    cache_models = cache.get("models")
    deepseek_models = deepseek.get("models")
    if not isinstance(cache_models, list) or not isinstance(deepseek_models, list):
        raise ValueError("both catalogs must contain a models list")

    deepseek_slugs = set()
    for model in deepseek_models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise ValueError("every DeepSeek model must contain a string slug")
        deepseek_slugs.add(model["slug"])

    official_models = []
    for model in cache_models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise ValueError("every official model must contain a string slug")
        if model["slug"] in deepseek_slugs or model["slug"] in LEGACY_DEEPSEEK_SLUGS:
            continue
        official_models.append(model)

    return {"models": official_models + deepseek_models}


def extract_deepseek_key(text):
    """Return the plaintext key only from the legacy DeepSeek provider block."""
    current_section = None
    assignment = re.compile(
        r"^\s*experimental_bearer_token\s*=\s*([\"'])(.*?)\1\s*$"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            continue
        if current_section != "[model_providers.deepseek]":
            continue
        match = assignment.match(line)
        if match:
            quote, value = match.groups()
            return json.loads('"{0}"'.format(value)) if quote == '"' else value
    return None


def configured_model(text):
    """Return the top-level `model` value, or None when it is not set."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        match = TOP_LEVEL_MODEL_ASSIGNMENT.match(line)
        if match:
            quote, value = match.groups()
            return json.loads('"{0}"'.format(value)) if quote == '"' else value
    return None


def rewrite_codex_config(text, available_models=None):
    """Point Codex at the router while keeping the current default model.

    The default is preserved only when the merged catalog can still serve it;
    otherwise the installer falls back to DEFAULT_MODEL so a stale or removed
    model name cannot break the app after reinstall.
    """
    lines = text.splitlines(keepends=True)
    first_section = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )

    top_level = []
    assignment = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    for line in lines[:first_section]:
        match = assignment.match(line)
        if match and match.group(1) in TOP_LEVEL_REPLACED_KEYS:
            continue
        top_level.append(line)

    current_model = configured_model(text)
    keep_current = (
        isinstance(current_model, str)
        and bool(current_model)
        and (available_models is None or current_model in available_models)
    )
    migrated_top = [
        'model = "{0}"\n'.format(current_model if keep_current else DEFAULT_MODEL),
        'model_provider = "local_router"\n',
        'model_catalog_json = "~/.codex/models-router.json"\n',
    ]
    migrated_top.extend(top_level)

    retained_sections = []
    skipping = False
    for line in lines[first_section:]:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped in REMOVED_PROVIDER_SECTIONS
        if not skipping:
            retained_sections.append(line)

    migrated = "".join(migrated_top + retained_sections).rstrip("\n")
    return migrated + "\n\n" + ROUTER_PROVIDER_BLOCK


def write_security_password_prompt(arguments, secret):
    """Answer security(1)'s two password prompts through a private pseudo-terminal."""
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        os.execv(arguments[0], arguments)

    prompt_index = 0
    buffered = b""
    status = None
    deadline = time.monotonic() + 30
    prompts = (
        b"password data for new item:",
        b"retype password for new item:",
    )
    try:
        while status is None:
            finished_pid, child_status = os.waitpid(child_pid, os.WNOHANG)
            if finished_pid:
                status = child_status
                break
            if time.monotonic() >= deadline:
                os.kill(child_pid, 15)
                _, status = os.waitpid(child_pid, 0)
                break
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    _, status = os.waitpid(child_pid, 0)
                    break
                raise
            buffered = (buffered + chunk)[-8192:]
            if prompt_index < len(prompts) and prompts[prompt_index] in buffered:
                os.write(master_fd, (secret + "\n").encode("utf-8"))
                prompt_index += 1
                buffered = b""
    finally:
        os.close(master_fd)

    return subprocess.CompletedProcess(
        arguments,
        os.waitstatus_to_exitcode(status),
        "",
        "",
    )


def read_keychain_secret(runner=subprocess.run, account=None):
    account_name = account or pwd.getpwuid(os.getuid()).pw_name
    result = runner(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            account_name,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def install_keychain_secret(
    secret,
    password_writer=write_security_password_prompt,
    key_reader=None,
    account=None,
):
    """Store and verify the DeepSeek key without putting it in process arguments."""
    account_name = account or pwd.getpwuid(os.getuid()).pw_name
    arguments = [
        "/usr/bin/security",
        "add-generic-password",
        "-a",
        account_name,
        "-s",
        KEYCHAIN_SERVICE,
        "-U",
        "-w",
    ]
    result = password_writer(arguments, secret)
    if result.returncode != 0:
        raise InstallError("Keychain rejected the DeepSeek API key")
    reader = key_reader or (lambda selected_account: read_keychain_secret(account=selected_account))
    stored = reader(account_name)
    if stored is None or not hmac.compare_digest(stored, secret):
        raise InstallError("Keychain verification failed for the DeepSeek API key")


def keychain_secret_exists(runner=subprocess.run, account=None):
    return read_keychain_secret(runner=runner, account=account) is not None


def render_launch_agent(home, python):
    install_dir = home / ".codex" / "model-router"
    logs_dir = install_dir / "logs"
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(python),
            str(install_dir / "codex_model_router.py"),
            "--host",
            "127.0.0.1",
            "--port",
            "17890",
            "--catalog",
            str(home / ".codex" / "models-router.json"),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "WorkingDirectory": str(install_dir),
        "StandardOutPath": str(logs_dir / "router.log"),
        "StandardErrorPath": str(logs_dir / "router-error.log"),
        "ThrottleInterval": 5,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _write_atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _snapshot(paths):
    snapshots = {}
    for path in paths:
        if path.exists():
            snapshots[path] = (True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        else:
            snapshots[path] = (False, b"", 0o600)
    return snapshots


def _restore(snapshots):
    for path, (existed, content, mode) in snapshots.items():
        if existed:
            _write_atomic(path, content, mode)
        elif path.exists():
            path.unlink()


def _create_backup(layout):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = layout.install_dir / "backups" / timestamp
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(layout.install_dir, 0o700)
    os.chmod(layout.install_dir / "backups", 0o700)
    os.chmod(backup, 0o700)

    copied = []
    absent = []
    tracked = (
        layout.config,
        layout.legacy_catalog,
        layout.merged_catalog,
        layout.launch_agent,
    )
    for source in tracked:
        if source.exists():
            destination = backup / source.name
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            copied.append(str(source))
        else:
            absent.append(str(source))
    _write_atomic(
        backup / "manifest.json",
        json.dumps({"files": copied, "absent": absent}, indent=2).encode("utf-8")
        + b"\n",
    )
    return backup


def migrate(
    layout,
    runner=subprocess.run,
    password_writer=write_security_password_prompt,
):
    """Install files transactionally after securing the plaintext API key."""
    for required in (
        layout.config,
        layout.model_cache,
        layout.deepseek_catalog,
        layout.source_router,
    ):
        if not required.exists():
            raise InstallError("required file is missing: {0}".format(required))

    original_config = layout.config.read_text(encoding="utf-8")
    secret = extract_deepseek_key(original_config)
    if not secret and not keychain_secret_exists(runner=runner):
        raise InstallError("DeepSeek API key is missing from the current provider config")

    backup = _create_backup(layout)
    if secret:
        install_keychain_secret(
            secret,
            password_writer=password_writer,
            key_reader=lambda account: read_keychain_secret(
                runner=runner, account=account
            ),
        )

    cache = json.loads(layout.model_cache.read_text(encoding="utf-8"))
    deepseek = json.loads(layout.deepseek_catalog.read_text(encoding="utf-8"))
    merged = merge_catalog(cache, deepseek)
    available_models = {model["slug"] for model in merged["models"]}
    migrated_config = rewrite_codex_config(original_config, available_models)
    launch_agent = render_launch_agent(layout.home, Path("/usr/bin/python3"))

    touched = (
        layout.config,
        layout.merged_catalog,
        layout.installed_router,
        layout.launch_agent,
    )
    snapshots = _snapshot(touched)
    try:
        layout.install_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(layout.install_dir, 0o700)
        (layout.install_dir / "logs").mkdir(parents=True, exist_ok=True)
        os.chmod(layout.install_dir / "logs", 0o700)
        _write_atomic(layout.config, migrated_config.encode("utf-8"))
        _write_atomic(
            layout.merged_catalog,
            json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        _write_atomic(layout.installed_router, layout.source_router.read_bytes(), 0o700)
        _write_atomic(layout.launch_agent, launch_agent)
    except Exception:
        _restore(snapshots)
        raise
    return backup


def port_is_available(port, host="127.0.0.1"):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((host, port))
    except OSError:
        return False
    finally:
        listener.close()
    return True


def activate_launch_agent(launch_agent, runner=subprocess.run, uid=None):
    user_id = os.getuid() if uid is None else uid
    domain = "gui/{0}".format(user_id)
    service = "{0}/{1}".format(domain, LAUNCH_AGENT_LABEL)
    runner(
        ["/bin/launchctl", "bootout", service],
        text=True,
        capture_output=True,
        check=False,
    )
    bootstrap = runner(
        ["/bin/launchctl", "bootstrap", domain, str(launch_agent)],
        text=True,
        capture_output=True,
        check=False,
    )
    if bootstrap.returncode != 0:
        raise InstallError("launchd could not bootstrap the router")
    kickstart = runner(
        ["/bin/launchctl", "kickstart", "-k", service],
        text=True,
        capture_output=True,
        check=False,
    )
    if kickstart.returncode != 0:
        raise InstallError("launchd could not start the router")


def rollback(layout, backup, runner=subprocess.run, uid=None):
    manifest_path = Path(backup) / "manifest.json"
    if not manifest_path.exists():
        raise InstallError("backup manifest is missing: {0}".format(manifest_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    user_id = os.getuid() if uid is None else uid
    runner(
        [
            "/bin/launchctl",
            "bootout",
            "gui/{0}/{1}".format(user_id, LAUNCH_AGENT_LABEL),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    for destination_text in manifest.get("files", []):
        destination = Path(destination_text)
        source = Path(backup) / destination.name
        if not source.exists():
            raise InstallError("backup file is missing: {0}".format(source))
        _write_atomic(destination, source.read_bytes(), 0o600)
    for destination_text in manifest.get("absent", []):
        destination = Path(destination_text)
        if destination.exists():
            destination.unlink()


def latest_backup(layout):
    backup_root = layout.install_dir / "backups"
    candidates = sorted(
        path for path in backup_root.iterdir() if path.is_dir()
    ) if backup_root.exists() else []
    if not candidates:
        raise InstallError("no router backup is available")
    return candidates[-1]


def uninstall(layout, runner=subprocess.run, uid=None):
    """Stop the router and remove activation files.

    Restores the latest backup when one exists so Codex returns to its
    pre-install configuration. The Keychain item and timestamped backups
    are preserved on purpose; credentials are never deleted implicitly.
    """
    backup_root = layout.install_dir / "backups"
    has_backup = backup_root.exists() and any(
        path.is_dir() for path in backup_root.iterdir()
    )
    if has_backup:
        rollback(layout, latest_backup(layout), runner=runner, uid=uid)
    else:
        user_id = os.getuid() if uid is None else uid
        runner(
            [
                "/bin/launchctl",
                "bootout",
                "gui/{0}/{1}".format(user_id, LAUNCH_AGENT_LABEL),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    for path in (layout.launch_agent, layout.installed_router):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    logs_dir = layout.install_dir / "logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)


def install(
    layout,
    runner=subprocess.run,
    password_writer=write_security_password_prompt,
    port_checker=port_is_available,
):
    if not port_checker(17890):
        raise InstallError("127.0.0.1:17890 is already in use")
    backup = migrate(layout, runner=runner, password_writer=password_writer)
    try:
        activate_launch_agent(layout.launch_agent, runner=runner)
    except Exception:
        rollback(layout, backup, runner=runner)
        raise
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "rollback", "uninstall"))
    parser.add_argument("backup", nargs="?")
    arguments = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    layout = InstallLayout(home=Path.home(), project_root=project_root)
    try:
        if arguments.command == "install":
            backup = install(layout)
            print("Installed Codex model router")
            print("Backup: {0}".format(backup))
        elif arguments.command == "rollback":
            backup = Path(arguments.backup) if arguments.backup else latest_backup(layout)
            rollback(layout, backup)
            print("Rolled back Codex model router")
            print("Backup: {0}".format(backup))
        elif arguments.command == "uninstall":
            uninstall(layout)
            print("Uninstalled Codex model router")
            print("Keychain item and backups were preserved")
    except InstallError as error:
        parser.exit(1, "Installation failed: {0}\n".format(error))


if __name__ == "__main__":
    main()
