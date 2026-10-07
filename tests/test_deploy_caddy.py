"""Isolated deployment checks: no real Caddy, Docker, /etc or /root is used."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("scenario", [
    "success", "custom_config", "config_prefix", "watch", "resume",
    "api_mismatch", "occupied", "validate_failure", "reload_failure",
    "existing_env", "late_api_change", "late_import_change",
])
def test_deployment_preserves_existing_services(tmp_path, scenario):
    project = tmp_path / "project"
    for folder in [project / "scripts", tmp_path / "caddy", tmp_path / "root",
                   tmp_path / "bin", tmp_path / "proc/2471"]:
        folder.mkdir(parents=True)
    config_path = tmp_path / "caddy/Caddyfile"
    original = "course.yolodev.top {\n reverse_proxy 127.0.0.1:3017\n}\n"
    config_path.write_text(original)
    source_path = Path(__file__).parents[1] / "scripts/deploy-caddy.sh"
    script = source_path.read_text().replace(
        "[[ $EUID -eq 0 ]] || fail '请以 root 在服务器执行。'",
        ": # Root guard omitted only in the isolated mock copy.",
    ).replace("/etc/caddy", str(tmp_path / "caddy")).replace(
        "/root/", str(tmp_path / "root") + "/",
    ).replace("/proc/", str(tmp_path / "proc") + "/")
    (project / "scripts/deploy-caddy.sh").write_text(script)
    (project / "compose.yaml").write_text("services: {}\n")
    args = ["/usr/bin/caddy", "run", "--config", str(config_path)]
    if scenario == "custom_config":
        args[-1] = "/other/Caddyfile"
    elif scenario == "config_prefix":
        args[-1] += "-old"
    elif scenario == "watch":
        args.append("-w")
    elif scenario == "resume":
        args.append("--resume")
    (tmp_path / "proc/2471/cmdline").write_bytes("\0".join(args).encode() + b"\0")
    disk = {"apps": {"http": {"servers": {"srv0": {"routes": [
        {"match": [{"host": ["course.yolodev.top"]}]},
    ]}}}}}
    (tmp_path / "disk.json").write_text(json.dumps(disk))
    (tmp_path / "live.json").write_text(json.dumps({} if scenario == "api_mismatch" else disk))
    existing_env = "READER_PASSWORD=preserve-this-password\nCOMPOSE_PROJECT_NAME=other_project\n"
    if scenario == "existing_env":
        (project / ".env").write_text(existing_env)
    mocks = {
        "systemctl": """#!/bin/bash
if [[ "$*" == *MainPID* ]]; then echo 2471; fi
""",
        "getent": '#!/bin/bash\necho "50.114.172.72 STREAM"\n',
        "ss": """#!/bin/bash
if [[ "$READER_TEST_SCENARIO" == occupied ]]; then echo LISTEN; fi
""",
        "docker": """#!/bin/bash
printf '%s\n' "$*" >> "$READER_TEST_BASE/docker.log"
if [[ "$*" == *" port "* ]]; then exit 1; fi
""",
        "curl": """#!/bin/bash
if [[ "$*" == *"/config/"* ]]; then
    if [[ -e "$READER_TEST_BASE/api-read" && "$READER_TEST_SCENARIO" == late_api_change ]]; then
        echo '{}'
    else
        cat "$READER_TEST_BASE/live.json"
    fi
    touch "$READER_TEST_BASE/api-read"
else
    echo '{"status":"ok"}'
fi
""",
        "caddy": """#!/bin/bash
printf '%s\n' "$*" >> "$READER_TEST_BASE/caddy.log"
case "$1" in
adapt)
    if [[ -e "$READER_TEST_BASE/adapted" && "$READER_TEST_SCENARIO" == late_import_change ]]; then
        echo '{}'
    else
        cat "$READER_TEST_BASE/disk.json"
    fi
    touch "$READER_TEST_BASE/adapted";;
validate) [[ "$READER_TEST_SCENARIO" != validate_failure ]];;
reload) [[ "$READER_TEST_SCENARIO" != reload_failure ]];;
esac
""",
        # GNU cmp accepts --silent; translate for this test on macOS too.
        "cmp": '#!/bin/bash\nif [[ "$1" == --silent ]]; then shift; fi\n/usr/bin/cmp -s "$@"\n',
    }
    for name, body in mocks.items():
        mock = tmp_path / "bin" / name
        mock.write_text(body)
        mock.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=str(tmp_path / "bin") + ":" + env["PATH"],
               READER_TEST_BASE=str(tmp_path), READER_TEST_SCENARIO=scenario)
    result = subprocess.run(
        ["bash", str(project / "scripts/deploy-caddy.sh"), "md.yolodev.top", "18080"],
        env=env, text=True, capture_output=True,
    )
    assert (result.returncode == 0) == (scenario == "success"), result.stderr + result.stdout
    if scenario == "success":
        assert config_path.read_text().startswith(original)
        assert config_path.read_text().count("md.yolodev.top {") == 1
        assert "reverse_proxy 127.0.0.1:18080" in config_path.read_text()
        assert "COMPOSE_PROJECT_NAME=modu_reader" in (project / ".env").read_text()
        assert (project / ".env").stat().st_mode & 0o777 == 0o600
        assert "-p modu_reader up -d --build reader" in (tmp_path / "docker.log").read_text()
    else:
        assert config_path.read_text() == original
    if scenario in {"custom_config", "config_prefix", "watch", "resume", "api_mismatch"}:
        assert not (project / ".env").exists()
        assert (tmp_path / "docker.log").read_text() == "compose version\n"
    if scenario == "existing_env":
        assert (project / ".env").read_text() == existing_env
    if scenario != "success":
        caddy_log = tmp_path / "caddy.log"
        if caddy_log.exists():
            assert ("reload --config" in caddy_log.read_text()) == (scenario == "reload_failure")
