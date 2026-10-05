import json
import os
from pathlib import Path
import time

import pytest

from airline_valuation.trial_exec import (
    BudgetExhausted,
    FakeExecutableAdapter,
    LIVE_PROVIDER_CAPABILITY,
    TokenBudget,
)


def _adapter(tmp_path: Path, body: str, **kwargs) -> tuple[FakeExecutableAdapter, Path]:
    executable = tmp_path / "fake-tool"
    executable.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = FakeExecutableAdapter(
        executable,
        output_schema=schema,
        budget=kwargs.pop("budget", TokenBudget(100, 20)),
        cwd=tmp_path,
        **kwargs,
    )
    return adapter, executable


def _response(*, usage=True, status="completed", result=None):
    document = {"status": status, "result": {"ok": True} if result is None else result}
    if usage:
        document["usage"] = {
            "scope": "reported_aggregate", "input_tokens": 3,
            "output_tokens": 4, "total_tokens": 7, "retries": 1,
        }
    return json.dumps(document, separators=(",", ":"))


def test_success_uses_fixed_args_and_reconciles_reported_aggregate(tmp_path):
    marker = tmp_path / "args.json"
    output = _response()
    code = (
        "import json,sys\n"
        f"open({str(marker)!r},'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdin.buffer.read(); print({output!r})\n"
    )
    adapter, _ = _adapter(tmp_path, code, facet="trusted-trial")
    result = adapter.invoke({"prompt": "offline"})
    assert result.accepted and result.value == {"ok": True}
    assert result.usage_status == "reported_aggregate"
    assert result.usage.input_tokens == 3 and result.usage.output_tokens == 4
    assert result.usage.total_tokens == 7 and result.usage.retries == 1
    assert result.elapsed_seconds >= 0
    assert adapter.budget.reserved_tokens == 7
    assert json.loads(marker.read_text()) == [
        "--output-schema", str((tmp_path / "schema.json").resolve()),
        "--facet", "trusted-trial",
    ]


def test_facet_is_omitted_when_not_supplied_and_no_next_call_without_full_reserve(tmp_path):
    output = _response(usage=False)
    adapter, _ = _adapter(tmp_path, f"import sys\nsys.stdin.buffer.read(); print({output!r})\n",
                          budget=TokenBudget(25, 20))
    result = adapter.invoke({"x": 1})
    assert not result.accepted and result.error == "missing_or_invalid_usage"
    assert result.usage_status == "unknown"
    assert adapter.budget.reserved_tokens == 20
    with pytest.raises(BudgetExhausted):
        adapter.invoke({"x": 2})


def test_usage_above_reserved_maximum_is_rejected_and_keeps_reservation(tmp_path):
    document = {
        "status": "completed", "result": {"ok": True},
        "usage": {"scope": "reported_aggregate", "input_tokens": 12,
                  "output_tokens": 9, "total_tokens": 21, "retries": 0},
    }
    adapter, _ = _adapter(tmp_path, f"import sys\nsys.stdin.buffer.read(); print({json.dumps(document)!r})\n")
    result = adapter.invoke({})
    assert not result.accepted and result.error == "reported_usage_exceeds_reserved_maximum"
    assert adapter.budget.reserved_tokens == 20


def test_nonzero_exit_rejects_and_keeps_usage_unknown(tmp_path):
    output = _response()
    adapter, _ = _adapter(tmp_path, f"import sys\nsys.stdin.buffer.read(); print({output!r}); sys.exit(4)\n")
    result = adapter.invoke({})
    assert not result.accepted and result.error == "nonzero_exit"
    assert result.usage is None and result.usage_status == "unknown"
    assert adapter.budget.reserved_tokens == 20


@pytest.mark.parametrize("output,error", [
    ("not-json", "malformed_or_partial_output"),
    (_response(status="refused"), "refusal_or_partial_result"),
    (_response().split(",\"usage\"")[0], "malformed_or_partial_output"),
])
def test_malformed_partial_and_refusal_are_rejected(tmp_path, output, error):
    adapter, _ = _adapter(tmp_path, f"import sys\nsys.stdin.buffer.read(); print({output!r})\n")
    result = adapter.invoke({})
    assert not result.accepted and result.error == error
    assert adapter.budget.reserved_tokens == 20


def test_launch_failure_returns_controlled_unknown_usage_and_keeps_reservation(tmp_path):
    adapter, executable = _adapter(tmp_path, "raise SystemExit(0)\n", budget=TokenBudget(20, 20))
    executable.unlink()
    result = adapter.invoke({})
    assert not result.accepted and result.error == "launch_failed"
    assert result.usage is None and result.usage_status == "unknown"
    assert adapter.budget.reserved_tokens == 20
    with pytest.raises(BudgetExhausted):
        adapter.invoke({})


def test_closed_pipes_then_hang_is_classified_as_timeout(tmp_path):
    marker = tmp_path / "pid"
    code = (
        "import os,time\n"
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
        "os.close(0); os.close(1); os.close(2); time.sleep(30)\n"
    )
    adapter, _ = _adapter(tmp_path, code, timeout_seconds=0.15, budget=TokenBudget(20, 20))
    result = adapter.invoke({})
    assert not result.accepted and result.error == "timeout"
    assert result.usage_status == "unknown" and result.usage is None
    assert adapter.budget.reserved_tokens == 20
    with pytest.raises(BudgetExhausted):
        adapter.invoke({})
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_stderr_overflow_kills_process_group_and_is_bounded(tmp_path):
    marker = tmp_path / "pid"
    code = (
        "import os,sys,time\n"
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
        "sys.stdin.buffer.read(); sys.stderr.write('x'*1000000); sys.stderr.flush(); time.sleep(30)\n"
    )
    adapter, _ = _adapter(tmp_path, code, max_stderr_bytes=128, timeout_seconds=2)
    result = adapter.invoke({})
    assert not result.accepted and result.error == "stderr_limit_exceeded"
    assert result.usage is None and result.usage_status == "unknown"
    assert adapter.budget.reserved_tokens == 20
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_stdout_overflow_kills_process_group_and_is_bounded(tmp_path):
    marker = tmp_path / "pid"
    code = (
        "import os,sys,time\n"
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
        "sys.stdin.buffer.read(); sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(30)\n"
    )
    adapter, _ = _adapter(tmp_path, code, max_stdout_bytes=128, timeout_seconds=2)
    result = adapter.invoke({})
    assert not result.accepted and result.error == "stdout_limit_exceeded"
    assert adapter.budget.reserved_tokens == 20
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_terminates_fake_process(tmp_path):
    marker = tmp_path / "pid"
    code = (
        "import os,sys,time\n"
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
        "sys.stdin.buffer.read(); time.sleep(30)\n"
    )
    adapter, _ = _adapter(tmp_path, code, timeout_seconds=0.15)
    result = adapter.invoke({})
    assert not result.accepted and result.error == "timeout"
    assert adapter.budget.reserved_tokens == 20
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stdin_limit_and_live_capability_are_explicit(tmp_path):
    adapter, _ = _adapter(tmp_path, "raise SystemExit(0)\n", max_stdin_bytes=8)
    result = adapter.invoke({"too": "large"})
    assert not result.accepted and result.error == "stdin_limit_exceeded"
    assert adapter.budget.reserved_tokens == 0
    assert "no-go" in LIVE_PROVIDER_CAPABILITY
