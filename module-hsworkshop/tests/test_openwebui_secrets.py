#!/usr/bin/env python3
"""Offline unit tests for module-hsworkshop/apply-openwebui-secrets.sh (Task 2).

Fully local and offline: `oc` is a recorded mock, no cluster access, no real
secret values, no provision. Run:
    python3 -m unittest module-hsworkshop/tests/test_openwebui_secrets.py
or from module-hsworkshop:
    python3 -m unittest tests.test_openwebui_secrets
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "module-hsworkshop")
SCRIPT = os.path.join(MODULE, "apply-openwebui-secrets.sh")
MANIFEST = os.path.join(MODULE, "install", "openwebui.yaml")

EXPECTED_CONTEXT = "hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko"
EXPECTED_API = "https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443"

GOOD_CTX = EXPECTED_CONTEXT
GOOD_SRV = EXPECTED_API

ADMIN_EMAIL = "admin@hsworkshop.local"
ADMIN_PASSWORD = "supersecret-password-1"
PIPE_KEY = "pipe-key-abcdefghijklmnopqrstuvwxyz01"

try:
    import yaml  # PyYAML

    HAVE_YAML = True
except ImportError:  # pragma: no cover
    HAVE_YAML = False


def _b64(value):
    return base64.b64encode(value.encode()).decode().replace("\n", "")


MOCK_OC = r"""#!/usr/bin/env bash
echo "argv: $*" >> "${OC_LOG_PATH:-/dev/null}"
case "$1" in
  config)
    if [[ "$2" == "current-context" ]]; then
      if [[ -f "${OC_MOCK_CONTEXT_FILE:-}" ]]; then cat "${OC_MOCK_CONTEXT_FILE}"; else echo "default/none"; fi
      exit 0
    fi
    exit 0 ;;
  whoami)
    if [[ "$2" == "--show-server" ]]; then
      if [[ -f "${OC_MOCK_SERVER_FILE:-}" ]]; then cat "${OC_MOCK_SERVER_FILE}"; else echo "https://default:6443"; fi
      exit 0
    fi
    echo "mock-user"; exit 0 ;;
  apply)
    # Capture the manifest for inspection (find -f by argv pair, robust to
    # flag order) into a per-call copy under OC_MOCK_MANIFEST_DIR (the script
    # deletes its own temp files, which is asserted separately). Record the
    # original temp path for cleanup checks, then succeed.
    PREV=""
    for a in "$@"; do
      if [[ "${PREV}" == "-f" || "${PREV}" == "--filename" ]]; then
        if [[ -f "${a}" ]]; then
          if [[ -n "${OC_MOCK_MANIFEST_DIR:-}" && -d "${OC_MOCK_MANIFEST_DIR}" ]]; then
            N=$(ls "${OC_MOCK_MANIFEST_DIR}" 2>/dev/null | wc -l | tr -d ' ')
            cp "${a}" "${OC_MOCK_MANIFEST_DIR}/manifest-${N}.yaml"
          fi
          cp "${a}" "${OC_MOCK_LAST_MANIFEST:-/dev/null}"
        fi
        echo "${a}" >> "${OC_MOCK_MANIFEST_PATHS:-/dev/null}"
      fi
      PREV="${a}"
    done
    echo "applied"; exit 0 ;;
  *)
    echo "unexpected-subcommand: $*" >&2; exit 1 ;;
esac
"""


class OpenWebuiSecretsScriptTest(unittest.TestCase):
    """Behavioral tests against the script with a mocked oc binary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bindir)
        self.oc_log = os.path.join(self.tmp.name, "oc.log")
        self.last_manifest = os.path.join(self.tmp.name, "last.yaml")
        self.manifest_paths = os.path.join(self.tmp.name, "manifest-paths.log")
        self.manifest_dir = os.path.join(self.tmp.name, "manifests")
        os.makedirs(self.manifest_dir, exist_ok=True)
        oc_path = os.path.join(self.bindir, "oc")
        with open(oc_path, "w") as f:
            f.write(MOCK_OC)
        os.chmod(oc_path, 0o755)

    def _write_guard_files(self, ctx=GOOD_CTX, srv=GOOD_SRV):
        ctx_file = os.path.join(self.tmp.name, "ctx")
        srv_file = os.path.join(self.tmp.name, "srv")
        with open(ctx_file, "w") as f:
            f.write(ctx + "\n")
        with open(srv_file, "w") as f:
            f.write(srv + "\n")
        return ctx_file, srv_file

    def _run(self, stdin_text, ctx=GOOD_CTX, srv=GOOD_SRV, extra_env=None):
        ctx_file, srv_file = self._write_guard_files(ctx, srv)
        open(self.oc_log, "w").close()
        open(self.manifest_paths, "w").close()
        env = dict(os.environ)
        env.update(
            {
                "PATH": self.bindir + os.pathsep + env.get("PATH", ""),
                "OC_LOG_PATH": self.oc_log,
                "OC_MOCK_CONTEXT_FILE": ctx_file,
                "OC_MOCK_SERVER_FILE": srv_file,
                "OC_MOCK_LAST_MANIFEST": self.last_manifest,
                "OC_MOCK_MANIFEST_PATHS": self.manifest_paths,
                "OC_MOCK_MANIFEST_DIR": self.manifest_dir,
            }
        )
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["bash", SCRIPT],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc

    def _log(self):
        with open(self.oc_log) as f:
            return f.read()

    # -- Guards run BEFORE any input is consumed / any mutation happens --------

    def test_wrong_context_refused_before_apply(self):
        proc = self._run("x\n", ctx="wrong-context/other")
        self.assertNotEqual(proc.returncode, 0, "script ran with a wrong context")
        self.assertNotIn("apply", self._log(), "oc apply issued on wrong context")

    def test_wrong_server_refused_before_apply(self):
        proc = self._run("x\n", srv="https://api.some-other-cluster.example:6443")
        self.assertNotEqual(proc.returncode, 0, "script ran with a wrong server")
        self.assertNotIn("apply", self._log(), "oc apply issued on wrong server")

    def test_guard_refusal_output_has_no_secrets(self):
        # Refusal happens before input; even so, output must never carry values.
        proc = self._run(ADMIN_EMAIL + "\n" + ADMIN_PASSWORD + "\n" + PIPE_KEY + "\n",
                         ctx="wrong-context/other")
        for secret in (ADMIN_PASSWORD, PIPE_KEY):
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    # -- Missing / malformed input ----------------------------------------------

    def test_missing_input_fails_without_apply(self):
        # stdin closes with no lines -> all reads fail validation.
        proc = self._run("")
        self.assertNotEqual(proc.returncode, 0, "empty input was accepted")
        self.assertNotIn("apply", self._log(), "oc apply issued on empty input")

    def test_partial_input_fails_without_apply(self):
        proc = self._run(ADMIN_EMAIL + "\n" + ADMIN_PASSWORD + "\n")
        self.assertNotEqual(proc.returncode, 0, "partial input was accepted")
        self.assertNotIn("apply", self._log(), "oc apply issued on partial input")

    def test_semicolon_in_pipelines_key_rejected_admin_values_accepted(self):
        # The ';' delimiter constraint applies ONLY to PIPELINES_API_KEY
        # (it is composed into ';'-separated OPENAI_API_KEYS). The admin
        # password is the EXISTING credential and may legitimately contain
        # spaces, semicolons, or be short — any nonempty value is valid,
        # with no minimum length and no forced reset.
        for bad_key in ("key;with-semicolon000000", "key with space", ""):
            with self.subTest(key=bad_key):
                proc = self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{bad_key}\n")
                self.assertNotEqual(proc.returncode, 0, "bad pipelines key accepted")
                self.assertNotIn("apply", self._log(), "oc apply issued on bad pipelines key")

    def test_admin_password_with_spaces_semicolons_and_short_accepted(self):
        for pw in ("pass;word with space", "a", "p w ; d", "  spaced  "):
            with self.subTest(password=pw):
                proc = self._run(f"{ADMIN_EMAIL}\n{pw}\n{PIPE_KEY}\n")
                self.assertEqual(
                    proc.returncode, 0,
                    f"existing admin password {pw!r} was rejected (no forced reset): "
                    + proc.stderr,
                )
                self.assertEqual(self._log().count("argv: apply"), 2)
                for secret in (pw, PIPE_KEY):
                    # Guard: skip trivially-short values whose substrings
                    # would appear in any output (e.g. 'a' in 'applied').
                    if len(secret) >= 6:
                        self.assertNotIn(secret, proc.stdout + proc.stderr + self._log())

    def test_empty_admin_values_rejected(self):
        for email, pw in ((ADMIN_EMAIL, ""), ("", ADMIN_PASSWORD), ("", "")):
            with self.subTest(email=email, password=pw):
                proc = self._run(f"{email}\n{pw}\n{PIPE_KEY}\n")
                self.assertNotEqual(proc.returncode, 0, "empty admin value accepted")
                self.assertNotIn("apply", self._log(), "oc apply issued on empty admin value")

    def test_bad_email_rejected(self):
        for bad in ("not-an-email", "a b@example.com", "a@b c.example"):
            with self.subTest(email=bad):
                proc = self._run(f"{bad}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
                self.assertNotEqual(proc.returncode, 0, "bad email accepted")
                self.assertNotIn("apply", self._log(), "oc apply issued on bad email")

    def test_missing_secrets_file_is_error_not_prompt(self):
        proc = self._run(
            "x\n",
            extra_env={"OPENWEBUI_SECRETS_FILE": os.path.join(self.tmp.name, "nope.txt")},
        )
        self.assertNotEqual(proc.returncode, 0, "missing file silently accepted")
        self.assertNotIn("apply", self._log(), "oc apply issued on missing file")

    # -- Happy path ------------------------------------------------------------

    def test_happy_path_two_server_side_applies_of_exactly_two_secrets(self):
        proc = self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        self.assertEqual(proc.returncode, 0, "happy path failed: " + proc.stderr)
        log = self._log()
        self.assertEqual(log.count("argv: apply"), 2, "expected exactly 2 oc apply calls")
        self.assertIn("--server-side", log, "applies are not server-side")
        self.assertNotIn("--force-conflicts", log, "force-conflicts must never be used")
        # No restart / delete / patch / rollout / secret reads.
        self.assertNotRegex(log, r"argv: (rollout|delete|patch|set|get)\b",
                            "script issued commands beyond secret apply")
        self.assertNotIn("openwebui-secret", log.replace("openwebui-secrets", ""),
                         "legacy openwebui-secret referenced in oc argv")

    def test_no_credentials_in_argv_or_output(self):
        proc = self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        combined = proc.stdout + proc.stderr + self._log()
        for secret in (ADMIN_PASSWORD, PIPE_KEY):
            self.assertNotIn(secret, combined, "credential leaked to output/argv")
        # The admin email is a username, not a credential; it may appear in
        # neither argv (it is read via stdin) nor output regardless:
        self.assertNotIn(ADMIN_EMAIL, self._log(), "admin email leaked into oc argv")

    def test_manifests_contain_expected_keys_and_encoded_values(self):
        proc = self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Both applied manifests were captured by the mock (persistent copies;
        # the script's own temp files are deleted, which is asserted in
        # test_temp_manifests_cleaned_up_on_success).
        with open(self.manifest_paths) as f:
            paths = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(paths), 2, f"expected 2 manifest paths, got {paths}")
        copies = sorted(
            os.path.join(self.manifest_dir, n) for n in os.listdir(self.manifest_dir)
        )
        self.assertEqual(len(copies), 2, f"expected 2 captured manifests, got {copies}")
        contents = []
        for p in copies:
            with open(p) as f:
                contents.append(f.read())
        joined = "\n---\n".join(contents)
        self.assertIn("openwebui-connections-secret", joined)
        self.assertIn("openwebui-admin-bootstrap-secret", joined)
        self.assertIn("GUARDRAILS_API_KEY: " + _b64("unused-guardrails-placeholder"), joined)
        self.assertIn("PIPELINES_API_KEY: " + _b64(PIPE_KEY), joined)
        self.assertIn("WEBUI_ADMIN_EMAIL: " + _b64(ADMIN_EMAIL), joined)
        self.assertIn("WEBUI_ADMIN_PASSWORD: " + _b64(ADMIN_PASSWORD), joined)
        # Values are base64-encoded, never plaintext.
        for plaintext in (ADMIN_PASSWORD, PIPE_KEY):
            self.assertNotRegex(joined, re.escape(plaintext) + r"\n")

    def test_temp_manifests_cleaned_up_on_success(self):
        self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        with open(self.manifest_paths) as f:
            paths = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(paths), 2)
        for p in paths:
            self.assertFalse(os.path.exists(p), f"temp manifest not deleted: {p}")

    def test_temp_manifests_cleaned_up_on_apply_failure(self):
        # Make apply fail on the FIRST call only, so the script aborts between
        # the two applies; the trap must still remove every temp manifest.
        oc_path = os.path.join(self.bindir, "oc")
        with open(oc_path, "w") as f:
            f.write(
                MOCK_OC.replace(
                    'echo "applied"; exit 0 ;;',
                    'if [[ -f "${OC_MOCK_FAIL_ONCE:-}" ]]; then rm -f "${OC_MOCK_FAIL_ONCE}"; echo "apply failed" >&2; exit 1; fi; echo "applied"; exit 0 ;;',
                )
            )
        os.chmod(oc_path, 0o755)
        fail_once = os.path.join(self.tmp.name, "fail-once")
        open(fail_once, "w").close()
        proc = self._run(
            f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n",
            extra_env={"OC_MOCK_FAIL_ONCE": fail_once},
        )
        self.assertNotEqual(proc.returncode, 0, "failing apply did not fail the script")
        with open(self.manifest_paths) as f:
            paths = [l.strip() for l in f if l.strip()]
        self.assertGreaterEqual(len(paths), 1, "no manifest was applied before the failure")
        for p in paths:
            self.assertFalse(os.path.exists(p), f"temp manifest not deleted on failure: {p}")
        # No credential leak on failure.
        for secret in (ADMIN_PASSWORD, PIPE_KEY):
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_secrets_file_input_mode(self):
        secrets_file = os.path.join(self.tmp.name, "secrets.env")
        with open(secrets_file, "w") as f:
            f.write(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        os.chmod(secrets_file, 0o600)
        proc = self._run("", extra_env={"OPENWEBUI_SECRETS_FILE": secrets_file})
        self.assertEqual(proc.returncode, 0, "file input failed: " + proc.stderr)
        self.assertEqual(self._log().count("argv: apply"), 2)

    def test_secrets_file_wrong_permissions_rejected(self):
        secrets_file = os.path.join(self.tmp.name, "secrets.env")
        with open(secrets_file, "w") as f:
            f.write(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        os.chmod(secrets_file, 0o644)
        proc = self._run("", extra_env={"OPENWEBUI_SECRETS_FILE": secrets_file})
        self.assertNotEqual(proc.returncode, 0, "non-0600 file accepted")
        self.assertNotIn("apply", self._log(), "oc apply issued on non-0600 file")

    def test_apply_error_does_not_echo_secret_values(self):
        # Make apply fail: replace the mock's apply with a failing one.
        oc_path = os.path.join(self.bindir, "oc")
        with open(oc_path, "w") as f:
            f.write(MOCK_OC.replace('echo "applied"; exit 0 ;;',
                                   'echo "apply failed" >&2; exit 1 ;;'))
        os.chmod(oc_path, 0o755)
        proc = self._run(f"{ADMIN_EMAIL}\n{ADMIN_PASSWORD}\n{PIPE_KEY}\n")
        self.assertNotEqual(proc.returncode, 0, "failing apply did not fail the script")
        combined = proc.stdout + proc.stderr
        for secret in (ADMIN_PASSWORD, PIPE_KEY):
            self.assertNotIn(secret, combined, "credential leaked on apply error")


class OpenWebuiManifestTest(unittest.TestCase):
    """Static assertions on install/openwebui.yaml (Task 2 scope)."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_YAML:
            raise unittest.SkipTest("PyYAML not available")
        with open(MANIFEST) as f:
            cls.docs = [d for d in yaml.safe_load_all(f) if d]
        cls.deployment = [d for d in cls.docs if d["kind"] == "Deployment"][0]
        cls.env = cls.deployment["spec"]["template"]["spec"]["containers"][0]["env"]

    def _env_map(self):
        return {e["name"]: e for e in self.env}

    def test_image_pinned_to_frozen_digest(self):
        image = self.deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(
            image,
            "ghcr.io/open-webui/open-webui@sha256:"
            "0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd",
        )

    def test_plural_urls_ordered_no_whitespace(self):
        envs = self._env_map()
        self.assertEqual(
            envs["OPENAI_API_BASE_URLS"]["value"],
            "http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1;"
            "http://pipelines.hsworkshop.svc.cluster.local:9099",
        )
        self.assertNotIn(" ", envs["OPENAI_API_BASE_URLS"]["value"])

    def test_singular_connection_env_removed(self):
        envs = self._env_map()
        self.assertNotIn("OPENAI_API_BASE_URL", envs)
        self.assertNotIn("OPENAI_API_KEY", envs)

    def test_secret_key_refs_precede_openai_api_keys(self):
        names = [e["name"] for e in self.env]
        self.assertIn("GUARDRAILS_API_KEY", names)
        self.assertIn("PIPELINES_API_KEY", names)
        self.assertLess(names.index("GUARDRAILS_API_KEY"), names.index("OPENAI_API_KEYS"))
        self.assertLess(names.index("PIPELINES_API_KEY"), names.index("OPENAI_API_KEYS"))
        envs = self._env_map()
        for ref_name in ("GUARDRAILS_API_KEY", "PIPELINES_API_KEY"):
            skr = envs[ref_name]["valueFrom"]["secretKeyRef"]
            self.assertEqual(skr["name"], "openwebui-connections-secret")
            self.assertEqual(skr["key"], ref_name)
        self.assertEqual(
            envs["OPENAI_API_KEYS"]["value"],
            "$(GUARDRAILS_API_KEY);$(PIPELINES_API_KEY)",
        )

    def test_no_openai_api_configs(self):
        # Freeze decision: plural lists are index-paired and sufficient; the
        # verified-index JSON config env is not needed.
        self.assertNotIn("OPENAI_API_CONFIGS", self._env_map())

    def test_startup_config_policy_envs(self):
        envs = self._env_map()
        self.assertEqual(envs["ENABLE_PERSISTENT_CONFIG"]["value"], "false")
        self.assertEqual(envs["ENABLE_SIGNUP"]["value"], "false")
        self.assertEqual(envs["DEFAULT_USER_ROLE"]["value"], "user")

    def test_admin_bootstrap_refs_override_legacy_envfrom(self):
        envs = self._env_map()
        for key in ("WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD"):
            skr = envs[key]["valueFrom"]["secretKeyRef"]
            self.assertEqual(skr["name"], "openwebui-admin-bootstrap-secret")
            self.assertEqual(skr["key"], key)
        # env entries win over envFrom for the same key names (Kubernetes
        # precedence), and the legacy envFrom is preserved for other keys.
        env_from = self.deployment["spec"]["template"]["spec"]["containers"][0]["envFrom"]
        self.assertTrue(
            any(sf.get("secretRef", {}).get("name") == "openwebui-secret" for sf in env_from),
            "legacy openwebui-secret envFrom was removed",
        )

    def test_preserved_workshop_settings(self):
        envs = self._env_map()
        self.assertEqual(envs["ENABLE_OPENAI_API"]["value"], "true")
        self.assertEqual(envs["ENABLE_OLLAMA_API"]["value"], "false")
        self.assertEqual(envs["WEBUI_NAME"]["value"], "HS Workshop AI")
        self.assertEqual(envs["LANGFUSE_HOST"]["value"], "http://langfuse:3000")

    def test_web_search_preserved_off_with_0125_env_names(self):
        # Inventoried live state: web.search.enable=false (docs §13). 0.12.5
        # reads ENABLE_WEB_SEARCH/WEB_SEARCH_ENGINE, not the removed legacy
        # RAG_* names.
        envs = self._env_map()
        self.assertEqual(envs["ENABLE_WEB_SEARCH"]["value"], "false")
        self.assertEqual(envs["WEB_SEARCH_ENGINE"]["value"], "searxng")
        self.assertEqual(
            envs["SEARXNG_QUERY_URL"]["value"],
            "http://searxng:8080/search?q=<query>&format=json",
        )
        self.assertNotIn("ENABLE_RAG_WEB_SEARCH", envs)
        self.assertNotIn("RAG_WEB_SEARCH_ENGINE", envs)

    def test_code_execution_preserved_on_with_pyodide(self):
        # Inventoried live state: code_execution/code_interpreter enabled with
        # engine pyodide; declared explicitly for persistence-off.
        envs = self._env_map()
        self.assertEqual(envs["ENABLE_CODE_EXECUTION"]["value"], "true")
        self.assertEqual(envs["CODE_EXECUTION_ENGINE"]["value"], "pyodide")
        self.assertEqual(envs["ENABLE_CODE_INTERPRETER"]["value"], "true")
        self.assertEqual(envs["CODE_INTERPRETER_ENGINE"]["value"], "pyodide")

    def test_pvc_route_tls_preserved(self):
        pvc = [d for d in self.docs if d["kind"] == "PersistentVolumeClaim"][0]
        self.assertEqual(pvc["metadata"]["name"], "openwebui-data")
        route = [d for d in self.docs if d["kind"] == "Route"][0]
        self.assertEqual(route["metadata"]["name"], "openwebui")
        self.assertEqual(route["spec"]["tls"]["termination"], "edge")
        self.assertEqual(
            route["spec"]["host"],
            "openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com",
        )

    def test_no_plaintext_secret_values(self):
        with open(MANIFEST) as f:
            text = f.read()
        self.assertNotIn("password:", text.lower().replace("webui_admin_password", ""))
        # The composed keys use dependency references, not literal values.
        for e in self.env:
            if e["name"] == "OPENAI_API_KEYS":
                self.assertNotRegex(e.get("value", ""), r"[A-Za-z0-9]{20,}",
                                    "OPENAI_API_KEYS contains a literal value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
