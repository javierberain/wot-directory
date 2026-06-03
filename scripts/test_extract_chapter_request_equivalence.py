#!/usr/bin/env python3
"""Offline checks for the --batch path of extract_chapter.py.

These tests make NO network/API calls and do NOT compare model responses (the
model samples; response-level equality is not a valid check). They use fake
in-memory client objects that record what would be sent.

Run with the project venv:
    .venv/Scripts/python.exe scripts/test_extract_chapter_request_equivalence.py

Covers:
  1. Request equivalence (acceptance criterion 1): the params dict fed to the
     batch request is equal to the kwargs the synchronous path would send. Both
     come from build_request_params.
  2. Success path through _call_api_batch returns the result Message.
  3. Errored-path access chain: walks entry.result.error.error.{type,message}
     and asserts the raise carries the custom_id and the inner message, so a
     wrong attribute chain fails loudly and immediately.
"""
import extract_chapter as ec


# --------------------------------------------------------------------------
# Fakes. None of these touch the network.
# --------------------------------------------------------------------------
class _RecordingMessages:
    """Stands in for client.messages; records the create(**kwargs) call."""
    def __init__(self, batches=None):
        self.recorded_create_kwargs = None
        self.batches = batches

    def create(self, **kwargs):
        self.recorded_create_kwargs = kwargs
        return "SYNC_MESSAGE_SENTINEL"


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


class _FakeBatch:
    def __init__(self, batch_id, status, counts="counts(stub)"):
        self.id = batch_id
        self.processing_status = status
        self.request_counts = counts


class _FakeEntry:
    def __init__(self, custom_id, result):
        self.custom_id = custom_id
        self.result = result


class _Attr:
    """Tiny attribute bag for hand-building result/error shapes."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeBatches:
    """Stands in for client.messages.batches. Records the submitted params and
    serves a scripted lifecycle: create -> (already 'ended') -> results."""
    def __init__(self, results_entries):
        self.recorded_params = None
        self.recorded_custom_id = None
        self._results_entries = results_entries
        self.retrieve_calls = 0

    def create(self, requests):
        assert len(requests) == 1, "single-chapter path submits one request"
        self.recorded_custom_id = requests[0]["custom_id"]
        self.recorded_params = requests[0]["params"]
        return _FakeBatch("msgbatch_TEST", "ended")

    def retrieve(self, batch_id):
        # Status is already 'ended' from create, so _call_api_batch's poll loop
        # never calls this. Defined for completeness.
        self.retrieve_calls += 1
        return _FakeBatch(batch_id, "ended")

    def results(self, batch_id):
        return iter(self._results_entries)


def _sample_params():
    tool = ec.build_tool()
    user_msg = "BOOK: Test\nCHAPTER 0: Prologue\n\n=== CHAPTER TEXT ===\nhi"
    params = ec.build_request_params(tool, user_msg)
    return tool, user_msg, params


# --------------------------------------------------------------------------
# 1. Request equivalence: sync kwargs == batch params, both from the builder.
# --------------------------------------------------------------------------
def test_request_equivalence():
    tool, user_msg, params = _sample_params()
    custom_id = "b5_c0"

    # Sync path records the kwargs it would pass to messages.create.
    sync_msgs = _RecordingMessages()
    sync_client = _FakeClient(sync_msgs)
    ec._call_api(sync_client, params)
    sync_kwargs = sync_msgs.recorded_create_kwargs
    assert sync_kwargs == params, "sync path must pass build_request_params verbatim"

    # Batch path records requests[0]["params"].
    succeeded = _FakeEntry(custom_id,
                           _Attr(type="succeeded", message=_Attr(id="msg_1")))
    fake_batches = _FakeBatches([succeeded])
    batch_client = _FakeClient(_RecordingMessages(batches=fake_batches))
    ec._call_api_batch(batch_client, params, custom_id, poll_interval=0)
    batch_params = fake_batches.recorded_params
    assert fake_batches.recorded_custom_id == custom_id

    # The key check: the two paths send the same request, byte-for-byte.
    assert sync_kwargs == batch_params, "sync and batch must send identical requests"
    assert batch_params == params
    # Spot-check the cached system block survived into both.
    assert batch_params["system"][0]["cache_control"] == {"type": "ephemeral"}
    print("ok: request equivalence (sync kwargs == batch params == builder)")


# --------------------------------------------------------------------------
# 2. Success path returns the result Message (and carries the batch id).
# --------------------------------------------------------------------------
def test_batch_success_returns_message():
    _, _, params = _sample_params()
    custom_id = "b5_c0"
    msg = _Attr(id="msg_42")
    succeeded = _FakeEntry(custom_id, _Attr(type="succeeded", message=msg))
    fake_batches = _FakeBatches([succeeded])
    client = _FakeClient(_RecordingMessages(batches=fake_batches))

    resp = ec._call_api_batch(client, params, custom_id, poll_interval=0)
    assert resp is msg, "succeeded result must return entry.result.message"
    assert getattr(resp, "_batch_id", None) == "msgbatch_TEST"
    print("ok: batch success returns the result Message with batch id attached")


# --------------------------------------------------------------------------
# 3. Errored-path access chain: entry.result.error.error.{type,message}.
# --------------------------------------------------------------------------
def test_batch_errored_access_chain():
    _, _, params = _sample_params()
    custom_id = "b5_c0"
    inner_message = "messages.0.content: malformed tool block (SENTINEL_DETAIL)"
    # Shape mirrors anthropic ErrorResponse: .type == "error", and the
    # discriminating type/message live one level deeper on .error.
    error_response = _Attr(
        type="error",
        error=_Attr(type="invalid_request_error", message=inner_message),
    )
    errored = _FakeEntry(custom_id, _Attr(type="errored", error=error_response))
    client = _FakeClient(_RecordingMessages(batches=_FakeBatches([errored])))

    raised = None
    try:
        ec._call_api_batch(client, params, custom_id, poll_interval=0)
    except Exception as exc:  # noqa: BLE401 - we assert on the message below
        raised = exc
    assert raised is not None, "an errored result must raise"
    text = str(raised)
    assert custom_id in text, f"raise must name the custom_id; got: {text}"
    assert inner_message in text, f"raise must carry inner .message; got: {text}"
    print("ok: errored path walks .error.error.{type,message} and raises loudly")


def main():
    test_request_equivalence()
    test_batch_success_returns_message()
    test_batch_errored_access_chain()
    print("\nALL OFFLINE CHECKS PASSED")


if __name__ == "__main__":
    main()
