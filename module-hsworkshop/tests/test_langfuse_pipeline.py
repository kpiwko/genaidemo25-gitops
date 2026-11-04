#!/usr/bin/env python3
"""Offline unit tests for the Task 3 Pipelines/Langfuse filter deliverables.

Fully local and offline: the Langfuse SDK and the Pipelines server module are
mocked; no cluster access, no network, no real key material. Run:
    python3 -m unittest module-hsworkshop/tests/test_langfuse_pipeline.py
or from module-hsworkshop:
    python3 -m unittest tests.test_langfuse_pipeline
"""

import asyncio
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "module-hsworkshop")
FILTER = os.path.join(MODULE, "pipelines", "langfuse_v4_filter_pipeline.py")
CHECK_READY = os.path.join(MODULE, "pipelines", "check_ready.py")
LOCK = os.path.join(MODULE, "pipelines", "requirements.lock")
MANIFEST = os.path.join(MODULE, "install", "pipelines.yaml")
KUSTOMIZATION = os.path.join(MODULE, "install", "kustomization.yaml")

SECRET_KEY = "sk-lf-test-secret-key-000000000000"
PUBLIC_KEY = "pk-lf-test-public-key-000000000000"
API_KEY = "pipe-key-abcdefghijklmnopqrstuvwxyz01"

try:
    import yaml  # PyYAML

    HAVE_YAML = True
except ImportError:  # pragma: no cover
    HAVE_YAML = False


def _load_filter_module():
    """Import the filter file with the Pipelines/Langfuse deps mocked."""
    fake_pipelines_main = mock.MagicMock()
    fake_pipelines_main.get_last_assistant_message = lambda messages: ""
    langfuse_mod = mock.MagicMock()
    langfuse_mod.Langfuse = mock.MagicMock()
    langfuse_mod.propagate_attributes = mock.MagicMock()

    class _Field:
        def __init__(self, default=None):
            self.default = default

    class _StubBaseModel:
        """Minimal pydantic.BaseModel stand-in for the Valves model."""

        def __init__(self, **kwargs):
            fields = self.__class__._stub_fields()
            for name in fields:
                setattr(self, name, kwargs.pop(name, None))
            # Fields with bare annotations (no default) are required by real
            # pydantic; accept and store any remaining keys so the stub
            # behaves like the real Valves model for this test suite.
            for name, value in kwargs.items():
                setattr(self, name, value)

        @classmethod
        def _stub_fields(cls):
            fields = {}
            for klass in reversed(cls.__mro__):
                for name, default in vars(klass).items():
                    if name.startswith("_") or callable(default):
                        continue
                    fields[name] = default
            return fields

        def model_dump(self):
            return {k: getattr(self, k) for k in self._stub_fields()}

    pydantic_mod = mock.MagicMock()
    pydantic_mod.BaseModel = _StubBaseModel

    # utils.pipelines.main and langfuse/pydantic are stubbed so the module
    # imports without the container environment.
    for name, mod in (
        ("utils", mock.MagicMock()),
        ("utils.pipelines", mock.MagicMock()),
        ("utils.pipelines.main", fake_pipelines_main),
        ("langfuse", langfuse_mod),
        ("pydantic", pydantic_mod),
    ):
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location(
        "langfuse_v4_filter_pipeline_under_test", FILTER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, langfuse_mod


class FilterValvesTest(unittest.TestCase):
    """Persisted-valves overlay: managed keys are re-asserted everywhere."""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls.langfuse = _load_filter_module()

    def _make_pipeline(self, **env):
        base = {
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        base.update({k: v for k, v in env.items() if v is not None})
        for k in [k for k, v in env.items() if v is None]:
            base.pop(k, None)
        with mock.patch.dict(os.environ, base, clear=False):
            return self.mod.Pipeline()

    def test_managed_keys_forced_on_construction(self):
        p = self._make_pipeline()
        self.assertEqual(p.valves.pipelines, ["*"])
        self.assertEqual(p.valves.priority, 0)
        self.assertFalse(p.valves.debug)
        self.assertEqual(p.valves.host, "http://langfuse:3000")
        self.assertEqual(p.valves.secret_key, SECRET_KEY)
        self.assertEqual(p.valves.public_key, PUBLIC_KEY)

    def test_missing_env_creds_fail_closed_no_cloud_default(self):
        for missing in ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST"):
            with self.subTest(missing=missing):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(self.mod.MissingEnvCredentialsError):
                        self._make_pipeline(**{missing: None})
                self.assertIn(missing, buf.getvalue())
                # No personal-cloud fallback host is ever used.
                self.assertNotIn("cloud.langfuse.com", buf.getvalue())

    def test_managed_keys_and_env_creds_reasserted_after_persisted_overlay(self):
        # Simulate the Pipelines server valves.json overlay: persisted values
        # override code defaults for EVERY key - including STALE host/keys.
        p = self._make_pipeline()
        overlay = {
            "pipelines": ["some-private-model"],
            "priority": 99,
            "debug": True,
            "host": "http://stale-persisted.example:3000",
            "secret_key": "stale-persisted-secret",
            "public_key": "stale-persisted-public",
        }
        merged = {**p.valves.model_dump(), **overlay}
        p.valves = p.Valves(**merged)
        # ... and the server then calls on_valves_updated (env still Secret-
        # backed in the pod; patch it for the local test run):
        env = {
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(p, "set_langfuse"):
            asyncio.run(p.on_valves_updated())
        # Managed keys forced back to declared values.
        self.assertEqual(p.valves.pipelines, ["*"], "pipelines valve not forced back to wildcard")
        self.assertEqual(p.valves.priority, 0, "priority valve not forced back to 0")
        self.assertFalse(p.valves.debug, "debug valve not forced off")
        # Stale persisted host/keys overridden by ACTUAL env (Secret) values.
        self.assertEqual(p.valves.host, "http://langfuse:3000")
        self.assertEqual(p.valves.secret_key, SECRET_KEY)
        self.assertEqual(p.valves.public_key, PUBLIC_KEY)
        self.assertNotIn("stale-persisted", p.valves.host + p.valves.secret_key + p.valves.public_key)

    def test_startup_reasserts_managed_keys_and_env_creds(self):
        p = self._make_pipeline()
        p.valves.pipelines = ["narrow"]
        p.valves.priority = 42
        p.valves.debug = True
        p.valves.host = "http://stale.example:3000"
        env = {
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(p, "_startup_authenticate"):
            asyncio.run(p.on_startup())
        self.assertEqual(p.valves.pipelines, ["*"])
        self.assertEqual(p.valves.priority, 0)
        self.assertFalse(p.valves.debug)
        self.assertEqual(p.valves.host, "http://langfuse:3000")

    def test_valves_update_with_missing_env_fails_closed(self):
        # After an overlay, a missing env credential must still raise, not
        # silently continue with persisted values.
        p = self._make_pipeline()
        with mock.patch.dict(os.environ, {"LANGFUSE_SECRET_KEY": ""}, clear=False):
            with self.assertRaises(self.mod.MissingEnvCredentialsError):
                asyncio.run(p.on_valves_updated())


class _FakeApiError(Exception):
    """Stand-in for the Fern-generated langfuse ApiError (SDK 5.0.4).

    Real shape (verified in the pinned wheel): status_code: Optional[int],
    headers: Optional[dict], body: Any. Only status_code is used for
    classification - never the string form, whose body/headers may contain
    misleading substrings (e.g. a 503 whose body mentions 401).
    """

    def __init__(self, status_code=None, headers=None, body=None):
        super().__init__(f"headers: {headers}, status_code: {status_code}, body: {body}")
        self.status_code = status_code
        self.headers = headers
        self.body = body


class FilterStartupAuthTest(unittest.TestCase):
    """Bounded startup authentication: require True, invalid creds raise."""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls.langfuse = _load_filter_module()

    def _make_pipeline(self):
        with mock.patch.dict(
            os.environ,
            {"LANGFUSE_SECRET_KEY": SECRET_KEY,
             "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
             "LANGFUSE_HOST": "http://langfuse:3000"},
            clear=False,
        ):
            return self.mod.Pipeline()

    def _fake_client(self, get_side_effect=None, get_return=None):
        # Reset the shared module-level Langfuse factory mock (loaded once
        # per class) so call counts are per-test.
        self.langfuse.Langfuse.reset_mock()
        client = mock.MagicMock()
        projects = mock.MagicMock()
        projects.data = get_return
        client.api.projects.get.side_effect = get_side_effect
        client.api.projects.get.return_value = projects
        self.langfuse.Langfuse.return_value = client
        return client

    def test_successful_auth_sets_client_with_bounded_request_options(self):
        p = self._make_pipeline()
        client = self._fake_client(get_return=[mock.MagicMock()])
        p._startup_authenticate()
        self.assertIs(p.langfuse, client)
        client.shutdown.assert_not_called()  # success keeps the client
        # Pinned-SDK request mechanism with EXPLICIT bounds: max_retries=0
        # (no Fern internal retries) and timeout capped to the remaining
        # budget (never beyond the 300s deadline).
        _, kwargs = client.api.projects.get.call_args
        opts = kwargs["request_options"]
        self.assertEqual(opts["max_retries"], 0)
        self.assertLessEqual(opts["timeout_in_seconds"],
                             self.mod.AUTH_PER_CALL_TIMEOUT_SECONDS)
        self.assertGreaterEqual(opts["timeout_in_seconds"], 1)

    def test_transient_retry_success_factory_once_no_poisoning(self):
        # 5.0.4 singleton semantics: LangfuseResourceManager is keyed by
        # public_key; a shutdown client would poison every later Langfuse()
        # in the process. After transient failures the ONE client must still
        # be alive and usable (no shutdown on retry), and the factory must be
        # called exactly once.
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = [
            _FakeApiError(status_code=503),  # transient
            OSError("connection reset"),  # transient, untyped
            mock.MagicMock(data=[mock.MagicMock()]),  # success
        ]
        with mock.patch.object(self.mod.time, "sleep"):
            p._startup_authenticate()
        # Factory called exactly ONCE - the client is created before the
        # retry loop and reused for every attempt.
        self.assertEqual(self.langfuse.Langfuse.call_count, 1)
        # NO shutdown on transient retries or on success.
        client.shutdown.assert_not_called()
        self.assertIs(p.langfuse, client)
        # The surviving client is usable for runtime work (scores/media
        # enqueue) after transient failures - proving it was not poisoned.
        client.add_score_task.assert_not_called()  # nothing enqueued during auth
        client.add_score_task(
            {"trace_id": "t", "name": "n", "value": 1, "data_type": "NUMERIC"}
        )
        client.media_manager.flush()
        self.assertEqual(client.api.projects.get.call_count, 3)

    def test_transient_exhaust_deadline_factory_once_single_shutdown(self):
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = _FakeApiError(
            status_code=503, body="service unavailable"
        )

        class FakeClock:
            def __init__(self):
                self.t = 0.0

            def __call__(self):
                value = self.t
                self.t += 60.0
                return value

        with mock.patch.object(self.mod.time, "monotonic", side_effect=FakeClock()), \
             mock.patch.object(self.mod.time, "sleep"), \
             redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(self.mod.StartupTimeoutError):
                p._startup_authenticate()
        # One client for all attempts; exactly one shutdown at the terminal
        # deadline failure (never per-retry).
        self.assertEqual(self.langfuse.Langfuse.call_count, 1)
        client.shutdown.assert_called_once()
        # No raw error bodies logged (only the exception class name).
        self.assertNotIn("service unavailable", out.getvalue())
        self.assertNotIn(SECRET_KEY, out.getvalue())
        self.assertNotIn(PUBLIC_KEY, out.getvalue())

    def test_invalid_credentials_factory_once_single_shutdown(self):
        p = self._make_pipeline()
        client = self._fake_client(
            get_side_effect=_FakeApiError(
                status_code=401, body={"error": "invalid key " + SECRET_KEY}
            )
        )
        with mock.patch.object(self.mod.time, "sleep"), \
             redirect_stdout(io.StringIO()) as buf:
            with self.assertRaises(self.mod.InvalidCredentialsError):
                p._startup_authenticate()
        self.assertEqual(self.langfuse.Langfuse.call_count, 1)
        client.shutdown.assert_called_once()  # terminal: singleton released
        self.assertNotIn(SECRET_KEY, buf.getvalue())
        self.assertNotIn(PUBLIC_KEY, buf.getvalue())

    def test_empty_project_list_fails_closed_immediately(self):
        # auth_check()-equivalent semantics (5.0.4 source): zero projects for
        # the keys == credentials wrong for this project -> InvalidCredentials
        # without retry. Terminal: singleton released once.
        p = self._make_pipeline()
        client = self._fake_client(get_return=[])
        with mock.patch.object(self.mod.time, "sleep") as sleep, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(self.mod.InvalidCredentialsError):
                p._startup_authenticate()
        sleep.assert_not_called()
        self.assertEqual(self.langfuse.Langfuse.call_count, 1)
        client.shutdown.assert_called_once()

    def test_invalid_credentials_typed_401_raise_immediately(self):
        p = self._make_pipeline()
        client = self._fake_client(
            get_side_effect=_FakeApiError(status_code=401, body={"error": "unauthorized"})
        )
        with mock.patch.object(self.mod.time, "sleep") as sleep, \
             redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(self.mod.InvalidCredentialsError):
                p._startup_authenticate()
        sleep.assert_not_called()
        client.shutdown.assert_called_once()  # failed client released
        self.assertNotIn(SECRET_KEY, out.getvalue())
        self.assertNotIn(PUBLIC_KEY, out.getvalue())

    def test_typed_403_invalid_credentials_raise_immediately(self):
        p = self._make_pipeline()
        self._fake_client(get_side_effect=_FakeApiError(status_code=403))
        with mock.patch.object(self.mod.time, "sleep") as sleep:
            with self.assertRaises(self.mod.InvalidCredentialsError):
                p._startup_authenticate()
        sleep.assert_not_called()

    def test_typed_503_with_misleading_401_403_substrings_is_transient(self):
        # A 503 whose headers/content-length/body contain 401/403 substrings
        # must NOT be misclassified as invalid credentials: only the TYPED
        # status_code counts. It retries and then succeeds.
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = [
            _FakeApiError(
                status_code=503,
                headers={"content-length": "4010", "retry-after": "60"},
                body={"message": "upstream returned 403 for dependency; try later"},
            ),
            _FakeApiError(status_code=502, headers={"content-length": "403"}),
            mock.MagicMock(data=[mock.MagicMock()]),
        ]
        with mock.patch.object(self.mod.time, "sleep") as sleep:
            p._startup_authenticate()
        self.assertEqual(client.api.projects.get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_untyped_errors_are_transient_not_invalid(self):
        # Untyped exceptions (no status_code attribute) are conservatively
        # retried, never classified as invalid credentials.
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = [
            OSError("connection refused"),
            mock.MagicMock(data=[mock.MagicMock()]),
        ]
        with mock.patch.object(self.mod.time, "sleep"):
            p._startup_authenticate()
        self.assertEqual(client.api.projects.get.call_count, 2)

    def test_transient_errors_retry_then_succeed_within_budget(self):
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = [
            _FakeApiError(status_code=503),  # transient
            _FakeApiError(status_code=502),  # transient
            mock.MagicMock(data=[mock.MagicMock()]),  # success
        ]
        with mock.patch.object(self.mod.time, "sleep") as sleep:
            p._startup_authenticate()
        self.assertEqual(client.api.projects.get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_transient_errors_exhaust_budget_and_raise(self):
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = _FakeApiError(
            status_code=503, body="service unavailable"
        )

        class FakeClock:
            def __init__(self):
                self.t = 0.0

            def __call__(self):
                value = self.t
                self.t += 60.0
                return value

        with mock.patch.object(self.mod.time, "monotonic", side_effect=FakeClock()), \
             mock.patch.object(self.mod.time, "sleep"), \
             redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(self.mod.StartupTimeoutError):
                p._startup_authenticate()
        # No raw error bodies logged (only the exception class name).
        self.assertNotIn("service unavailable", out.getvalue())
        self.assertNotIn(SECRET_KEY, out.getvalue())
        self.assertNotIn(PUBLIC_KEY, out.getvalue())

    def test_per_call_timeout_never_exceeds_remaining_budget(self):
        p = self._make_pipeline()
        client = self._fake_client()
        client.api.projects.get.side_effect = _FakeApiError(status_code=503)
        # 290s elapsed at deadline setup (deadline = 590); by the first loop
        # iteration's remaining-budget check the clock reads 580 -> only 10s
        # remain, so the per-call timeout must be capped to <=10s.
        class FakeClock:
            values = [290.0, 580.0]
            idx = 0

            def __call__(self):
                if self.idx < len(self.values):
                    v = self.values[self.idx]
                else:
                    v = self.values[-1] + 60.0 * (self.idx - len(self.values) + 1)
                self.idx += 1
                return v

        with mock.patch.object(self.mod.time, "monotonic", side_effect=FakeClock()), \
             mock.patch.object(self.mod.time, "sleep"):
            with self.assertRaises(self.mod.StartupTimeoutError):
                p._startup_authenticate()
        _, kwargs = client.api.projects.get.call_args
        self.assertLessEqual(kwargs["request_options"]["timeout_in_seconds"], 10,
                             "per-call timeout exceeded remaining budget")

    def test_on_startup_exception_propagates(self):
        # The Pipelines lifespan handler does not catch module on_startup
        # exceptions - an auth failure must propagate to fail the process.
        p = self._make_pipeline()
        env = {
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(
            p, "_startup_authenticate",
            side_effect=self.mod.InvalidCredentialsError("bad keys"),
        ):
            with self.assertRaises(self.mod.InvalidCredentialsError):
                asyncio.run(p.on_startup())

    def test_no_secret_leak_in_startup_failure_messages(self):
        p = self._make_pipeline()
        client = self._fake_client(get_side_effect=_FakeApiError(
            status_code=401,
            body={"error": "invalid key " + SECRET_KEY},
        ))
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(self.mod.InvalidCredentialsError):
                p._startup_authenticate()
        self.assertNotIn(SECRET_KEY, buf.getvalue())
        self.assertNotIn(PUBLIC_KEY, buf.getvalue())


class FilterRuntimeFailOpenTest(unittest.TestCase):
    """Runtime (valves-update) reinit stays fail-open; shutdown is idempotent."""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls.langfuse = _load_filter_module()

    def _make_pipeline(self):
        with mock.patch.dict(
            os.environ,
            {"LANGFUSE_SECRET_KEY": SECRET_KEY,
             "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
             "LANGFUSE_HOST": "http://langfuse:3000"},
            clear=False,
        ):
            return self.mod.Pipeline()

    def test_valves_update_auth_failure_disables_client_not_process(self):
        p = self._make_pipeline()
        env = {
            "LANGFUSE_SECRET_KEY": SECRET_KEY,
            "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
            "LANGFUSE_HOST": "http://langfuse:3000",
        }
        # Typed SDK-like auth failure on the runtime (fail-open) path.
        client = mock.MagicMock()
        client.auth_check.side_effect = _FakeApiError(status_code=401)
        self.langfuse.Langfuse.return_value = client
        # Must not raise (runtime fail-open preserved).
        with mock.patch.dict(os.environ, env, clear=False):
            asyncio.run(p.on_valves_updated())
        self.assertIsNone(p.langfuse)

    def test_repeated_shutdown_no_error(self):
        p = self._make_pipeline()
        client = mock.MagicMock()
        client.api.projects.get.return_value = mock.MagicMock(data=[mock.MagicMock()])
        self.langfuse.Langfuse.return_value = client
        p._startup_authenticate()
        asyncio.run(p.on_shutdown())
        asyncio.run(p.on_shutdown())  # second call: client already None
        client.shutdown.assert_called_once()

    def test_runtime_fail_open_on_uninitialized_client(self):
        # inlet/outlet with no client pass the body through unchanged.
        p = self._make_pipeline()
        p.langfuse = None
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"chat_id": "c1"}}
        out = asyncio.run(p.inlet(dict(body)))
        self.assertEqual(out["model"], "m")


class CheckReadyTest(unittest.TestCase):
    """Readiness helper: env key, authenticated discovery, no argv key."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("check_ready_under_test", CHECK_READY)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _run_main(self, env, response=None, error=None):
        response_obj = mock.MagicMock()
        response_obj.status = 200
        response_obj.read.return_value = (
            response if isinstance(response, bytes) else b"{}"
        )
        urlopen = mock.MagicMock()
        if error is not None:
            urlopen.side_effect = error
        else:
            urlopen.return_value.__enter__.return_value = response_obj
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(self.mod.urllib.request, "urlopen", urlopen):
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = self.mod.main()
        return code, buf.getvalue(), urlopen

    def test_ready_when_filter_discovered(self):
        payload = (
            b'{"data": [{"name": "Langfuse Filter", "pipeline": '
            b'{"type": "filter", "pipelines": ["*"]}}]}'
        )
        code, _, urlopen = self._run_main(
            {"PIPELINES_API_KEY": API_KEY}, response=payload
        )
        self.assertEqual(code, 0)
        # Key from env, sent as bearer header - never in argv.
        req = urlopen.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer " + API_KEY)
        self.assertEqual(req.full_url.endswith("/models"), True)
        # Bounded timeout on the call.
        self.assertEqual(urlopen.call_args[1].get("timeout"),
                         self.mod.HTTP_TIMEOUT_SECONDS)

    def test_missing_env_key_fails_without_request(self):
        env = {k: v for k, v in os.environ.items() if k != "PIPELINES_API_KEY"}
        env["PIPELINES_API_KEY"] = ""
        code, err, urlopen = self._run_main(env)
        self.assertEqual(code, 1)
        self.assertIn("PIPELINES_API_KEY", err)
        urlopen.assert_not_called()

    def test_wrong_key_rejected(self):
        import urllib.error
        code, err, _ = self._run_main(
            {"PIPELINES_API_KEY": "wrong-key"},
            error=urllib.error.HTTPError(
                "url", 401, "Unauthorized", hdrs=None, fp=None
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertNotIn("wrong-key", err)

    def test_server_down_fails_cleanly(self):
        import urllib.error
        code, err, _ = self._run_main(
            {"PIPELINES_API_KEY": API_KEY},
            error=urllib.error.URLError("connection refused"),
        )
        self.assertEqual(code, 1)
        self.assertNotIn(API_KEY, err)

    def test_ready_when_filter_matched_by_id(self):
        payload = (
            b'{"data": [{"id": "langfuse_v4_filter_pipeline", "pipeline": '
            b'{"type": "filter", "pipelines": ["*"]}}]}'
        )
        code, _, _ = self._run_main(
            {"PIPELINES_API_KEY": API_KEY}, response=payload
        )
        self.assertEqual(code, 0)

    def test_filter_wrong_type_or_wildcard_not_ready(self):
        payload = (
            b'{"data": [{"name": "Langfuse Filter", "pipeline": '
            b'{"type": "pipe", "pipelines": ["*"]}}]}'
        )
        code, err, _ = self._run_main(
            {"PIPELINES_API_KEY": API_KEY}, response=payload
        )
        self.assertEqual(code, 1)
        self.assertIn("mismatch", err)

    def test_filter_absent_not_ready(self):
        payload = b'{"data": [{"name": "Other Pipe", "pipeline": {"type": "pipe"}}]}'
        code, err, _ = self._run_main(
            {"PIPELINES_API_KEY": API_KEY}, response=payload
        )
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class LockFileTest(unittest.TestCase):
    """The hash lock: exact versions, sha256 pins, no frontmatter install."""

    def test_lock_pins_langfuse_5_0_4_with_hash(self):
        with open(LOCK) as f:
            content = f.read()
        self.assertIn("langfuse==5.0.4 --hash=sha256:", content)
        self.assertIn(
            "6ab4ddcbc4b33efbd9c7f5feb405bfb0060949e7a170f7410f8fcc52984887a7",
            content,
        )

    def test_every_requirement_line_has_exact_pin_and_hash(self):
        with open(LOCK) as f:
            lines = [
                l.strip() for l in f
                if l.strip() and not l.strip().startswith("#")
            ]
        self.assertGreaterEqual(len(lines), 9)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-_.-]+==[A-Za-z0-9.]+ --hash=sha256:[0-9a-f]{64}$", line)
        names = [l.split("==")[0] for l in lines]
        self.assertIn("langfuse", names)
        self.assertIn("opentelemetry-sdk", names)
        # No unrelated heavy dependencies added (grpcio etc. must NOT appear:
        # they are already satisfied in the pinned image).
        self.assertNotIn("grpcio", names)
        self.assertNotIn("httpx", names)
        self.assertNotIn("pydantic", names)

    def test_filter_frontmatter_has_no_requirements_line(self):
        with open(FILTER) as f:
            header = f.read(2000)
        self.assertNotIn("requirements:", header)

    def test_filter_declares_managed_valve_constants(self):
        with open(FILTER) as f:
            content = f.read()
        self.assertIn(
            'MANAGED_VALVE_KEYS = ("pipelines", "priority", "debug", "host", "secret_key", "public_key")',
            content,
        )
        self.assertIn("AUTH_TOTAL_BUDGET_SECONDS = 300", content)
        # Env-only credential sources; no personal-cloud fallback defaults.
        self.assertNotIn("cloud.langfuse.com", content)
        self.assertNotIn("your-secret-key-here", content)


@unittest.skipUnless(HAVE_YAML, "PyYAML not available")
class PipelinesManifestTest(unittest.TestCase):
    """Static assertions on install/pipelines.yaml + kustomization layout."""

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST) as f:
            cls.docs = [d for d in yaml.safe_load_all(f) if d]
        cls.deployment = [d for d in cls.docs if d["kind"] == "Deployment"][0]
        with open(KUSTOMIZATION) as f:
            cls.kustomization = yaml.safe_load(f)
        with open(os.path.join(MODULE, "pipelines", "kustomization.yaml")) as f:
            cls.pipelines_kustomization = yaml.safe_load(f)

    def test_image_pinned_to_frozen_digest(self):
        pod = self.deployment["spec"]["template"]["spec"]
        for container in pod["initContainers"] + pod["containers"]:
            self.assertEqual(
                container["image"],
                "ghcr.io/open-webui/pipelines@sha256:"
                "b48e9bc338ce2be0acfbeff01810db72408a12f07739f9e3879c1f2b00952d6e",
            )

    def test_pipelines_urls_removed(self):
        env = self.deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        self.assertNotIn("PIPELINES_URLS", names)

    def test_requirements_path_points_at_lock(self):
        env = self.deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        lock_env = [e for e in env if e["name"] == "PIPELINES_REQUIREMENTS_PATH"]
        self.assertEqual(len(lock_env), 1)
        self.assertEqual(lock_env[0]["value"], "/managed/requirements.lock")

    def test_pipelines_api_key_from_task2_secret(self):
        env = self.deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        key_env = [e for e in env if e["name"] == "PIPELINES_API_KEY"]
        self.assertEqual(len(key_env), 1)
        ref = key_env[0]["valueFrom"]["secretKeyRef"]
        self.assertEqual(ref["name"], "openwebui-connections-secret")
        self.assertEqual(ref["key"], "PIPELINES_API_KEY")

    def test_init_container_copies_only_managed_filter(self):
        init = self.deployment["spec"]["template"]["spec"]["initContainers"][0]
        self.assertEqual(init["name"], "install-managed-filter")
        script = init["command"][-1]
        self.assertIn("langfuse_v4_filter_pipeline.py", script)
        # Copies exactly one managed file; never deletes/resets the PVC.
        self.assertNotIn("rm ", script)
        self.assertNotIn("RESET_PIPELINES_DIR", script)
        self.assertEqual(script.count("cp -f"), 1)

    def test_readiness_probe_runs_env_key_checker(self):
        probe = self.deployment["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertEqual(probe["exec"]["command"],
                         ["python3", "/managed/check_ready.py"])
        # Bounded probe timeout.
        self.assertLessEqual(probe["timeoutSeconds"], 15)

    def test_startup_probe_accommodates_install_and_auth(self):
        # startupProbe covers pip install + bounded auth (300s budget).
        probe = self.deployment["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
        self.assertEqual(probe["exec"]["command"],
                         ["python3", "/managed/check_ready.py"])
        budget = probe["periodSeconds"] * probe["failureThreshold"]
        self.assertGreaterEqual(budget, 300 + 60)

    def test_no_liveness_probe(self):
        # An external Langfuse outage must NOT restart the container
        # unexpectedly: no liveness probe is defined.
        self.assertNotIn("livenessProbe",
                         self.deployment["spec"]["template"]["spec"]["containers"][0])

    def test_pvc_preserved(self):
        pvc = [d for d in self.docs if d["kind"] == "PersistentVolumeClaim"][0]
        self.assertEqual(pvc["metadata"]["name"], "pipelines-data")
        volumes = self.deployment["spec"]["template"]["spec"]["volumes"]
        data = [v for v in volumes if v["name"] == "data"][0]
        self.assertEqual(data["persistentVolumeClaim"]["claimName"], "pipelines-data")

    def test_kustomization_generates_hash_configmap(self):
        # The generator lives in the NESTED pipelines/kustomization.yaml with
        # LOCAL file sources (standard load restrictions stay enabled); the
        # base includes it as a directory resource.
        self.assertIn("../pipelines", self.kustomization["resources"])
        gens = self.pipelines_kustomization["configMapGenerator"]
        self.assertEqual(len(gens), 1)
        gen = gens[0]
        self.assertEqual(gen["name"], "pipelines-langfuse-filter")
        self.assertEqual(gen["namespace"], "hsworkshop")
        self.assertEqual(
            gen["files"],
            ["langfuse_v4_filter_pipeline.py", "requirements.lock", "check_ready.py"],
        )
        # Every generator file exists next to the nested kustomization.
        for f in gen["files"]:
            self.assertTrue(os.path.exists(os.path.join(MODULE, "pipelines", f)), f)
        # Content hash suffix explicitly enabled.
        self.assertFalse(
            self.pipelines_kustomization["generatorOptions"]["disableNameSuffixHash"]
        )

    def test_managed_volume_is_configmap_readonly(self):
        volumes = self.deployment["spec"]["template"]["spec"]["volumes"]
        managed = [v for v in volumes if v["name"] == "managed-filter"][0]
        self.assertEqual(managed["configMap"]["name"], "pipelines-langfuse-filter")
        pod = self.deployment["spec"]["template"]["spec"]
        for container in pod["initContainers"] + pod["containers"]:
            mounts = [m for m in container["volumeMounts"] if m["name"] == "managed-filter"]
            self.assertTrue(all(m.get("readOnly") for m in mounts))

    def test_no_plaintext_secrets(self):
        with open(MANIFEST) as f:
            text = f.read()
        self.assertNotRegex(text, r"(?i)api[_-]?key:\s*[A-Za-z0-9]{16,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
