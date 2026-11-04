#!/usr/bin/env python3
"""Task 6 predeployment runtime fixture: fresh-isolated signup precedence.

Proves, against the EXACT OpenWebUI image digest pinned in
``module-hsworkshop/install/openwebui.yaml``, the plan's startup-config
precedence claim for ``ENABLE_PERSISTENT_CONFIG=false``:

  A. fresh bootstrap, empty disposable data, ``ENABLE_SIGNUP=true``
     -> first start reports ``enable_signup=false`` (native bootstrap side
        effect, no auth hooks involved).
  B. second start on the SAME data, existing users, ``ENABLE_SIGNUP=true``
     -> ``enable_signup=true``; with persistence off the declared env value
        governs once bootstrap is skipped. This is the core pending proof.
  C. third start on the SAME data, existing users, ``ENABLE_SIGNUP=false``
     -> ``enable_signup=false``; the production manifest value is preserved
        for existing data (negative control).

Isolation and safety (deliberate, see docs/headless-openwebui.md Task 6):

  * image reference is parsed from the manifest and asserted to be the exact
    repo digest; no tag, no :latest, no build;
  * no published ports: HTTP is probed with ``podman exec`` on the container's
    own loopback, so nothing is exposed on the host or the network;
  * disposable data directory under the approved TMPDIR only; no PVC, Secret,
    cluster or production DB is mounted, and ``secrets.env`` is never read;
  * fixture admin credentials are generated in-process with ``secrets`` and
    passed to the container through environment variables (``-e NAME`` with the
    value inherited from the client process), never via argv, stdout or logs;
  * only non-secret observations are printed (booleans, role, version, digest);
  * the fixture container and its data are torn down in a ``finally`` block.

Usage::

    python3 module-hsworkshop/tests/runtime_openwebui_signup_precedence.py

Exit status 0 only when all three scenarios match. Requires a local podman
(no installs, no dependency upgrades). If the pinned image is absent the
script asks podman to pull exactly that digest.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO_ROOT, "module-hsworkshop", "install", "openwebui.yaml")
PODMAN = os.environ.get("PODMAN", "podman")

READY_TIMEOUT_SECONDS = 420
POLL_SECONDS = 3
CONTAINER_NAME = f"owui-signup-fixture-{os.getpid()}"

# Runs inside the container; loopback only. Emits public, non-secret config.
PROBE = r"""
import json, sys, urllib.request
out = {}
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5) as r:
        out["health"] = json.loads(r.read().decode() or "{}")
except Exception as exc:  # noqa: BLE001 - probe must degrade to a status
    print(json.dumps({"ready": False, "error": type(exc).__name__}))
    sys.exit(0)
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/api/config", timeout=5) as r:
        cfg = json.loads(r.read().decode())
    feats = cfg.get("features") or {}
    out["ready"] = True
    out["version"] = cfg.get("version")
    out["enable_signup"] = feats.get("enable_signup")
    out["enable_signin"] = feats.get("enable_signin")
except Exception as exc:  # noqa: BLE001
    out["ready"] = True
    out["config_error"] = type(exc).__name__
print(json.dumps(out))
"""

# Runs inside the container; exercises the public signup route with a
# disposable synthetic account. Emits only the status and assigned role.
SIGNUP_PROBE = r"""
import json, os, urllib.error, urllib.request
body = json.dumps({
    "name": "fixture signup",
    "email": os.environ["WEBUI_FIXTURE_SIGNUP_EMAIL"],
    "password": os.environ["WEBUI_FIXTURE_SIGNUP_PASSWORD"],
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/api/v1/auths/signup",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode())
    print(json.dumps({"signup": True, "status": r.status, "role": payload.get("role")}))
except urllib.error.HTTPError as exc:
    print(json.dumps({"signup": False, "status": exc.code}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"signup": False, "error": type(exc).__name__}))
"""

# Runs inside the container; proves the bootstrapped admin row survives a
# restart. Credentials arrive via environment. The JWT is never printed.
SIGNIN_PROBE = r"""
import json, os, urllib.error, urllib.request
body = json.dumps({
    "email": os.environ["WEBUI_ADMIN_EMAIL"],
    "password": os.environ["WEBUI_ADMIN_PASSWORD"],
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/api/v1/auths/signin",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode())
    print(json.dumps({
        "signin": True,
        "role": payload.get("role"),
        "token_type": payload.get("token_type"),
    }))
except urllib.error.HTTPError as exc:
    print(json.dumps({"signin": False, "status": exc.code}))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"signin": False, "error": type(exc).__name__}))
"""


class FixtureError(RuntimeError):
    pass


def pinned_image() -> str:
    with open(MANIFEST, "r", encoding="utf-8") as handle:
        text = handle.read()
    refs = set(
        re.findall(
            r"image:\s*(ghcr\.io/open-webui/open-webui@sha256:[0-9a-f]{64})", text
        )
    )
    if len(refs) != 1:
        raise FixtureError(
            "expected exactly one pinned OpenWebUI image reference in the manifest"
        )
    return refs.pop()


def run(args, *, env, timeout=120, check=True):
    completed = subprocess.run(
        args, env=env, capture_output=True, text=True, timeout=timeout
    )
    if check and completed.returncode != 0:
        raise FixtureError(
            f"command failed ({args[1]}): {redact(completed.stderr.strip()[-500:], env)}"
        )
    return completed


def redact(text: str, env: dict) -> str:
    for key in (
        "WEBUI_ADMIN_EMAIL",
        "WEBUI_ADMIN_PASSWORD",
        "WEBUI_FIXTURE_SIGNUP_EMAIL",
        "WEBUI_FIXTURE_SIGNUP_PASSWORD",
    ):
        value = env.get(key)
        if value:
            text = text.replace(value, "<fixture-credential>")
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", text)
    text = re.sub(r"eyJ[A-Za-z0-9._\-]{8,}", "<redacted-jwt>", text)
    return text


def container_status(env) -> str:
    done = run(
        [PODMAN, "inspect", "--format", "{{.State.Status}}", CONTAINER_NAME],
        env=env,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else "absent"


def start(image: str, data_dir: str, signup: bool, env: dict) -> None:
    run(
        [
            PODMAN,
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--pids-limit",
            "1024",
            "--volume",
            f"{data_dir}:/app/backend/data",
            "-e",
            "WEBUI_ADMIN_EMAIL",
            "-e",
            "WEBUI_ADMIN_PASSWORD",
            "-e",
            "ENABLE_PERSISTENT_CONFIG=false",
            "-e",
            f"ENABLE_SIGNUP={'true' if signup else 'false'}",
            "-e",
            "DEFAULT_USER_ROLE=user",
            image,
        ],
        env=env,
        timeout=180,
    )


def wait_ready(env: dict) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last = {"ready": False, "error": "not_started"}
    while time.monotonic() < deadline:
        status = container_status(env)
        if status in ("exited", "dead", "absent"):
            raise FixtureError(
                f"fixture container stopped before readiness (status={status}); "
                "logs withheld from the default report"
            )
        done = run(
            [PODMAN, "exec", CONTAINER_NAME, "python3", "-c", PROBE],
            env=env,
            timeout=30,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            try:
                last = json.loads(done.stdout.strip())
            except json.JSONDecodeError:
                last = {"ready": False, "error": "unparseable_probe"}
            if last.get("ready"):
                return last
        time.sleep(POLL_SECONDS)
    raise FixtureError(f"readiness timeout after {READY_TIMEOUT_SECONDS}s: {last}")


def signin_role(env: dict) -> dict:
    done = run(
        [
            PODMAN,
            "exec",
            "-e",
            "WEBUI_ADMIN_EMAIL",
            "-e",
            "WEBUI_ADMIN_PASSWORD",
            CONTAINER_NAME,
            "python3",
            "-c",
            SIGNIN_PROBE,
        ],
        env=env,
        timeout=40,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        return {"signin": False, "error": "exec_failed"}
    try:
        return json.loads(done.stdout.strip())
    except json.JSONDecodeError:
        return {"signin": False, "error": "unparseable_probe"}


def signup_attempt(env: dict) -> dict:
    done = run(
        [
            PODMAN,
            "exec",
            "-e",
            "WEBUI_FIXTURE_SIGNUP_EMAIL",
            "-e",
            "WEBUI_FIXTURE_SIGNUP_PASSWORD",
            CONTAINER_NAME,
            "python3",
            "-c",
            SIGNUP_PROBE,
        ],
        env=env,
        timeout=40,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        return {"signup": False, "error": "exec_failed"}
    try:
        return json.loads(done.stdout.strip())
    except json.JSONDecodeError:
        return {"signup": False, "error": "unparseable_probe"}


def stop_and_remove(env: dict) -> None:
    run([PODMAN, "rm", "-f", CONTAINER_NAME], env=env, timeout=90, check=False)


def main() -> int:
    if shutil.which(PODMAN) is None:
        print(f"SKIP: {PODMAN} is not available on this host", file=sys.stderr)
        return 77

    image = pinned_image()
    digest = image.rsplit("@", 1)[1]

    data_dir = tempfile.mkdtemp(
        prefix="owui-signup-fixture-", dir=os.environ.get("TMPDIR")
    )
    env = dict(os.environ)
    env["WEBUI_ADMIN_EMAIL"] = f"fixture-admin-{secrets.token_hex(6)}@example.invalid"
    env["WEBUI_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    env["WEBUI_FIXTURE_SIGNUP_EMAIL"] = (
        f"fixture-user-{secrets.token_hex(6)}@example.invalid"
    )
    env["WEBUI_FIXTURE_SIGNUP_PASSWORD"] = secrets.token_urlsafe(24)

    results: list[tuple[str, bool, str]] = []
    version = None

    try:
        if not run([PODMAN, "image", "exists", image], env=env, check=False).returncode == 0:
            print(f"pulling pinned image by digest {digest}", file=sys.stderr)
            run([PODMAN, "pull", image], env=env, timeout=1800)

        inspected = run(
            [PODMAN, "image", "inspect", "--format", "{{json .RepoDigests}}", image],
            env=env,
            timeout=60,
        ).stdout.strip()
        if digest not in inspected:
            raise FixtureError("pulled image digest does not match the manifest pin")

        # Scenario A: fresh bootstrap.
        start(image, data_dir, signup=True, env=env)
        obs_a = wait_ready(env)
        version = obs_a.get("version")
        results.append(
            (
                "A fresh bootstrap, ENABLE_SIGNUP=true",
                obs_a.get("enable_signup") is False,
                f"enable_signup={obs_a.get('enable_signup')} "
                f"enable_signin={obs_a.get('enable_signin')}",
            )
        )
        signin_a = signin_role(env)
        results.append(
            (
                "A bootstrapped admin can sign in",
                signin_a.get("signin") is True and signin_a.get("role") == "admin",
                f"signin={signin_a.get('signin')} role={signin_a.get('role')}",
            )
        )
        signup_a = signup_attempt(env)
        results.append(
            (
                "A fresh-bootstrap signup denied",
                signup_a.get("signup") is False
                and signup_a.get("status") in (400, 403),
                f"signup={signup_a.get('signup')} status={signup_a.get('status')}",
            )
        )
        stop_and_remove(env)

        # Scenario B: second start, same data, existing users, signup env true.
        start(image, data_dir, signup=True, env=env)
        obs_b = wait_ready(env)
        results.append(
            (
                "B second start, ENABLE_SIGNUP=true",
                obs_b.get("enable_signup") is True,
                f"enable_signup={obs_b.get('enable_signup')} "
                f"enable_signin={obs_b.get('enable_signin')}",
            )
        )
        signin_b = signin_role(env)
        results.append(
            (
                "B existing admin preserved across restart",
                signin_b.get("signin") is True and signin_b.get("role") == "admin",
                f"signin={signin_b.get('signin')} role={signin_b.get('role')}",
            )
        )
        signup_b = signup_attempt(env)
        results.append(
            (
                "B second-start signup accepted with default user role",
                signup_b.get("signup") is True and signup_b.get("role") == "user",
                f"signup={signup_b.get('signup')} status={signup_b.get('status')} "
                f"role={signup_b.get('role')}",
            )
        )
        stop_and_remove(env)

        # Scenario C: third start, same data, production value signup false.
        start(image, data_dir, signup=False, env=env)
        obs_c = wait_ready(env)
        results.append(
            (
                "C existing data, ENABLE_SIGNUP=false preserved",
                obs_c.get("enable_signup") is False,
                f"enable_signup={obs_c.get('enable_signup')} "
                f"enable_signin={obs_c.get('enable_signin')}",
            )
        )
        signup_c = signup_attempt(env)
        results.append(
            (
                "C preserved-policy signup denied",
                signup_c.get("signup") is False
                and signup_c.get("status") in (400, 403),
                f"signup={signup_c.get('signup')} status={signup_c.get('status')}",
            )
        )
    except FixtureError as exc:
        results.append(("fixture execution", False, redact(str(exc), env)))
    finally:
        stop_and_remove(env)
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"image: {image}")
    print(f"version: {version}")
    failed = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failed += 1
    if not results:
        print("FAIL fixture execution: no scenarios ran")
        failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
