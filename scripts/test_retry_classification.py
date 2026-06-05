#!/usr/bin/env python3
"""Offline unit tests for extract_chapter.py's billing-safe retry classifier.

NO network/API calls. Verifies that a read/response timeout (which may mean a
completed, BILLED call) is NEVER retried for the non-idempotent messages.create
path, while errors that definitely did not reach the model (429/529, connection-
establishment failures) are retried within a tight cap.

Run with the project venv from the scripts/ dir:
    ../.venv/Scripts/python.exe test_retry_classification.py
"""
import anthropic
import httpx

import extract_chapter as ec

# Never actually sleep during the backoff between retries.
ec.time.sleep = lambda *a, **k: None

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _timeout():
    return anthropic.APITimeoutError(request=REQ)


def _conn_establishment():
    e = anthropic.APIConnectionError(message="connect refused", request=REQ)
    e.__cause__ = httpx.ConnectError("Connection refused")
    return e


def _conn_after_send():
    e = anthropic.APIConnectionError(message="read error", request=REQ)
    e.__cause__ = httpx.ReadError("peer reset mid-stream")
    return e


def _status(code):
    return anthropic.APIStatusError(
        f"HTTP {code}", response=httpx.Response(code, request=REQ), body=None)


def _counter(make_exc, succeed_at=None):
    """Return (fn, calls) where fn raises make_exc() until call #succeed_at."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if succeed_at is not None and calls["n"] >= succeed_at:
            return "OK"
        raise make_exc()
    return fn, calls


def test_timeout_not_retried():
    """A read/response timeout on the non-idempotent path must NOT retry."""
    fn, calls = _counter(_timeout)
    raised = None
    try:
        ec._with_retries(fn)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, RuntimeError), f"expected RuntimeError, got {raised!r}"
    assert "BILLED" in str(raised), "must warn the call may have been billed"
    assert calls["n"] == 1, f"timeout must fire exactly once, fired {calls['n']}x"
    print("ok: APITimeoutError is NOT retried (fired once, surfaced loudly)")


def test_post_send_conn_error_not_retried():
    """A transport error AFTER the request was sent has unknown outcome -> stop."""
    fn, calls = _counter(_conn_after_send)
    raised = None
    try:
        ec._with_retries(fn)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, RuntimeError) and "BILLED" in str(raised)
    assert calls["n"] == 1, f"post-send error must not retry, fired {calls['n']}x"
    print("ok: post-send connection error is NOT retried")


def test_connect_establishment_retried():
    """A connection-establishment failure never reached the model -> safe retry."""
    fn, calls = _counter(_conn_establishment, succeed_at=3)
    assert ec._with_retries(fn) == "OK"
    assert calls["n"] == 3, f"should retry up to the cap, fired {calls['n']}x"
    print("ok: connection-establishment error IS retried within the cap")


def test_connect_establishment_capped():
    """Connection-establishment retries are tightly capped, then surface."""
    fn, calls = _counter(_conn_establishment)  # never succeeds
    raised = None
    try:
        ec._with_retries(fn)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, anthropic.APIConnectionError)
    assert calls["n"] == ec._API_MAX_ATTEMPTS, \
        f"cap is {ec._API_MAX_ATTEMPTS}, fired {calls['n']}x"
    print(f"ok: connection retries capped at {ec._API_MAX_ATTEMPTS}")


def test_429_retried():
    """429/529 = request rejected, not billed -> safe to retry."""
    fn, calls = _counter(lambda: _status(429), succeed_at=2)
    assert ec._with_retries(fn) == "OK"
    assert calls["n"] == 2
    print("ok: HTTP 429 IS retried (rejected, not billed)")


def test_non_retryable_status_raises():
    """A 400 is not retryable and must surface immediately."""
    fn, calls = _counter(lambda: _status(400))
    raised = None
    try:
        ec._with_retries(fn)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, anthropic.APIStatusError)
    assert calls["n"] == 1
    print("ok: non-retryable HTTP 400 surfaces immediately (no retry)")


def test_idempotent_timeout_retried():
    """For an idempotent GET (batch poll), a timeout IS safe to retry."""
    fn, calls = _counter(_timeout, succeed_at=2)
    assert ec._with_retries(fn, idempotent=True) == "OK"
    assert calls["n"] == 2
    print("ok: idempotent=True retries timeouts (batch poll path)")


def test_client_config():
    """SDK auto-retry disabled and a generous read timeout configured."""
    assert ec._SDK_MAX_RETRIES == 0, "SDK auto-retry must be disabled"
    assert ec._REQUEST_TIMEOUT.read == 600.0, "read timeout should be 10 minutes"
    print(f"ok: SDK max_retries={ec._SDK_MAX_RETRIES}, "
          f"read timeout={ec._REQUEST_TIMEOUT.read}s")


def main():
    test_timeout_not_retried()
    test_post_send_conn_error_not_retried()
    test_connect_establishment_retried()
    test_connect_establishment_capped()
    test_429_retried()
    test_non_retryable_status_raises()
    test_idempotent_timeout_retried()
    test_client_config()
    print("\nALL RETRY-CLASSIFICATION CHECKS PASSED")


if __name__ == "__main__":
    main()
