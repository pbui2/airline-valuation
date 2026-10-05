"""AC.3 DeepInfra boundary regression tests; live networking is opt-in only."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

import pytest

from airline_valuation.trial_deepinfra import (
    ALL_REFS, API_URL, MODEL, SYNTHETIC_GROUP, BoundaryError, CostLedger,
    DeepInfraAdapter, Invocation, InvocationResult, Reservation, Usage, combine_groups,
    group_invocations,
)


def _document(group, refs, *, response_id="id-1", usage=None, model=MODEL,
              finish="stop", content_override=None, message_extra=None):
    items = [{"ref": ref, "status": "unavailable", "value": "", "reason": "not verified"}
             for ref in refs]
    content = content_override if content_override is not None else json.dumps({"group": group, "items": items})
    return {"id": response_id, "model": model,
            "choices": [{"finish_reason": finish, "message": {"content": content, **(message_extra or {})}}],
            "usage": usage if usage is not None else {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}}


class FakeServer:
    """Real localhost HTTP server whose reply can be controlled per request."""
    def __init__(self, responder, *, status=200, headers=None, raw=None, delay=0):
        self.responder, self.status = responder, status
        self.headers, self.raw, self.delay = headers or {}, raw, delay
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append((self.command, self.path, dict(self.headers), data))
                if outer.delay:
                    time.sleep(outer.delay)
                request = json.loads(data)
                response = outer.raw if outer.raw is not None else json.dumps(outer.responder(request)).encode()
                self.send_response(outer.status)
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                try:
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1/openai/chat/completions"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def _adapter(endpoint, *, ledger=None, timeout=45, max_bytes=256 * 1024):
    return DeepInfraAdapter(ledger or CostLedger(), timeout_seconds=timeout,
                            max_response_bytes=max_bytes,
                            _test_endpoint=endpoint, _allow_localhost_test=True)


def _env(monkeypatch):
    monkeypatch.setenv("AIRLINE_TRIAL_ALLOW_LIVE", "1")
    monkeypatch.setenv("AIRLINE_TRIAL_ALLOW_SMOKE", "1")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "local-test-secret")


def _smoke():
    return Invocation(SYNTHETIC_GROUP,
        'Return JSON for group __ac3_synthetic_smoke__ with one unavailable item ref "AC3-SMOKE", empty-string value, and reason "synthetic probe".',
        ("AC3-SMOKE",), 128, False)


# Preserve the five focused ticket/smoke/accounting/transfer regression tests.
def test_ac3_equal_reservations_out_of_order_and_replay_rejected():
    ledger = CostLedger()
    a, b = ledger.reserve(900, 200), ledger.reserve(900, 200)
    assert isinstance(a, Reservation) and a.amount_usd == b.amount_usd and a.id != b.id
    usage = Usage(10, 20, 30, .0000042)
    ledger.settle(b, usage, True)
    ledger.settle(a, usage, True)
    assert [r["reservation_id"] for r in ledger.records] == [b.id, a.id]
    with pytest.raises(BoundaryError):
        ledger.settle(b, usage, True)
    assert len(ledger.records) == 2 and ledger.actual_usd == pytest.approx(2 * usage.cost_usd)


def test_ac3_smoke_missing_id_rejected_before_success_settlement_no_exception(monkeypatch, tmp_path):
    _env(monkeypatch)
    invocation = _smoke()
    response = _document(invocation.group, invocation.refs, response_id=None,
                         usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})
    server = FakeServer(lambda _req: response)
    with server as endpoint:
        adapter = _adapter(endpoint)
        adapter.is_test_transport = False  # Test seam only; socket remains localhost.
        adapter.smoke_path = tmp_path / "must-not-be-written.json"
        result = adapter._invoke(invocation, allow_smoke=True)
    assert not result.accepted and result.error == "smoke_response_metadata_missing"
    assert len(server.requests) == 1 and len(adapter.ledger.records) == 1
    assert adapter.ledger.records[0]["input_tokens"] == 20
    assert adapter.ledger.records[0]["output_tokens"] == 10
    assert adapter.ledger.records[0]["total_tokens"] == 30
    assert adapter.ledger.actual_usd == pytest.approx(result.usage.cost_usd)


def test_ac3_smoke_persistence_oserror_after_success_does_not_settle_twice(monkeypatch, tmp_path):
    import airline_valuation.trial_deepinfra as deepinfra
    _env(monkeypatch)
    monkeypatch.setattr(deepinfra, "_persist_smoke", lambda *_args: (_ for _ in ()).throw(OSError("write denied")))
    invocation = _smoke()
    response = _document(invocation.group, invocation.refs, response_id="smoke-id",
                         usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})
    server = FakeServer(lambda _req: response)
    with server as endpoint:
        adapter = _adapter(endpoint)
        adapter.is_test_transport = False
        adapter.smoke_path = tmp_path / "smoke.json"
        result = adapter._invoke(invocation, allow_smoke=True)
    assert not result.accepted and result.error == "smoke_evidence_persistence_failed"
    assert len(adapter.ledger.records) == 1 and adapter.ledger.records[0]["total_tokens"] == 30
    assert adapter.ledger.actual_usd == pytest.approx(result.usage.cost_usd)


def test_ac3_missing_usage_total_is_unknown_none_and_keeps_full_reservation(monkeypatch):
    _env(monkeypatch)
    group, refs = "group", (ALL_REFS[0],)
    server = FakeServer(lambda _req: _document(group, refs, usage={"prompt_tokens": 5, "completion_tokens": 4}))
    with server as endpoint:
        adapter = _adapter(endpoint)
        result = adapter.invoke(Invocation(group, "synthetic", refs))
    assert not result.accepted and result.usage.scope == "provider_reported_partial"
    assert result.usage.total_tokens is None
    record = adapter.ledger.records[0]
    assert record["usage_status"] == "provider_reported_partial" and record["total_tokens"] is None
    assert adapter.ledger.reserved_usd == result.reserved_usd


def test_ac3_filing_transfer_remains_fail_closed(monkeypatch):
    DeepInfraAdapter._production_ledger = None
    adapter = DeepInfraAdapter(CostLedger())
    monkeypatch.setenv("AIRLINE_TRIAL_ALLOW_LIVE", "1")
    assert adapter.invoke(Invocation("group", "filing", (ALL_REFS[0],), filing_text=True)).error == "trusted_transfer_api_unavailable"


# Restored named AC.3 coverage.
def test_deepinfra_transport_tool_free_request(monkeypatch):
    _env(monkeypatch)
    invocation = Invocation("transport-test", "synthetic only", (ALL_REFS[0],), max_tokens=77)
    server = FakeServer(lambda _req: _document(invocation.group, invocation.refs))
    with server as endpoint:
        adapter = _adapter(endpoint)
        result = adapter.invoke(invocation)
    assert result.accepted and len(server.requests) == 1
    method, path, headers, raw = server.requests[0]
    payload = json.loads(raw)
    assert (method, path) == ("POST", "/v1/openai/chat/completions")
    assert headers["Authorization"] == "Bearer local-test-secret"
    assert payload["model"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert payload["tool_choice"] == "none" and payload["stream"] is False
    assert payload["max_tokens"] == 77
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert "tools" not in payload and "functions" not in payload
    assert MODEL == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert API_URL == "https://api.deepinfra.com/v1/openai/chat/completions"


def test_deepinfra_all_67_schema_groups(monkeypatch):
    _env(monkeypatch)
    groups = group_invocations(lambda name, refs: f"synthetic group {name} refs {','.join(refs)}",
                               max_refs=8, max_tokens=128)
    assert len(ALL_REFS) == 67 and len(groups) == 9
    assert tuple(ref for group in groups for ref in group.refs) == ALL_REFS
    results = []
    request_count = 0
    for group in groups:
        server = FakeServer(lambda _req, g=group: _document(g.group, g.refs))
        with server as endpoint:
            adapter = _adapter(endpoint)
            result = adapter.invoke(group)
        request_count += len(server.requests)
        assert result.accepted and set(result.value) == set(group.refs)
        assert len(server.requests) == 1
        results.append(result)
    assert request_count == 9
    assert set(combine_groups(groups, tuple(results))) == set(ALL_REFS)
    failed = list(results)
    failed[3] = InvocationResult(False, None, None, "unknown", "failed_group", 0, 0)
    with pytest.raises(BoundaryError):
        combine_groups(groups, tuple(failed))


def test_deepinfra_rejection_redaction_and_destination(monkeypatch):
    _env(monkeypatch)
    inv = Invocation("reject", "synthetic", (ALL_REFS[0],))
    def bad_doc(**kwargs):
        return _document(inv.group, inv.refs, **kwargs)
    cases = [
        bad_doc(model="wrong-model"),
        bad_doc(content_override="not-json"),
        bad_doc(finish="length"),
        bad_doc(message_extra={"refusal": "private secret"}),
        bad_doc(message_extra={"tool_calls": []}),
        bad_doc(content_override=json.dumps({"group": inv.group, "items": []})),
        bad_doc(content_override=json.dumps({"group": inv.group, "items": [
            {"ref": inv.refs[0], "status": "unavailable", "value": "", "reason": "one"},
            {"ref": inv.refs[0], "status": "unavailable", "value": "", "reason": "duplicate"}]})),
        bad_doc(content_override=json.dumps({"group": inv.group, "items": [
            {"ref": inv.refs[0], "status": "available", "value": "NaN", "reason": ""}]})),
    ]
    for document in cases:
        server = FakeServer(lambda _req, d=document: d)
        with server as endpoint:
            result = _adapter(endpoint).invoke(inv)
        assert not result.accepted and "private secret" not in (result.error or "")
        assert len(server.requests) == 1
    with pytest.raises(ValueError):
        DeepInfraAdapter(CostLedger(), _test_endpoint="https://attacker.invalid/v1/openai/chat/completions", _allow_localhost_test=True)
    assert "local-test-secret" not in repr(result)


def test_deepinfra_usage_and_unknown_reservation(monkeypatch):
    _env(monkeypatch)
    inv = Invocation("usage", "synthetic", (ALL_REFS[0],))
    server = FakeServer(lambda _req: _document(inv.group, inv.refs,
        usage={"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10}))
    with server as endpoint:
        adapter = _adapter(endpoint); result = adapter.invoke(inv)
    assert result.accepted and result.usage.input_tokens == 6 and result.usage.total_tokens == 10
    assert adapter.ledger.records[0]["actual_usd"] == pytest.approx(result.usage.cost_usd)

    partial = FakeServer(lambda _req: _document(inv.group, inv.refs,
        usage={"prompt_tokens": 6, "completion_tokens": 4}))
    with partial as endpoint:
        unknown_adapter = _adapter(endpoint); unknown = unknown_adapter.invoke(inv)
    assert not unknown.accepted and unknown.usage.total_tokens is None
    assert unknown.usage_status == "provider_reported_partial"
    assert unknown_adapter.ledger.records[0]["total_tokens"] is None
    assert unknown_adapter.ledger.reserved_usd == unknown.reserved_usd


def test_deepinfra_cost_admission_and_no_retry(monkeypatch):
    _env(monkeypatch)
    inv = Invocation("budget", "small", (ALL_REFS[0],), max_tokens=4000)
    from airline_valuation.trial_deepinfra import _request_body
    estimated = ((len(_request_body(inv)) + 512) * .06 + 4000 * .18) / 1_000_000
    ledger = CostLedger()
    ledger.reserved_usd = .50 - estimated / 2
    server = FakeServer(lambda _req: _document(inv.group, inv.refs))
    with server as endpoint:
        result = _adapter(endpoint, ledger=ledger).invoke(inv)
    assert result.error == "insufficient_budget_for_full_reservation" and server.requests == []

    for status in (302, 429, 500, 503):
        failed = FakeServer(lambda _req: _document(inv.group, inv.refs), status=status,
                            headers={"Location": "http://127.0.0.1:1/redirect"})
        with failed as endpoint:
            failed_result = _adapter(endpoint).invoke(inv)
        assert not failed_result.accepted and failed_result.error == "provider_http_error"
        assert len(failed.requests) == 1


def test_deepinfra_over_reservation_disables_run_and_records_actual_usage(monkeypatch):
    _env(monkeypatch)
    from airline_valuation.trial_deepinfra import _body_input_bound, _request_body
    inv = Invocation("over", "x", (ALL_REFS[0],), max_tokens=100)
    over_input = _body_input_bound(len(_request_body(inv))) + 1
    doc = _document(inv.group, inv.refs, usage={"prompt_tokens": over_input,
        "completion_tokens": 3, "total_tokens": over_input + 3})
    server = FakeServer(lambda _req: doc)
    with server as endpoint:
        adapter = _adapter(endpoint); result = adapter.invoke(inv)
    assert not result.accepted and result.error == "reported_usage_exceeds_reservation_bound"
    assert adapter.ledger.halted and adapter.ledger.records[0]["input_tokens"] == over_input
    assert adapter.ledger.actual_usd == pytest.approx(result.usage.cost_usd)
    with pytest.raises(BoundaryError): adapter.ledger.reserve(1, 1)


def test_deepinfra_filing_transfer_prerequisites(monkeypatch, tmp_path):
    monkeypatch.delenv("AIRLINE_TRIAL_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("AIRLINE_TRIAL_ALLOW_FILING", raising=False)
    DeepInfraAdapter._production_ledger = None
    production = DeepInfraAdapter(CostLedger(), smoke_path=tmp_path / "smoke.json")
    filing = Invocation("filing", "do not send", (ALL_REFS[0],), filing_text=True)
    assert production.invoke(filing).error == "trusted_transfer_api_unavailable"
    monkeypatch.setenv("AIRLINE_TRIAL_ALLOW_LIVE", "1")
    monkeypatch.setenv("AIRLINE_TRIAL_ALLOW_FILING", "1")
    # Even fresh/stale/local smoke markers cannot authorize an unsupported transfer.
    (tmp_path / "smoke.json").write_text(json.dumps({"status": "success", "model": MODEL}))
    assert production.invoke(filing).error == "trusted_transfer_api_unavailable"
    with pytest.raises(BoundaryError, match="independent_transfer_authorization_unavailable"):
        from airline_valuation.trial_deepinfra import authorize_transfer
        authorize_transfer(tmp_path / "smoke.json", filing_text=True)
    assert production.ledger.calls == 0


@pytest.mark.skipif(__import__("os").environ.get("AIRLINE_TRIAL_LIVE_SMOKE") != "1",
                    reason="explicit live smoke opt-in required")
def test_deepinfra_live_synthetic_smoke():
    pytest.skip("live request intentionally not run in automated AC.3 tests")
