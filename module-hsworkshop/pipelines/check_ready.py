#!/usr/bin/env python3
"""Readiness checker for the Pipelines Langfuse filter (Task 3).

Run inside the pipelines container (same image, no extra dependencies):
    python3 /app/pipelines/managed/check_ready.py

Readiness = the Pipelines server is up AND the managed Langfuse filter is
discoverable via an AUTHENTICATED call. The API key is read from the
PIPELINES_API_KEY environment variable (never from argv, so it cannot leak
into process listings or logs).

Exit codes: 0 = ready, 1 = not ready (any reason), 2 = bad usage.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Same namespace-local service the OpenWebUI connection uses.
DEFAULT_BASE_URL = "http://localhost:9099"
EXPECTED_FILTER_NAME = "Langfuse Filter"
HTTP_TIMEOUT_SECONDS = 10


def _fail(message: str) -> int:
    # Never include the API key or response bodies in output.
    print(f"check_ready: not ready ({message})", file=sys.stderr)
    return 1


def main() -> int:
    base_url = os.environ.get("PIPELINES_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("PIPELINES_API_KEY", "")
    if not api_key:
        return _fail("PIPELINES_API_KEY environment variable is not set")

    # GET /models requires the bearer key (verified in the pinned image's
    # main.py: get_current_user Depends) and lists every loaded pipeline with
    # its type and filter valves - the authenticated discovery contract.
    request = urllib.request.Request(
        base_url + "/models",
        headers={"Authorization": "Bearer " + api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return _fail(f"HTTP {response.status} from /models")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401/403 means the key does not match the server's PIPELINES_API_KEY.
        return _fail(f"authenticated discovery refused (HTTP {exc.code})")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return _fail(f"server not reachable: {type(reason).__name__}")

    for model in payload.get("data", []):
        pipeline = model.get("pipeline", {})
        if (
            model.get("name") == EXPECTED_FILTER_NAME
            or model.get("id") == "langfuse_v4_filter_pipeline"
        ):
            if (
                pipeline.get("type") == "filter"
                and pipeline.get("pipelines") == ["*"]
            ):
                print("check_ready: ready")
                return 0
            return _fail("filter found but type/wildcard mismatch")
    return _fail("expected Langfuse filter not found in authenticated discovery")


if __name__ == "__main__":
    sys.exit(main())
