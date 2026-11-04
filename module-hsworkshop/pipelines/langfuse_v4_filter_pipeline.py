"""
title: Langfuse Filter Pipeline for v4
author: kpiwko (based on open-webui's langfuse_v3_filter_pipeline.py)
date: 2026-10-29
version: 1.1.0
license: MIT
description: A filter pipeline that uses Langfuse v4 (Python SDK v5, observations-first data model).

Dependencies are NOT installed from this frontmatter: the exact hash-checked
lock (langfuse 5.0.4 + OpenTelemetry closure) is installed at container
startup by start.sh via PIPELINES_REQUIREMENTS_PATH -> pipelines/requirements.lock
(mounted from the pipelines-langfuse-filter ConfigMap).
"""

from typing import List, Optional
import os
import time
import uuid

from utils.pipelines.main import get_last_assistant_message
from pydantic import BaseModel
from langfuse import Langfuse, propagate_attributes

# Managed valves: after the persisted valves.json overlay is applied by the
# Pipelines server (persisted values override code defaults), these keys are
# re-asserted so Git/Secret-owned configuration always wins. The overlay keeps
# every other persisted key untouched.
MANAGED_VALVE_KEYS = ("pipelines", "priority", "debug", "host", "secret_key", "public_key")
# Env-var sources for the managed host/credentials: pod environment is
# Secret-backed (guardrails-langfuse-secret), so re-asserting after every
# valves overlay guarantees Git/Secret-owned values win over any persisted
# stale copies. Missing credentials are a hard startup failure (no fallback
# to personal cloud defaults).
LANGFUSE_HOST_ENV = "LANGFUSE_HOST"
LANGFUSE_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"
LANGFUSE_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
# Bounded startup authentication: retry transients until the total budget is
# exhausted; invalid credentials fail immediately (no retry). Per-call
# timeouts are capped by the time REMAINING in the budget, never beyond it.
AUTH_TOTAL_BUDGET_SECONDS = 300
AUTH_PER_CALL_TIMEOUT_SECONDS = 30
AUTH_RETRY_DELAY_SECONDS = 10


class InvalidCredentialsError(Exception):
    """Langfuse rejected the credentials at startup."""


class StartupTimeoutError(Exception):
    """Langfuse auth_check did not succeed within the startup budget."""


class MissingEnvCredentialsError(Exception):
    """Required Secret-backed Langfuse env credentials are not set."""


def get_last_assistant_message_obj(messages: List[dict]) -> dict:
    """Retrieve the last assistant message from the message list."""
    for message in reversed(messages):
        if message["role"] == "assistant":
            return message
    return {}


class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = []
        priority: int = 0
        secret_key: str
        public_key: str
        host: str
        # New valve that controls whether task names are added as tags:
        insert_tags: bool = True
        # New valve that controls whether to use model name instead of model ID for generation
        use_model_name_instead_of_id_for_generation: bool = False
        debug: bool = False

    def __init__(self):
        self.type = "filter"
        self.name = "Langfuse Filter"

        # Env-only credential reads (no personal-cloud fallback defaults):
        # _assert_managed_valves enforces presence and re-asserts these after
        # every persisted-valves overlay. Missing values raise fail-closed.
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "secret_key": os.getenv(LANGFUSE_SECRET_KEY_ENV, ""),
                "public_key": os.getenv(LANGFUSE_PUBLIC_KEY_ENV, ""),
                "host": os.getenv(LANGFUSE_HOST_ENV, ""),
                "use_model_name_instead_of_id_for_generation": os.getenv("USE_MODEL_NAME", "false").lower() == "true",
                "debug": os.getenv("DEBUG_MODE", "false").lower() == "true",
            }
        )
        self._assert_managed_valves()

        self.langfuse = None
        self.chat_traces = {}
        self.suppressed_logs = set()
        # Dictionary to store model names for each chat
        self.model_names = {}

    def _assert_managed_valves(self):
        """Re-assert Git/Secret-owned managed valve keys after any overlay.

        The Pipelines server merges persisted valves.json over the code
        defaults at load time and on every valves update. The managed keys
        below must always return to their declared values so tracing targets
        every model at the normal priority without debug logging, and the
        host/credentials come from the pod environment (Secret-backed), never
        from a stale persisted copy. Missing env credentials raise
        immediately (fail-closed: no default personal-cloud fallback).
        """
        self.valves.pipelines = ["*"]
        self.valves.priority = 0
        self.valves.debug = False

        host = os.getenv(LANGFUSE_HOST_ENV)
        secret_key = os.getenv(LANGFUSE_SECRET_KEY_ENV)
        public_key = os.getenv(LANGFUSE_PUBLIC_KEY_ENV)
        missing = [
            name
            for name, value in (
                (LANGFUSE_HOST_ENV, host),
                (LANGFUSE_SECRET_KEY_ENV, secret_key),
                (LANGFUSE_PUBLIC_KEY_ENV, public_key),
            )
            if not value
        ]
        if missing:
            # Fail the process: without Secret-backed credentials the filter
            # must not start (and must never fall back to the personal cloud
            # default host with placeholder keys).
            print(
                "[langfuse-filter] startup FAILED: required environment "
                f"variables not set: {', '.join(missing)}. Refusing to start "
                "without Langfuse credentials.",
                flush=True,
            )
            raise MissingEnvCredentialsError(
                f"missing required env: {', '.join(missing)}"
            )
        self.valves.host = host
        self.valves.secret_key = secret_key
        self.valves.public_key = public_key

    def log(self, message: str, suppress_repeats: bool = False):
        if self.valves.debug:
            if suppress_repeats:
                if message in self.suppressed_logs:
                    return
                self.suppressed_logs.add(message)
            print(f"[DEBUG] {message}")

    async def on_startup(self):
        self.log(f"on_startup triggered for {__name__}")
        self._assert_managed_valves()
        # Startup authentication is REQUIRED: auth_check() must return True
        # within the total budget or the process fails startup (an exception
        # raised here propagates through the Pipelines lifespan handler and
        # crashes the pod). Invalid credentials raise immediately.
        self._startup_authenticate()

    async def on_shutdown(self):
        self.log(f"on_shutdown triggered for {__name__}")
        if self.langfuse:
            try:
                # End all active traces
                for chat_id, trace in self.chat_traces.items():
                    try:
                        trace.end()
                        self.log(f"Ended trace for chat_id: {chat_id}")
                    except Exception as e:
                        self.log(f"Failed to end trace for chat_id: {chat_id}: {e}")

                self.chat_traces.clear()
                self.langfuse.shutdown()
                self.log("Langfuse data flushed on shutdown")
                self.langfuse = None
            except Exception as e:
                self.log(f"Failed to flush Langfuse: {e}")

    async def on_valves_updated(self):
        self.log("Valves updated, resetting Langfuse client.")
        # Re-assert managed keys BEFORE using any valve value: the update may
        # have overlaid them with persisted values.
        self._assert_managed_valves()
        self.set_langfuse()

    @staticmethod
    def _is_invalid_credentials_error(error: Exception) -> bool:
        """Exact typed classification: only HTTP 401/403 mean bad credentials.

        The Langfuse SDK (5.0.4, Fern-generated) raises typed ApiError
        instances carrying an integer status_code. A 503 whose body or headers
        happen to contain the substrings 401/403 must NOT be misclassified as
        an invalid-credential failure (it is transient). Untyped errors are
        conservatively treated as transient (retried within the budget).
        """
        status_code = getattr(error, "status_code", None)
        return isinstance(status_code, int) and status_code in (401, 403)

    def _startup_authenticate(self):
        """Bounded, blocking startup authentication.

        Uses the pinned SDK's own request mechanism (verified in the 5.0.4
        wheel: `client.api.projects.get(request_options=...)`, the exact call
        `auth_check()` makes) with EXPLICIT per-request bounds:
        `timeout_in_seconds` capped to the remaining budget and
        `max_retries=0` so Fern's internal retries (default 2, honoring
        Retry-After up to 60s) cannot overshoot the total deadline.

        auth_check()-equivalent semantics per the 5.0.4 source: success means
        the projects call returned AND at least one project exists for the
        keys.

        Client lifecycle (5.0.4 source): LangfuseResourceManager is a
        SINGLETON keyed by public_key (`__new__` returns the registered
        instance and ignores later constructor args), and `shutdown()` sets
        `_shutdown=True` WITHOUT unregistering from `_instances`. Therefore
        exactly ONE client is created BEFORE the retry loop and reused for
        every attempt; `shutdown()` is called ONLY on terminal failure (typed
        401/403, empty project list, exhausted deadline) — never on a
        transient retry, which would poison the singleton for every later
        `Langfuse(...)` in this process. Normal process shutdown goes through
        `on_shutdown()`.

        - Transient failures (network, timeouts, 5xx) are retried until the
          AUTH_TOTAL_BUDGET_SECONDS budget is exhausted.
        - Invalid credentials (typed 401/403 ApiError) raise
          InvalidCredentialsError immediately.
        - Error messages never include key material or raw error bodies
          (only the host and the exception class name).
        """
        deadline = time.monotonic() + AUTH_TOTAL_BUDGET_SECONDS
        client = Langfuse(
            secret_key=self.valves.secret_key,
            public_key=self.valves.public_key,
            host=self.valves.host,
        )
        released = False

        def _release():
            # Terminal failure only: release the public_key singleton
            # exactly once (idempotent guard against the outer handler).
            nonlocal released
            if not released:
                released = True
                try:
                    client.shutdown()
                except Exception:
                    pass

        attempt = 0
        last_error = None
        try:
            while True:
                attempt += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                per_call_timeout = min(AUTH_PER_CALL_TIMEOUT_SECONDS, max(1, int(remaining)))
                try:
                    projects = client.api.projects.get(
                        request_options={
                            "timeout_in_seconds": per_call_timeout,
                            "max_retries": 0,
                        }
                    )
                except Exception as e:
                    if self._is_invalid_credentials_error(e):
                        # Fail the process: invalid credentials must not be
                        # retried and must not leave a fail-open client
                        # running. Terminal failure: release the singleton.
                        _release()
                        print(
                            "[langfuse-filter] startup FAILED: Langfuse rejected the "
                            "credentials (invalid public/secret key for host "
                            f"{self.valves.host}). Fix the Langfuse keys; refusing to "
                            "start with tracing disabled.",
                            flush=True,
                        )
                        raise InvalidCredentialsError(
                            "Langfuse auth_check failed with invalid credentials"
                        ) from e
                    # Transient: record only the exception class name (raw
                    # bodies may echo request content; never log them). The
                    # client is KEPT alive for the next attempt (shutting it
                    # down would poison the public_key singleton).
                    last_error = f"{type(e).__name__}"
                else:
                    if getattr(projects, "data", None):
                        # auth_check()-equivalent success (5.0.4 source): at
                        # least one project exists for these keys. The client
                        # is kept as the filter's runtime client.
                        self.langfuse = client
                        self.log(
                            f"Langfuse startup authentication succeeded (attempt {attempt}) "
                            f"for host: {self.valves.host}"
                        )
                        return
                    # Empty project list: same meaning as auth_check()
                    # raising "no project found for the keys provided" -
                    # credentials are valid for the server but wrong for this
                    # project. Fail closed, immediately (not transient).
                    # Terminal failure: release the singleton.
                    _release()
                    print(
                        "[langfuse-filter] startup FAILED: Langfuse found no project "
                        f"for the provided keys (host {self.valves.host}). Refusing to "
                        "start with tracing disabled.",
                        flush=True,
                    )
                    raise InvalidCredentialsError(
                        "Langfuse auth check found no project for the keys"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(AUTH_RETRY_DELAY_SECONDS, remaining))
            # Deadline exhausted: terminal failure, release the singleton.
            _release()
            print(
                "[langfuse-filter] startup FAILED: Langfuse auth_check did not succeed "
                f"within {AUTH_TOTAL_BUDGET_SECONDS}s (last error: {last_error}). "
                "Refusing to start with tracing disabled.",
                flush=True,
            )
            raise StartupTimeoutError(
                f"Langfuse auth_check not True within {AUTH_TOTAL_BUDGET_SECONDS}s"
            )
        except BaseException:
            # Any other terminal exit (cancellation, unexpected error) must
            # not leave a half-initialized singleton behind either - but only
            # if we still own it (not yet handed to self.langfuse).
            if self.langfuse is not client:
                _release()
            raise

    def set_langfuse(self):
        """Runtime (non-startup) client (re)initialization - fail-open.

        Used by on_valves_updated: a runtime auth failure disables the client
        (requests pass through untraced) but does not kill the process. The
        strict bounded-authentication requirement applies to startup only.
        """
        try:
            self.log(f"Initializing Langfuse with host: {self.valves.host}")
            self.log(
                f"Secret key set: {'Yes' if self.valves.secret_key else 'No'}"
            )
            self.log(
                f"Public key set: {'Yes' if self.valves.public_key else 'No'}"
            )

            # Langfuse Python SDK v5 — needs Langfuse server v4.59+
            client = Langfuse(
                secret_key=self.valves.secret_key,
                public_key=self.valves.public_key,
                host=self.valves.host,
                timeout=AUTH_PER_CALL_TIMEOUT_SECONDS,
            )

            # Test authentication
            try:
                client.auth_check()
                self.langfuse = client
                self.log(
                    f"Langfuse client initialized and authenticated successfully. Connected to host: {self.valves.host}")
            except Exception as e:
                self.log(f"Auth check failed: {e}")
                self.log(f"Failed host: {self.valves.host}")
                if self.langfuse:
                    try:
                        self.langfuse.shutdown()
                    except Exception:
                        pass
                self.langfuse = None
                return

        except Exception as e:
            self.log(f"Langfuse initialization error: {e}")
            self.langfuse = None

    def _build_tags(self, task_name: str) -> list:
        """
        Builds a list of tags based on valve settings, ensuring we always add
        'open-webui' and skip user_response / llm_response from becoming tags themselves.
        """
        tags_list = []
        if self.valves.insert_tags:
            # Always add 'open-webui'
            tags_list.append("open-webui")
            # Add the task_name if it's not one of the excluded defaults
            if task_name not in ["user_response", "llm_response"]:
                tags_list.append(task_name)
        return tags_list

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        self.log("Langfuse Filter INLET called")

        # Check Langfuse client status
        if not self.langfuse:
            self.log("[WARNING] Langfuse client not initialized - Skipped")
            return body

        self.log(f"Inlet function called with body: {body} and user: {user}")

        metadata = body.get("metadata", {})
        chat_id = metadata.get("chat_id", str(uuid.uuid4()))

        # Handle temporary chats
        if chat_id == "local":
            session_id = metadata.get("session_id")
            chat_id = f"temporary-session-{session_id}"

        metadata["chat_id"] = chat_id
        body["metadata"] = metadata

        # Extract and store both model name and ID if available
        model_info = metadata.get("model", {})

        # Store model information for this chat
        if chat_id not in self.model_names:
            self.model_names[chat_id] = {"id": body.get("model")}
        else:
            self.model_names[chat_id]["id"] = body.get("model")

        if isinstance(model_info, dict) and "name" in model_info:
            self.model_names[chat_id]["name"] = model_info["name"]
            self.log(f"Stored model info - name: '{model_info['name']}', id: '{body.get('model')}' for chat_id: {chat_id}")

        required_keys = ["model", "messages"]
        missing_keys = [key for key in required_keys if key not in body]
        if missing_keys:
            error_message = f"Error: Missing keys in the request body: {', '.join(missing_keys)}"
            self.log(error_message)
            raise ValueError(error_message)

        user_email = user.get("email") if user else None
        # Defaulting to 'user_response' if no task is provided
        task_name = metadata.get("task", "user_response")

        # Build tags
        tags_list = self._build_tags(task_name)

        if chat_id not in self.chat_traces:
            self.log(f"Creating new trace for chat_id: {chat_id}")

            try:
                # v4 observations-first model: correlating attributes propagate to
                # every observation via propagate_attributes() instead of
                # update_current_trace()/start_span(name=...).
                with propagate_attributes(
                    trace_name=f"chat:{chat_id}",
                    user_id=user_email,
                    session_id=chat_id,
                    tags=tags_list if tags_list else None,
                    metadata={k: str(v) for k, v in metadata.items()},
                ):
                    trace = self.langfuse.start_observation(
                        name=f"chat:{chat_id}",
                        as_type="span",
                        input=body,
                        metadata=metadata,
                    )

                self.chat_traces[chat_id] = trace
                self.log(f"Successfully created trace for chat_id: {chat_id}")
            except Exception as e:
                self.log(f"Failed to create trace: {e}")
                return body
        else:
            trace = self.chat_traces[chat_id]
            self.log(f"Reusing existing trace for chat_id: {chat_id}")

        # Update metadata with type
        metadata["type"] = task_name
        metadata["interface"] = "open-webui"

        # Log user input as event
        try:
            trace = self.chat_traces[chat_id]
            with propagate_attributes(
                user_id=user_email,
                session_id=chat_id,
                tags=tags_list if tags_list else None,
            ):
                event_span = trace.start_observation(
                    name=f"user_input:{str(uuid.uuid4())}",
                    as_type="span",
                    metadata=metadata,
                    input=body["messages"],
                )
                event_span.end()
            self.log(f"User input event logged for chat_id: {chat_id}")
        except Exception as e:
            self.log(f"Failed to log user input event: {e}")

        return body

    async def outlet(self, body: dict, user: Optional[dict] = None) -> dict:
        self.log("Langfuse Filter OUTLET called")

        # Check Langfuse client status
        if not self.langfuse:
            self.log("[WARNING] Langfuse client not initialized - Skipped")
            return body

        self.log(f"Outlet function called with body: {body}")

        chat_id = body.get("chat_id")

        # Handle temporary chats
        if chat_id == "local":
            session_id = body.get("session_id")
            chat_id = f"temporary-session-{session_id}"

        metadata = body.get("metadata", {})
        # Defaulting to 'llm_response' if no task is provided
        task_name = metadata.get("task", "llm_response")

        # Build tags
        tags_list = self._build_tags(task_name)

        if chat_id not in self.chat_traces:
            self.log(f"[WARNING] No matching trace found for chat_id: {chat_id}, attempting to re-register.")
            # Re-run inlet to register if somehow missing
            return await self.inlet(body, user)

        trace = self.chat_traces[chat_id]

        assistant_message = get_last_assistant_message(body["messages"])
        assistant_message_obj = get_last_assistant_message_obj(body["messages"])

        usage = None
        if assistant_message_obj:
            info = assistant_message_obj.get("usage", {})
            if isinstance(info, dict):
                input_tokens = info.get("prompt_eval_count") or info.get("prompt_tokens")
                output_tokens = info.get("eval_count") or info.get("completion_tokens")
                if input_tokens is not None and output_tokens is not None:
                    usage = {
                        "input": input_tokens,
                        "output": output_tokens,
                    }
                    self.log(f"Usage data extracted: {usage}")

        metadata["type"] = task_name
        metadata["interface"] = "open-webui"

        # v4: root observation carries the trace output
        try:
            trace.update(output=assistant_message)
        except Exception as e:
            self.log(f"Failed to update trace output: {e}")

        # Outlet: Always create LLM generation (this is the LLM response)
        # Determine which model value to use based on the use_model_name valve
        model_id = self.model_names.get(chat_id, {}).get("id", body.get("model"))
        model_name = self.model_names.get(chat_id, {}).get("name", "unknown")

        # Pick primary model identifier based on valve setting
        model_value = (
            model_name
            if self.valves.use_model_name_instead_of_id_for_generation
            else model_id
        )

        # Add both values to metadata regardless of valve setting
        metadata["model_id"] = model_id
        metadata["model_name"] = model_name

        # Create LLM generation for the response
        try:
            generation_metadata = {
                **metadata,
                "type": "llm_response",
                "model_id": model_id,
                "model_name": model_name,
                "generation_id": str(uuid.uuid4()),
            }

            with propagate_attributes(
                user_id=user.get("email") if user else None,
                session_id=chat_id,
                tags=tags_list if tags_list else None,
            ):
                generation = trace.start_observation(
                    name=f"llm_response:{str(uuid.uuid4())}",
                    as_type="generation",
                    model=model_value,
                    input=body["messages"],
                    output=assistant_message,
                    metadata=generation_metadata,
                )

                # Update with usage if available
                if usage:
                    generation.update(usage_details=usage)

                generation.end()
            self.log(f"LLM generation completed for chat_id: {chat_id}")
        except Exception as e:
            self.log(f"Failed to create LLM generation: {e}")

        # Flush data to Langfuse
        try:
            if self.langfuse:
                self.langfuse.flush()
                self.log("Langfuse data flushed")
        except Exception as e:
            self.log(f"Failed to flush Langfuse: {e}")

        return body
