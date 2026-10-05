"""Fail-closed direct DeepInfra boundary; this is not a valuation engine."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import socket
import ssl
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

API_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
SCHEMA_VERSION = "trial-deepinfra-groups/1"
POLICY_VERSION = "ac3-cost-admission-2"
INPUT_USD_PER_MILLION = 0.06
OUTPUT_USD_PER_MILLION = 0.18
DEFAULT_OUTPUT_TOKENS = 4000
MAX_OUTPUT_TOKENS = 4000
MAX_PROMPT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CALLS = 12
MAX_SECONDS = 45.0
BODY_OVERHEAD_BYTES = 512
SYNTHETIC_GROUP = "__ac3_synthetic_smoke__"


def _all_refs() -> tuple[str, ...]:
    path = Path(__file__).with_name("detailed_contract.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    refs = tuple(data["input_allowlist"])
    if len(refs) != 67 or len(set(refs)) != 67:
        raise RuntimeError("contract must enumerate exactly 67 unique input references")
    return refs


ALL_REFS = _all_refs()


class BoundaryError(RuntimeError):
    """Sanitized invocation or policy failure."""


@dataclass(frozen=True)
class Invocation:
    group: str
    prompt: str
    refs: tuple[str, ...]
    max_tokens: int = DEFAULT_OUTPUT_TOKENS
    filing_text: bool = False


@dataclass(frozen=True)
class Reservation:
    id: int
    amount_usd: float
    input_token_bound: int
    max_output_tokens: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int | None
    cost_usd: float
    scope: str = "provider_reported"
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class InvocationResult:
    accepted: bool
    value: Mapping[str, Mapping[str, Any]] | None
    usage: Usage | None
    usage_status: str
    error: str | None
    elapsed_seconds: float
    reserved_usd: float
    response_id: str | None = None
    finish_reason: str | None = None
    server_usage: Mapping[str, Any] | None = None


class Provider(Protocol):
    def invoke(self, invocation: Invocation) -> InvocationResult: ...


class CostLedger:
    """Admission uses one-token-per-encoded-byte plus fixed JSON overhead, never a tokenizer guess."""

    def __init__(self, limit_usd: float = 0.50, *, max_calls: int = MAX_CALLS) -> None:
        if not math.isfinite(limit_usd) or limit_usd <= 0 or limit_usd > 0.50 or max_calls <= 0 or max_calls > MAX_CALLS:
            raise ValueError("invalid budget")
        self.limit_usd, self.max_calls = limit_usd, max_calls
        self.reserved_usd = self.actual_usd = 0.0
        self.calls, self.halted = 0, False
        self.records: list[dict[str, Any]] = []
        self._settled_reservations: set[int] = set()
        self._next_reservation_id = 0
        self._active_reservations: dict[int, Reservation] = {}

    def reserve(self, body_bytes: int, output_tokens: int) -> Reservation:
        if self.halted or self.calls >= self.max_calls:
            raise BoundaryError("trial_halted_or_call_limit")
        # Deliberate conservative bound: each encoded body byte + 512 bytes of
        # envelope/schema overhead may account for one input token. No tokenizer
        # ceiling is claimed; if this reservation is unaffordable, do not send.
        amount = ((body_bytes + BODY_OVERHEAD_BYTES) * INPUT_USD_PER_MILLION
                  + output_tokens * OUTPUT_USD_PER_MILLION) / 1_000_000
        if self.reserved_usd + amount > self.limit_usd + 1e-12:
            raise BoundaryError("insufficient_budget_for_full_reservation")
        self.reserved_usd += amount
        self.calls += 1
        self._next_reservation_id += 1
        ticket = Reservation(self._next_reservation_id, amount, body_bytes + BODY_OVERHEAD_BYTES, output_tokens)
        self._active_reservations[ticket.id] = ticket
        return ticket

    def settle(self, reservation: Reservation, usage: Usage | None, successful: bool, *,
               response_id: str | None = None, finish_reason: str | None = None,
               elapsed_seconds: float | None = None) -> bool:
        if not isinstance(reservation, Reservation):
            raise BoundaryError("reservation_ticket_required")
        active = self._active_reservations.get(reservation.id)
        if active is not reservation or reservation.id in self._settled_reservations:
            raise BoundaryError("reservation_already_settled_or_unknown")
        self._settled_reservations.add(reservation.id)
        self._active_reservations.pop(reservation.id, None)
        self.records.append({"reservation_id": reservation.id, "reserved_usd": reservation.amount_usd, "response_id": response_id,
                             "finish_reason": finish_reason, "elapsed_seconds": elapsed_seconds,
                             "usage_status": usage.scope if usage else "unknown",
                             "input_tokens": usage.input_tokens if usage else None,
                             "output_tokens": usage.output_tokens if usage else None,
                             "total_tokens": usage.total_tokens if usage else None,
                             "cached_input_tokens": usage.cached_input_tokens if usage else None,
                             "reasoning_tokens": usage.reasoning_tokens if usage else None,
                             "actual_usd": usage.cost_usd if usage else None})
        if usage is None:
            return True
        self.actual_usd += usage.cost_usd
        if usage.cost_usd > reservation.amount_usd + 1e-12:
            self.halted = True
            return False
        if successful:
            self.reserved_usd -= reservation.amount_usd - usage.cost_usd
        return True


class DeepInfraAdapter:
    """Fixed-host HTTPS; injected localhost is isolated from smoke-proof issuance."""

    _production_ledger: CostLedger | None = None

    def __init__(self, ledger: CostLedger, *, timeout_seconds: float = MAX_SECONDS,
                 max_response_bytes: int = MAX_RESPONSE_BYTES,
                 smoke_path: str | Path | None = None,
                 _test_endpoint: str | None = None, _allow_localhost_test: bool = False) -> None:
        if not 0 < timeout_seconds <= MAX_SECONDS or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("invalid transport bounds")
        self.is_test_transport = _test_endpoint is not None
        if _test_endpoint is not None:
            parsed = urlsplit(_test_endpoint)
            if (not _allow_localhost_test or parsed.scheme != "http" or
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port or
                parsed.path != "/v1/openai/chat/completions" or parsed.query or parsed.username):
                raise ValueError("test endpoint requires explicit localhost test mode")
            self.host, self.port, self.path, self.tls = parsed.hostname, parsed.port, parsed.path, False
        else:
            self.host, self.port, self.path, self.tls = "api.deepinfra.com", 443, "/v1/openai/chat/completions", True
        if not isinstance(ledger, CostLedger):
            raise ValueError("production adapter requires a CostLedger")
        if not self.is_test_transport and (ledger.limit_usd != 0.50 or ledger.max_calls != MAX_CALLS):
            raise ValueError("production adapter requires the fixed single-run ledger cap")
        if not self.is_test_transport:
            if DeepInfraAdapter._production_ledger is None:
                DeepInfraAdapter._production_ledger = ledger
            elif DeepInfraAdapter._production_ledger is not ledger:
                raise ValueError("production single-run ledger already owned")
        self.ledger, self.timeout_seconds = ledger, timeout_seconds
        self.max_response_bytes, self.smoke_path = max_response_bytes, Path(smoke_path) if smoke_path else None

    def invoke(self, invocation: Invocation) -> InvocationResult:
        if not isinstance(invocation, Invocation):
            return _failed("invalid_invocation")
        if not self.is_test_transport and invocation.group != SYNTHETIC_GROUP:
            return _failed("trusted_transfer_api_unavailable")
        if invocation.group == SYNTHETIC_GROUP:
            if os.environ.get("AIRLINE_TRIAL_ALLOW_SMOKE") != "1":
                return _failed("smoke_opt_in_required")
            if invocation.filing_text:
                return _failed("filing_text_rejected_for_synthetic_group")
            return _failed("smoke_must_use_smoke_method")
        return self._invoke(invocation, allow_smoke=False)

    def _invoke(self, invocation: Invocation, *, allow_smoke: bool) -> InvocationResult:
        _validate_invocation(invocation)
        if invocation.group == SYNTHETIC_GROUP:
            if not allow_smoke or invocation.filing_text:
                return _failed("smoke_must_use_smoke_method")
            if os.environ.get("AIRLINE_TRIAL_ALLOW_SMOKE") != "1":
                return _failed("smoke_opt_in_required")
        if len(invocation.prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return _failed("prompt_byte_limit")
        if invocation.filing_text:
            return _failed("trusted_transfer_api_unavailable")
        if os.environ.get("AIRLINE_TRIAL_ALLOW_LIVE") != "1":
            return _failed("live_opt_in_required")
        if invocation.group == SYNTHETIC_GROUP and self.is_test_transport:
            return _failed("production_smoke_required")
        body = _request_body(invocation)
        try:
            reservation = self.ledger.reserve(len(body), invocation.max_tokens)
        except BoundaryError as exc:
            return _failed(str(exc))
        started = time.monotonic()
        token = os.environ.get("DEEPINFRA_API_KEY", "")
        if not token or len(token) > 4096 or any(c in token for c in "\r\n"):
            self.ledger.settle(reservation, None, False)
            return _failed("credential_unavailable", reservation.amount_usd, started)
        def settle_call(observed: Usage | None, successful: bool) -> bool:
            return self.ledger.settle(reservation, observed, successful, response_id=response_id,
                                      finish_reason=finish_reason, elapsed_seconds=time.monotonic()-started)

        conn: http.client.HTTPConnection | None = None
        usage: Usage | None = None
        response_id: str | None = None
        finish_reason: str | None = None
        try:
            conn = (http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout_seconds,
                                                context=ssl.create_default_context()) if self.tls else
                    http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_seconds))
            conn.request("POST", self.path, body=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}",
                "Accept": "application/json", "Connection": "close"})
            response = conn.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise BoundaryError("response_byte_limit")
            doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            response_id = doc.get("id") if isinstance(doc, dict) and isinstance(doc.get("id"), str) else None
            choices = doc.get("choices") if isinstance(doc, dict) else None
            finish_reason = (choices[0].get("finish_reason") if isinstance(choices, list) and choices and
                             isinstance(choices[0], dict) and isinstance(choices[0].get("finish_reason"), str) else None)
            usage = _parse_usage(doc)
            if usage is not None and (usage.input_tokens > reservation.input_token_bound or
                                      usage.output_tokens > reservation.max_output_tokens):
                self.ledger.halted = True
                self.ledger.settle(reservation, usage, False, response_id=response_id,
                                   finish_reason=finish_reason, elapsed_seconds=time.monotonic()-started)
                return _failed("reported_usage_exceeds_reservation_bound", reservation.amount_usd, started, usage)
            if response.status != 200:
                ok = settle_call(usage, False)
                return _failed("provider_http_error", reservation.amount_usd, started, usage,
                               "reported_cost_exceeds_reservation" if not ok else None)
            if response.getheader("Content-Encoding") not in (None, "identity"):
                ok = settle_call(usage, False)
                return _failed("compressed_response_rejected", reservation.amount_usd, started, usage,
                               "reported_cost_exceeds_reservation" if not ok else None)
            try:
                value = _parse_output(doc, invocation)
            except (BoundaryError, ValueError, TypeError, KeyError, IndexError):
                ok = settle_call(usage, False)
                return _failed("response_validation_failed", reservation.amount_usd, started, usage,
                               "reported_cost_exceeds_reservation" if not ok else None)
            if usage is None or usage.scope != "provider_reported":
                settle_call(usage, False)
                return _failed("invalid_or_missing_usage", reservation.amount_usd, started, usage)
            if invocation.group == SYNTHETIC_GROUP and (not response_id or finish_reason != "stop"):
                settle_call(usage, False)
                return _failed("smoke_response_metadata_missing", reservation.amount_usd, started, usage)
            within = settle_call(usage, True)
            if not within:
                return _failed("reported_cost_exceeds_reservation", reservation.amount_usd, started, usage)
            result = InvocationResult(True, value, usage, usage.scope, None,
                                      time.monotonic() - started, reservation.amount_usd, response_id,
                                      finish_reason, _server_usage(doc))
            if invocation.group == SYNTHETIC_GROUP:
                try:
                    _persist_smoke(self, invocation, result)
                except OSError:
                    # Settlement already happened exactly once; evidence write failure
                    # changes acceptance but must never settle/reconcile again.
                    return _failed("smoke_evidence_persistence_failed", reservation.amount_usd, started, usage)
            return result
        except BoundaryError as exc:
            settle_call(usage, False)
            return _failed(str(exc), reservation.amount_usd, started, usage)
        except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException,
                UnicodeError, ValueError, TypeError):
            settle_call(usage, False)
            return _failed("transport_or_response_invalid", reservation.amount_usd, started, usage)
        finally:
            if conn is not None:
                conn.close()

    def smoke(self) -> InvocationResult:
        """Execute real provider synthetic request and persist proof; localhost cannot mint proof."""
        if self.is_test_transport or self.smoke_path is None:
            return _failed("production_smoke_required")
        if os.environ.get("AIRLINE_TRIAL_ALLOW_LIVE") != "1" or os.environ.get("AIRLINE_TRIAL_ALLOW_SMOKE") != "1":
            return _failed("smoke_opt_in_required")
        smoke_invocation = Invocation(SYNTHETIC_GROUP,
            'Return JSON for group __ac3_synthetic_smoke__ with one unavailable item ref "AC3-SMOKE", empty-string value, and reason "synthetic probe".',
            ("AC3-SMOKE",), max_tokens=128, filing_text=False)
        return self._invoke(smoke_invocation, allow_smoke=True)


def _failed(error: str, reservation: float = 0.0, started: float | None = None,
            usage: Usage | None = None, override: str | None = None) -> InvocationResult:
    return InvocationResult(False, None, usage, usage.scope if usage else "unknown",
                            override or error, time.monotonic() - started if started else 0.0, reservation)


def _validate_invocation(inv: Invocation) -> None:
    if not isinstance(inv, Invocation) or not isinstance(inv.group, str) or not inv.group or not isinstance(inv.prompt, str):
        raise ValueError("invalid invocation")
    if not inv.refs or len(inv.refs) != len(set(inv.refs)) or any(not isinstance(ref, str) for ref in inv.refs):
        raise ValueError("invalid or duplicate invocation references")
    allowed = ("AC3-SMOKE",) if inv.group == SYNTHETIC_GROUP else ALL_REFS
    if any(ref not in allowed for ref in inv.refs):
        raise ValueError("reference outside fixed input allowlist")
    if not isinstance(inv.max_tokens, int) or isinstance(inv.max_tokens, bool) or not 0 < inv.max_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError("max_tokens outside fixed limit")


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "group": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"}, "status": {"type": "string"},
            "value": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["ref", "status", "value", "reason"], "additionalProperties": False}}},
        "required": ["group", "items"], "additionalProperties": False}


def _request_body(inv: Invocation) -> bytes:
    return json.dumps({"model": MODEL, "messages": [{"role": "user", "content": inv.prompt}],
                       "max_tokens": inv.max_tokens, "stream": False, "tool_choice": "none",
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": "trial_group", "strict": True, "schema": _schema()}}},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_constant(_constant: str) -> None:
    raise ValueError("non-finite JSON constant")


def _body_input_bound(body_bytes: int) -> int:
    return body_bytes + BODY_OVERHEAD_BYTES


def _server_usage(doc: Any) -> Mapping[str, Any] | None:
    usage = doc.get("usage") if isinstance(doc, dict) else None
    return dict(usage) if isinstance(usage, dict) else None


def _parse_usage(doc: Any) -> Usage | None:
    if not isinstance(doc, dict) or not isinstance(doc.get("usage"), dict):
        return None
    raw = doc["usage"]
    inp, out, reported_total = raw.get("prompt_tokens"), raw.get("completion_tokens"), raw.get("total_tokens")
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in (inp, out)):
        return None
    cost = inp * INPUT_USD_PER_MILLION / 1_000_000 + out * OUTPUT_USD_PER_MILLION / 1_000_000
    if not math.isfinite(cost):
        return None
    if not isinstance(reported_total, int) or isinstance(reported_total, bool) or reported_total < 0:
        return Usage(inp, out, None, cost, "provider_reported_partial", None, None)
    total = reported_total
    if not math.isfinite(cost):
        return None
    details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    completion = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    cached = details.get("cached_tokens")
    reasoning = completion.get("reasoning_tokens")
    if cached is not None and (not isinstance(cached, int) or isinstance(cached, bool) or cached < 0): cached = None
    if reasoning is not None and (not isinstance(reasoning, int) or isinstance(reasoning, bool) or reasoning < 0): reasoning = None
    scope = "provider_reported" if total == inp + out else "provider_reported_inconsistent_total"
    return Usage(inp, out, total, cost, scope, cached, reasoning)


def _parse_output(doc: Any, inv: Invocation) -> dict[str, dict[str, Any]]:
    if not isinstance(doc, dict) or doc.get("model") != MODEL:
        raise BoundaryError("wrong_model")
    choices = doc.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise BoundaryError("malformed_choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict) or choice.get("finish_reason") != "stop":
        raise BoundaryError("truncated_response")
    if "refusal" in message or "tool_calls" in message or "function_call" in message:
        raise BoundaryError("refusal_or_tool_response")
    content = message.get("content")
    if not isinstance(content, str):
        raise BoundaryError("malformed_content")
    result = json.loads(content, parse_constant=_reject_constant)
    if not isinstance(result, dict) or set(result) != {"group", "items"} or result["group"] != inv.group or not isinstance(result["items"], list):
        raise BoundaryError("schema_rejected")
    items: dict[str, dict[str, Any]] = {}
    for item in result["items"]:
        if not isinstance(item, dict) or set(item) != {"ref", "status", "value", "reason"}:
            raise BoundaryError("schema_rejected")
        ref, status, value, reason = item["ref"], item["status"], item["value"], item["reason"]
        if ref not in inv.refs or ref in items or status not in {"available", "unavailable"}:
            raise BoundaryError("duplicate_or_unexpected_reference")
        if status == "unavailable":
            if value != "" or not isinstance(reason, str) or not reason.strip():
                raise BoundaryError("invalid_unavailable_item")
            normalized_value: Any = None
        elif not isinstance(value, str) or not value.strip() or reason != "":
            raise BoundaryError("invalid_available_item")
        else:
            try:
                normalized_value = json.loads(value, parse_constant=_reject_constant)
            except (ValueError, TypeError):
                raise BoundaryError("invalid_available_item") from None
            if normalized_value is None or not _finite_tree(normalized_value) or isinstance(normalized_value, (dict, list)):
                raise BoundaryError("invalid_available_item")
        items[ref] = {"status": status, "value": normalized_value, "reason": reason or None}
    if set(items) != set(inv.refs):
        raise BoundaryError("missing_reference")
    return items


def _finite_tree(value: Any) -> bool:
    if isinstance(value, float): return math.isfinite(value)
    if isinstance(value, dict): return all(isinstance(k, str) and _finite_tree(v) for k, v in value.items())
    if isinstance(value, list): return all(_finite_tree(v) for v in value)
    return value is None or isinstance(value, (str, int, bool))


def group_invocations(prompt_for_group, *, max_refs: int = 8, max_tokens: int = DEFAULT_OUTPUT_TOKENS) -> tuple[Invocation, ...]:
    """Partition all 67 refs into bounded, disjoint groups; prompts are caller-supplied per group."""
    if not 1 <= max_refs <= len(ALL_REFS):
        raise ValueError("invalid group size")
    groups = []
    for index in range(0, len(ALL_REFS), max_refs):
        refs = ALL_REFS[index:index + max_refs]
        name = f"refs-{index // max_refs + 1:02d}"
        groups.append(Invocation(name, prompt_for_group(name, refs), refs, max_tokens, False))
    if len(groups) > MAX_CALLS:
        raise BoundaryError("reference_grouping_exceeds_call_limit")
    return tuple(groups)


def combine_groups(groups: tuple[Invocation, ...], results: tuple[InvocationResult, ...]) -> dict[str, Mapping[str, Any]]:
    if len(groups) != len(results) or not groups:
        raise BoundaryError("incomplete_group_results")
    combined: dict[str, Mapping[str, Any]] = {}
    for invocation, result in zip(groups, results):
        if not result.accepted or result.value is None or set(result.value) != set(invocation.refs):
            raise BoundaryError("incomplete_group_results")
        if set(combined).intersection(invocation.refs):
            raise BoundaryError("overlapping_groups")
        combined.update(result.value)
    if set(combined) != set(ALL_REFS):
        raise BoundaryError("incomplete_group_coverage")
    return combined


def policy_fingerprint() -> str:
    schema_hash = hashlib.sha256(json.dumps(_schema(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload = f"{MODEL}|{schema_hash}|{POLICY_VERSION}|{INPUT_USD_PER_MILLION}|{OUTPUT_USD_PER_MILLION}|{BODY_OVERHEAD_BYTES}|{MAX_OUTPUT_TOKENS}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _persist_smoke(adapter: DeepInfraAdapter, invocation: Invocation, result: InvocationResult) -> None:
    expected = Invocation(SYNTHETIC_GROUP,
        'Return JSON for group __ac3_synthetic_smoke__ with one unavailable item ref "AC3-SMOKE", empty-string value, and reason "synthetic probe".',
        ("AC3-SMOKE",), max_tokens=128, filing_text=False)
    if (adapter.is_test_transport or adapter.smoke_path is None or not result.accepted or result.usage is None
            or invocation != expected or result.response_id is None or result.finish_reason != "stop"):
        raise BoundaryError("production_smoke_required")
    data = {"evidence_kind": "local_untrusted_smoke_record", "fingerprint": policy_fingerprint(),
            "model": MODEL, "schema_hash": _schema_hash(), "policy": POLICY_VERSION,
            "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
            "total_tokens": result.usage.total_tokens, "cached_input_tokens": result.usage.cached_input_tokens,
            "reasoning_tokens": result.usage.reasoning_tokens, "cost_usd": result.usage.cost_usd,
            "response_id": result.response_id, "finish_reason": result.finish_reason,
            "elapsed_seconds": result.elapsed_seconds, "status": "success"}
    adapter.smoke_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.smoke_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def _schema_hash() -> str:
    return hashlib.sha256(json.dumps(_schema(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_successful_smoke(path: str | Path) -> None:
    # A locally persisted record is evidence for operator review only, never authority.
    raise BoundaryError("independent_transfer_authorization_unavailable")


def authorize_transfer(smoke_path: str | Path, *, filing_text: bool = False) -> None:
    # Local files and a MAC made with the API bearer secret are not independent
    # provider attestations. No trusted-runner verifier exists in this module.
    raise BoundaryError("independent_transfer_authorization_unavailable")
