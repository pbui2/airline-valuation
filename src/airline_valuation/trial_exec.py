"""Offline-only bounded subprocess boundary for isolated trial adapter tests.

This module does not invoke a live provider. A CLI cannot prove that its reported
aggregate covers hidden internal requests, so live-provider use is intentionally
not supported by this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Any


LIVE_PROVIDER_CAPABILITY = "no-go: CLI cannot guarantee a bound across internal provider requests"


class BudgetExhausted(RuntimeError):
    """The full configured invocation maximum cannot be reserved."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    retries: int
    elapsed_seconds: float
    scope: str = "reported_aggregate"


@dataclass(frozen=True)
class InvocationResult:
    accepted: bool
    value: Any | None
    usage: Usage | None
    usage_status: str
    error: str | None
    elapsed_seconds: float
    reserved_tokens: int


class TokenBudget:
    """Reserve worst-case whole-invocation tokens before every subprocess call."""

    def __init__(self, limit_tokens: int, max_tokens_per_invocation: int) -> None:
        if limit_tokens < 0 or max_tokens_per_invocation <= 0:
            raise ValueError("token limits must be nonnegative and invocation maximum positive")
        self.limit_tokens = limit_tokens
        self.max_tokens_per_invocation = max_tokens_per_invocation
        self.reserved_tokens = 0

    @property
    def available_tokens(self) -> int:
        return self.limit_tokens - self.reserved_tokens

    def reserve(self) -> int:
        maximum = self.max_tokens_per_invocation
        if self.available_tokens < maximum:
            raise BudgetExhausted("insufficient budget for a full invocation reservation")
        self.reserved_tokens += maximum
        return maximum

    def reconcile(self, reservation: int, usage: Usage | None) -> None:
        # Unknown, partial, or non-aggregate usage keeps the entire reservation.
        if usage is not None and usage.scope == "reported_aggregate":
            if usage.total_tokens < reservation:
                self.reserved_tokens -= reservation - usage.total_tokens


class FakeExecutableAdapter:
    """Call only a caller-designated fake executable using a fixed argument shape."""

    def __init__(
        self,
        executable: str | Path,
        *,
        output_schema: str | Path,
        budget: TokenBudget,
        cwd: str | Path,
        facet: str | None = None,
        timeout_seconds: float = 10.0,
        max_stdin_bytes: int = 64 * 1024,
        max_stdout_bytes: int = 64 * 1024,
        max_stderr_bytes: int = 16 * 1024,
    ) -> None:
        self.executable = Path(executable).resolve(strict=True)
        self.output_schema = Path(output_schema).resolve(strict=True)
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise ValueError("executable must be an existing executable file")
        if not self.output_schema.is_file() or not self.cwd.is_dir():
            raise ValueError("schema must be a file and cwd must be a directory")
        if facet is not None and (not facet or "\x00" in facet):
            raise ValueError("facet must be a nonempty trusted value")
        if timeout_seconds <= 0 or min(max_stdin_bytes, max_stdout_bytes, max_stderr_bytes) <= 0:
            raise ValueError("timeout and byte limits must be positive")
        self.facet = facet
        self.budget = budget
        self.timeout_seconds = timeout_seconds
        self.max_stdin_bytes = max_stdin_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes

    def invoke(self, request: Any) -> InvocationResult:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.max_stdin_bytes:
            return InvocationResult(False, None, None, "unknown", "stdin_limit_exceeded", 0.0, 0)
        reservation = self.budget.reserve()
        argv = [str(self.executable), "--output-schema", str(self.output_schema)]
        if self.facet is not None:
            argv.extend(["--facet", self.facet])
        started = time.monotonic()
        try:
            stdout, stderr, err = self._run(argv, payload)
        except OSError:
            stdout, stderr, err = b"", b"", "launch_failed"
        elapsed = time.monotonic() - started
        parsed, usage, parse_error = self._parse(stdout, elapsed)
        if err:
            return InvocationResult(False, None, None, "unknown", err, elapsed, reservation)
        if not parse_error and usage is not None and usage.total_tokens > reservation:
            parsed, parse_error = None, "reported_usage_exceeds_reserved_maximum"
        if not err and not parse_error and usage is not None:
            self.budget.reconcile(reservation, usage)
        if err:
            return InvocationResult(False, None, usage, "reported_aggregate" if usage else "unknown",
                                    err, elapsed, reservation)
        if parse_error:
            return InvocationResult(False, None, usage, "reported_aggregate" if usage else "unknown",
                                    parse_error, elapsed, reservation)
        # _parse only returns a document for a clean, complete invocation.
        return InvocationResult(True, parsed["result"], usage, "reported_aggregate", None, elapsed, reservation)

    def _run(self, argv: list[str], payload: bytes) -> tuple[bytes, bytes, str | None]:
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": str(self.cwd)}
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=env, close_fds=True, start_new_session=True, shell=False,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        out = bytearray()
        err = bytearray()
        streams: dict[str, Any] = {"stdout": process.stdout, "stderr": process.stderr}
        pending = memoryview(payload)
        try:
            for name, stream in streams.items():
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            deadline = time.monotonic() + self.timeout_seconds
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_group(process)
                    return bytes(out), bytes(err), "timeout"
                events = selector.select(min(remaining, 0.1)) if selector.get_map() else []
                if not events and process.poll() is None and not selector.get_map():
                    time.sleep(min(remaining, 0.01))
                for key, _ in events:
                    if key.data == "stdin":
                        try:
                            written = os.write(process.stdin.fileno(), pending[:8192]) if pending else 0
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        target = out if key.data == "stdout" else err
                        cap = self.max_stdout_bytes if key.data == "stdout" else self.max_stderr_bytes
                        remaining_capacity = cap + 1 - len(target)
                        if remaining_capacity <= 0:
                            self._kill_group(process)
                            return bytes(out[:self.max_stdout_bytes]), bytes(err[:self.max_stderr_bytes]), f"{key.data}_limit_exceeded"
                        try:
                            chunk = os.read(key.fileobj.fileno(), min(8192, remaining_capacity))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        target.extend(chunk)
                        if len(target) > cap:
                            self._kill_group(process)
                            return bytes(out[:self.max_stdout_bytes]), bytes(err[:self.max_stderr_bytes]), f"{key.data}_limit_exceeded"
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._kill_group(process)
                return bytes(out), bytes(err), "timeout"
            if returncode != 0:
                return bytes(out), bytes(err), "nonzero_exit"
            return bytes(out), bytes(err), None
        except (OSError, subprocess.SubprocessError):
            self._kill_group(process)
            return bytes(out), bytes(err), "subprocess_error"
        finally:
            selector.close()
            if process.poll() is None:
                self._kill_group(process)
            process.wait()

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _parse(raw: bytes, elapsed: float) -> tuple[dict[str, Any] | None, Usage | None, str | None]:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None, "malformed_or_partial_output"
        if not isinstance(document, dict):
            return None, None, "malformed_output"
        usage_data = document.get("usage")
        usage = None
        if isinstance(usage_data, dict) and usage_data.get("scope") == "reported_aggregate":
            counts = [usage_data.get(key) for key in ("input_tokens", "output_tokens", "total_tokens", "retries")]
            if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts):
                input_tokens, output_tokens, total_tokens, retries = counts
                if total_tokens == input_tokens + output_tokens:
                    usage = Usage(input_tokens, output_tokens, total_tokens, retries, elapsed)
        if document.get("status") != "completed":
            return None, usage, "refusal_or_partial_result"
        if "result" not in document:
            return None, usage, "missing_result"
        if usage is None:
            return None, None, "missing_or_invalid_usage"
        return document, usage, None
