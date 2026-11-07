#!/usr/bin/env python3
"""Interactive setup for llm-otel-lgtm.

    python3 setup_wizard.py              # ask questions, configure, start, verify
    python3 setup_wizard.py --uninstall  # remove what this wizard added

It can:
  * check Docker / Colima, Claude Code and Codex
  * write deploy/local/.env
  * add OpenTelemetry settings to ~/.claude/settings.json and ~/.codex/config.toml
    (each file is backed up first; other settings are left untouched)
  * start the stack and send a test event end to end

Non-interactive (CI, fleet rollout):
    python3 setup_wizard.py --yes --sources claude,codex --prompt-logging off
    python3 setup_wizard.py --yes --collector http://otel.example.com:4317 --sources claude

Only the Python standard library is used (Python 3.9+).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent
COMPOSE_DIR = REPO / "deploy" / "local"
ENV_FILE = COMPOSE_DIR / ".env"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
GRAFANA = "http://localhost:3000"
LOCAL_GRPC = "http://localhost:4317"
LOCAL_HTTP = "http://localhost:4318"

CLAUDE_KEYS = [  # everything the wizard may add to ~/.claude/settings.json "env"
    "CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_LOG_USER_PROMPTS", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_METRICS_INCLUDE_SESSION_ID",
]
CODEX_BEGIN = "# >>> llm-otel-lgtm >>>"
CODEX_END = "# <<< llm-otel-lgtm <<<"

# ---------- terminal helpers ----------

_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def head(s: str) -> None:
    print("\n" + _c("1;36", s))


def ok(s: str) -> None:
    print(_c("32", "  ✓ ") + s)


def warn(s: str) -> None:
    print(_c("33", "  ! ") + s)


def fail(s: str) -> None:
    print(_c("31", "  ✗ ") + s)


def info(s: str) -> None:
    print("    " + s)


class Asker:
    """Questions with defaults; --yes answers every question with its default."""

    def __init__(self, assume_yes: bool):
        self.assume_yes = assume_yes

    def _input(self, prompt: str) -> str:
        if self.assume_yes:
            return ""
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    def yes_no(self, q: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            a = self._input(f"  {q} [{hint}] ").lower()
            if not a:
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            print("    Please answer y or n.")

    def choice(self, q: str, options: list[tuple[str, str]], default: str) -> str:
        print(f"  {q}")
        for i, (key, label) in enumerate(options, 1):
            mark = " (default)" if key == default else ""
            print(f"    {i}) {label}{mark}")
        while True:
            a = self._input("  > ")
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return options[int(a) - 1][0]
            for key, _ in options:
                if a.lower() == key:
                    return key
            print(f"    Enter 1-{len(options)}.")

    def text(self, q: str, default: str = "", validate=None) -> str:
        while True:
            shown = f" [{default}]" if default else ""
            a = self._input(f"  {q}{shown}: ") or default
            err = validate(a) if validate else None
            if not err:
                return a
            print(f"    {err}")
            if self.assume_yes:
                raise SystemExit(f"Invalid value for '{q}': {err}")


# ---------- checks ----------

def run(cmd: list[str], timeout: int = 60, cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def docker_status() -> str:
    """'ok', 'no-cli', 'no-compose' or 'not-running'."""
    if not shutil.which("docker"):
        return "no-cli"
    if run(["docker", "compose", "version"])[0] != 0:
        return "no-compose"
    if run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)[0] != 0:
        return "not-running"
    return "ok"


def tool_version(name: str) -> str | None:
    if not shutil.which(name):
        return None
    code, out = run([name, "--version"], timeout=20)
    return out.splitlines()[0] if code == 0 and out else "installed"


def port_open(url: str, timeout: float = 3.0) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout):
            return True
    except OSError:
        return False


def http_get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# ---------- config writers ----------

def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = f"{path.name}.bak-{datetime.now():%Y%m%d%H%M%S}"
    dst, n = path.with_name(stamp), 1
    while dst.exists():  # never overwrite an earlier backup (e.g. two runs in one second)
        dst, n = path.with_name(f"{stamp}-{n}"), n + 1
    shutil.copy2(path, dst)
    return dst


def write_env(prompt_logging: str, master_key: str | None, dry: bool) -> None:
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ENV_EXAMPLE.read_text()

    def setval(t: str, key: str, val: str) -> str:
        if re.search(rf"^{key}=.*$", t, re.M):
            return re.sub(rf"^{key}=.*$", f"{key}={val}", t, flags=re.M)
        return t.rstrip("\n") + f"\n{key}={val}\n"

    text = setval(text, "PROMPT_LOGGING", prompt_logging)
    if master_key:
        text = setval(text, "LITELLM_MASTER_KEY", master_key)
    if dry:
        info(f"would write {ENV_FILE}")
        return
    ENV_FILE.write_text(text)
    os.chmod(ENV_FILE, 0o600)  # may hold API keys
    ok(f"wrote {ENV_FILE.relative_to(REPO)} (PROMPT_LOGGING={prompt_logging})")


def claude_env(endpoint: str, prompts: bool, team: str, fleet: bool) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    }
    if prompts:
        env["OTEL_LOG_USER_PROMPTS"] = "1"
    if team:
        env["OTEL_RESOURCE_ATTRIBUTES"] = team
    if fleet:
        env["OTEL_METRICS_INCLUDE_SESSION_ID"] = "false"
    return env


def configure_claude(home: Path, env: dict[str, str], dry: bool) -> None:
    path = home / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError as e:
        fail(f"{path} is not valid JSON ({e}). Fix it, then run the wizard again.")
        return
    if not isinstance(data.get("env", {}), dict):
        fail(f'"env" in {path} is not an object; skipping Claude Code.')
        return
    current = data.setdefault("env", {})
    # Drop keys from an earlier run that this run no longer wants (e.g. prompts turned off).
    for k in CLAUDE_KEYS:
        if k not in env:
            current.pop(k, None)
    current.update(env)
    if dry:
        info(f"would update {path}:")
        print(json.dumps({"env": env}, indent=2))
        return
    b = backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    ok(f"updated {path}" + (f"  (backup: {b.name})" if b else ""))


def _strip_codex_otel(text: str) -> str:
    """Remove our marked block and any other [otel] / [otel.*] tables."""
    text = re.sub(rf"\n?{re.escape(CODEX_BEGIN)}.*?{re.escape(CODEX_END)}\n?", "\n", text, flags=re.S)
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if m:
            skipping = m.group(1) == "otel" or m.group(1).startswith("otel.")
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _has_top_level_otel_keys(text: str) -> bool:
    """otel.* dotted keys before the first table header."""
    for line in text.splitlines():
        if re.match(r"\s*\[", line):
            return False
        if re.match(r"\s*otel\.", line):
            return True
    return False


def configure_codex(home: Path, endpoint: str, prompts: bool, env_name: str, dry: bool) -> None:
    path = home / ".codex" / "config.toml"
    text = path.read_text() if path.exists() else ""
    if _has_top_level_otel_keys(text):
        fail(f"{path} sets otel.* as dotted keys. Remove them, then run the wizard again.")
        return
    block = (
        f"{CODEX_BEGIN}\n"
        "# OpenTelemetry -> llm-otel-lgtm (managed by setup_wizard.py)\n"
        "[otel]\n"
        f'environment = "{env_name}"\n'
        f"log_user_prompt = {'true' if prompts else 'false'}\n"
        f'exporter = {{ otlp-grpc = {{ endpoint = "{endpoint}" }} }}\n'
        f'trace_exporter = {{ otlp-grpc = {{ endpoint = "{endpoint}" }} }}\n'
        f'metrics_exporter = {{ otlp-grpc = {{ endpoint = "{endpoint}" }} }}\n'
        f"{CODEX_END}\n"
    )
    new = _strip_codex_otel(text).rstrip("\n")
    new = (new + "\n\n" if new else "") + block
    if dry:
        info(f"would update {path} with:")
        print(block)
        return
    b = backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    ok(f"updated {path}" + (f"  (backup: {b.name})" if b else ""))


def uninstall(home: Path, ask: Asker, dry: bool) -> None:
    head("Removing llm-otel-lgtm telemetry settings")
    cpath = home / ".claude" / "settings.json"
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text())
            env = data.get("env", {})
            removed = [k for k in CLAUDE_KEYS if k in env]
            if removed and not dry:
                b = backup(cpath)
                for k in removed:
                    env.pop(k)
                if not env:
                    data.pop("env")
                cpath.write_text(json.dumps(data, indent=2) + "\n")
                ok(f"removed {len(removed)} telemetry settings from {cpath}  (backup: {b.name})")
            elif removed:
                info(f"would remove from {cpath}: {', '.join(removed)}")
            else:
                ok(f"nothing to remove in {cpath}")
        except json.JSONDecodeError:
            fail(f"{cpath} is not valid JSON; left unchanged.")
    xpath = home / ".codex" / "config.toml"
    if xpath.exists():
        text = xpath.read_text()
        new = _strip_codex_otel(text)
        if new != text and not dry:
            b = backup(xpath)
            xpath.write_text(new.rstrip("\n") + "\n")
            ok(f"removed [otel] from {xpath}  (backup: {b.name})")
        elif new != text:
            info(f"would remove [otel] from {xpath}")
        else:
            ok(f"nothing to remove in {xpath}")
    if docker_status() == "ok" and ask.yes_no("Stop the local stack too? (data volumes are kept)", False):
        if not dry:
            code, out = run(["docker", "compose", "--profile", "gateway", "down"], timeout=180, cwd=COMPOSE_DIR)
            (ok if code == 0 else fail)("stack stopped" if code == 0 else out[-400:])
    print("\nRestart Claude Code and Codex for the change to take effect.")


# ---------- stack ----------

def start_stack(gateway: bool, dry: bool) -> bool:
    cmd = ["docker", "compose"] + (["--profile", "gateway"] if gateway else []) + ["up", "-d"]
    if dry:
        info("would run: " + " ".join(cmd) + f"  (in {COMPOSE_DIR.relative_to(REPO)})")
        return True
    info("starting containers (first run downloads ~1.5 GB of images)…")
    code, out = run(cmd, timeout=1200, cwd=COMPOSE_DIR)
    if code != 0:
        fail("docker compose up failed:\n" + out[-1500:])
        return False
    for _ in range(90):
        try:
            if http_get_json(f"{GRAFANA}/api/health", 3).get("database") == "ok":
                ok(f"stack is up: Grafana {GRAFANA}")
                return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    fail("Grafana did not become healthy. Check: docker compose logs grafana")
    return False


def send_test_event(http_endpoint: str) -> str | None:
    """Send one OTLP/HTTP log record; return its id, or None on failure."""
    check_id = secrets.token_hex(6)
    body = {"resourceLogs": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "llm-otel-setup-check"}}]},
        "scopeLogs": [{"scope": {"name": "setup_wizard"}, "logRecords": [{
            "timeUnixNano": str(time.time_ns()), "severityText": "INFO",
            "body": {"stringValue": f"setup check {check_id}"},
            "attributes": [{"key": "event.name", "value": {"stringValue": "setup_check"}}],
        }]}],
    }]}
    req = urllib.request.Request(http_endpoint.rstrip("/") + "/v1/logs", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return check_id if r.status < 300 else None
    except (OSError, urllib.error.URLError) as e:
        fail(f"could not send test event to {http_endpoint}: {e}")
        return None


def verify_local(check_id: str) -> bool:
    q = urllib.parse.urlencode({"query": f'{{service_name="llm-otel-setup-check"}} |= "{check_id}"',
                                "limit": "1", "start": str(time.time_ns() - 600 * 10**9)})
    url = f"{GRAFANA}/api/datasources/proxy/uid/loki/loki/api/v1/query_range?{q}"
    for _ in range(30):
        try:
            if http_get_json(url, 5)["data"]["result"]:
                return True
        except (OSError, KeyError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    return False


# ---------- main flow ----------

def validate_url(u: str) -> str | None:
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "Use a full URL, e.g. http://otel.example.com:4317"
    return None


def validate_attrs(s: str) -> str | None:
    if not s:
        return None
    if re.search(r'[\s";\\]', s) or not all(re.fullmatch(r"[^=]+=[^=]+", p) for p in s.split(",")):
        return "Use key=value pairs separated by commas, no spaces, e.g. team.id=platform,cost_center=eng-1"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up llm-otel-lgtm and connect Claude Code / Codex.")
    ap.add_argument("--yes", "-y", action="store_true", help="accept defaults, no questions")
    ap.add_argument("--sources", help="comma list: claude,codex,python,gateway (default: detected tools)")
    ap.add_argument("--collector", help="use an existing collector (OTLP gRPC URL) instead of the local stack")
    ap.add_argument("--prompt-logging", choices=["off", "on"], help="store prompt text (default off)")
    ap.add_argument("--team", default=None, help="labels for Claude Code, e.g. team.id=platform,cost_center=eng-1")
    ap.add_argument("--no-start", action="store_true", help="configure only; don't start Docker")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove the settings this wizard added")
    ap.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)  # for tests
    args = ap.parse_args()
    ask = Asker(args.yes)

    print(_c("1", "llm-otel-lgtm setup") + "  ·  monitor Claude Code, Codex and your LLM apps")
    if args.uninstall:
        uninstall(args.home, ask, args.dry_run)
        return 0

    # 1. Detect
    head("1/5  Checking this machine")
    claude_v, codex_v = tool_version("claude"), tool_version("codex")
    (ok if claude_v else warn)(f"Claude Code: {claude_v or 'not found'}")
    (ok if codex_v else warn)(f"Codex CLI:   {codex_v or 'not found'}")
    dstat = docker_status()
    msg = {"ok": "Docker: running", "no-cli": "Docker: not installed", "no-compose": "Docker Compose: not installed",
           "not-running": "Docker: installed but not running"}[dstat]
    (ok if dstat == "ok" else warn)(msg)

    # 2. Questions
    head("2/5  A few questions")
    if args.collector:
        target = "remote"
    else:
        target = ask.choice("Where should telemetry go?", [
            ("local", "Run the stack here (Docker): Grafana on http://localhost:3000"),
            ("remote", "An existing collector (company / shared stack)"),
        ], "local")
    grpc = LOCAL_GRPC
    if target == "remote":
        grpc = args.collector or ask.text("Collector OTLP gRPC URL", "", validate_url)
        if validate_url(grpc):
            raise SystemExit(validate_url(grpc))

    detected = [s for s, v in (("claude", claude_v), ("codex", codex_v)) if v]
    if args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        bad = set(sources) - {"claude", "codex", "python", "gateway"}
        if bad:
            raise SystemExit(f"Unknown source(s): {', '.join(sorted(bad))}")
    else:
        sources = []
        if ask.yes_no("Send Claude Code telemetry?" + ("" if claude_v else " (not installed yet)"), bool(claude_v)):
            sources.append("claude")
        if ask.yes_no("Send Codex CLI telemetry?" + ("" if codex_v else " (not installed yet)"), bool(codex_v)):
            sources.append("codex")
        if ask.yes_no("Will you instrument your own Python app (OpenAI, Claude, Gemini, Ollama …)?", False):
            sources.append("python")
        if target == "local" and ask.yes_no("Start the LiteLLM gateway (apps in any language, no code changes)?", False):
            sources.append("gateway")
    if not sources and not args.sources:
        warn("No sources picked. The stack will still be set up; connect sources later (docs/add-your-llm.md).")

    prompt_logging = args.prompt_logging or ("on" if ask.yes_no(
        "Store prompt and response text? (No = metadata only: tokens, cost, latency)", False) else "off")
    team = args.team if args.team is not None else (
        ask.text("Optional team labels for Claude Code (e.g. team.id=platform)", "", validate_attrs)
        if "claude" in sources else "")
    if validate_attrs(team or ""):
        raise SystemExit(validate_attrs(team))
    fleet = target == "remote"  # shared collectors: one series per user, not per session
    master_key = None
    if "gateway" in sources:
        master_key = "sk-" + secrets.token_urlsafe(18)

    # 3. Summary + confirm
    head("3/5  Plan")
    info(f"Telemetry goes to: {grpc}" + ("  (local stack)" if target == "local" else ""))
    info(f"Sources:           {', '.join(sources) or 'none yet'}")
    info(f"Prompt text:       {'stored' if prompt_logging == 'on' else 'not stored'}")
    if team:
        info(f"Team labels:       {team}")
    if "claude" in sources:
        info(f"Will edit:         {args.home / '.claude' / 'settings.json'}  (backed up first)")
    if "codex" in sources:
        info(f"Will edit:         {args.home / '.codex' / 'config.toml'}  (backed up first)")
    if target == "local":
        info(f"Will write:        {ENV_FILE.relative_to(REPO)}" + ("" if args.no_start else ", then start Docker"))
    if not args.dry_run and not ask.yes_no("Go ahead?", True):
        print("Nothing changed.")
        return 1

    # 4. Apply
    head("4/5  Configuring")
    if target == "local":
        write_env(prompt_logging, master_key, args.dry_run)
    if "claude" in sources:
        configure_claude(args.home, claude_env(grpc, prompt_logging == "on", team or "", fleet), args.dry_run)
    if "codex" in sources:
        configure_codex(args.home, grpc, prompt_logging == "on", "local" if target == "local" else "shared",
                        args.dry_run)

    # 5. Start + verify
    head("5/5  Starting and verifying")
    healthy = False
    if target == "local" and not args.no_start:
        if dstat != "ok":
            fail(msg + ". Start the stack after fixing it:")
            if dstat in ("no-cli", "no-compose") and platform.system() == "Darwin":
                info("brew install colima docker docker-compose && colima start")
                info('then add "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"] to ~/.docker/config.json')
            elif dstat == "not-running":
                info("colima start   (or open Docker Desktop)")
            info(f"cd {COMPOSE_DIR.relative_to(REPO)} && docker compose up -d")
        else:
            healthy = start_stack("gateway" in sources, args.dry_run)
    if args.dry_run:
        info("dry run: nothing was changed")
    elif target == "local" and healthy:
        check = send_test_event(LOCAL_HTTP)
        if check and verify_local(check):
            ok("test event went collector → Loki → Grafana")
        elif check:
            fail("test event was sent but did not show up in Loki. Check: docker compose logs otel-collector")
    elif target == "remote":
        (ok if port_open(grpc) else fail)(f"collector {'reachable' if port_open(grpc) else 'NOT reachable'} at {grpc}")

    # Next steps
    head("Done. Next steps")
    if target == "local":
        info(f"Open {GRAFANA}  →  dashboards: LLM Observability, AI Coding Agents")
    if "claude" in sources:
        info("Claude Code: restart it (settings are read at startup). Data appears within ~10 s.")
    if "codex" in sources:
        info("Codex: start a new session.")
    if "python" in sources:
        info('Python app: pip install "git+https://github.com/itzvadi/llm-otel-lgtm#subdirectory=instrumentation/python"')
        info(f"            export OTEL_EXPORTER_OTLP_ENDPOINT={LOCAL_HTTP if target == 'local' else '<collector OTLP HTTP URL>'}")
    if master_key:
        info(f"Gateway: http://localhost:4000  api_key={master_key}  (saved in {ENV_FILE.relative_to(REPO)})")
    info("Full guide: docs/add-your-llm.md   ·   Undo: python3 setup_wizard.py --uninstall")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Nothing more was changed.")
        sys.exit(130)
