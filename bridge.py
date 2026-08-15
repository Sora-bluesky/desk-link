#!/usr/bin/env python3
"""Durable, default-deny request dispatcher for the desk-link JSONL bus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import watch


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORTED_TARGETS = ("cursor", "claude", "codex", "grok-build")
SUPPORTED_KINDS = ("utterance", "design", "adversarial", "independent", "implement")
INTERNAL_SEATS = {
    "cursor": "cursor",
    "claude": "claude",
    "codex": "codex",
    "grok-build": "grok_build",
}
CLI_NAMES = {
    "cursor": "cursor-agent",
    "claude": "claude",
    "codex": "codex",
    "grok-build": "grok",
}
STATE_NAME = ".delivery-state.json"
DELIVERY_LOCK_NAME = ".delivery.lock"
OUTBOX_APPEND_LOCK_NAME = ".outbox-append.lock"
EXECUTION_LOCK_NAME = ".execution.lock"


def _configure_windows_stdio() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


_configure_windows_stdio()


class BridgeError(Exception):
    """An expected request, state, parsing, or execution failure."""


@dataclass(frozen=True)
class ExecutionResult:
    text: str
    session_id: Optional[str] = None


def _redact_full(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    profile = os.environ.get("USERPROFILE") or ""
    if profile:
        for representation in {profile, profile.replace("\\", "/")}:
            text = re.sub(re.escape(representation), "%USERPROFILE%", text, flags=re.IGNORECASE)
    for pattern in watch.REDACT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _safe_error(value: Any) -> str:
    redacted = _redact_full(value)
    return redacted or "request failed without diagnostic output"


@contextmanager
def _windows_bus_lock(bus: str, lock_name: str) -> Iterator[None]:
    import ctypes

    mutex_name = _windows_mutex_name(bus, lock_name)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise BridgeError(f"could not create delivery mutex: Windows error {ctypes.get_last_error()}")
    acquired = False
    try:
        # A kernel mutex is released automatically if its owning process exits.
        wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if wait_result not in (0x00000000, 0x00000080):
            raise BridgeError(f"could not acquire delivery mutex: wait result {wait_result}")
        acquired = True
        yield
    finally:
        if acquired and not kernel32.ReleaseMutex(handle):
            kernel32.CloseHandle(handle)
            raise BridgeError(f"could not release delivery mutex: Windows error {ctypes.get_last_error()}")
        kernel32.CloseHandle(handle)


def _windows_mutex_name(bus: str, lock_name: str) -> str:
    normalized = (os.path.normcase(os.path.realpath(bus)) + "\0" + lock_name).encode("utf-8")
    return "Global\\desk-link-" + hashlib.sha256(normalized).hexdigest()


@contextmanager
def _posix_bus_lock(bus: str, lock_name: str) -> Iterator[None]:
    import fcntl

    lock_path = os.path.join(bus, lock_name)
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bus_lock(bus: str, lock_name: str = DELIVERY_LOCK_NAME):
    if os.name == "nt":
        return _windows_bus_lock(bus, lock_name)
    return _posix_bus_lock(bus, lock_name)


def _atomic_write_json(path: str, value: Dict[str, Any]) -> None:
    temp_path = f"{path}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"offset": 0, "inflight": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("delivery state is unreadable") from exc
    if not isinstance(value, dict):
        raise BridgeError("delivery state must be a JSON object")
    offset = value.get("offset")
    inflight = value.get("inflight")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise BridgeError("delivery state offset must be a non-negative integer")
    if inflight is not None and not isinstance(inflight, dict):
        raise BridgeError("delivery state inflight value must be an object or null")
    return {"offset": offset, "inflight": inflight}


def _ensure_jsonl_boundary(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        has_newline = handle.read(1) == b"\n"
    if not has_newline:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _append_jsonl(path: str, event: Dict[str, Any]) -> None:
    _ensure_jsonl_boundary(path)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _iter_jsonl(path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return events
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _terminal_ack(ack_path: str, request_id: str) -> Optional[Dict[str, Any]]:
    for event in reversed(_iter_jsonl(ack_path)):
        if (
            event.get("reply_to") == request_id
            and event.get("kind") == "terminal"
            and event.get("status") in ("ok", "error")
        ):
            return event
    return None


def _has_started_ack(ack_path: str, request_id: str) -> bool:
    return any(
        event.get("reply_to") == request_id
        and event.get("kind") == "started"
        and event.get("status") == "started"
        for event in _iter_jsonl(ack_path)
    )


def _has_reply(inbox_path: str, request_id: str) -> bool:
    return any(event.get("reply_to") == request_id for event in _iter_jsonl(inbox_path))


def _latest_reply(inbox_path: str, request_id: str) -> Optional[Dict[str, Any]]:
    for event in reversed(_iter_jsonl(inbox_path)):
        if event.get("reply_to") == request_id and event.get("dir") == "in":
            return event
    return None


def _ack_event(
    request_id: str,
    target: str,
    kind: str,
    *,
    status: str,
    text: str = "",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": watch.now_iso(),
        "seat": "bot",
        "dir": "ack",
        "kind": kind,
        "status": status,
        "text": watch.redact(text),
        "src": "bridge",
        "reply_to": request_id,
        "request_id": request_id,
        "target": target,
    }
    if session_id:
        event["session_id"] = watch.redact(session_id)
    return event


def _reply_event(
    request_id: str,
    target: str,
    persisted_text: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": watch.now_iso(),
        "seat": INTERNAL_SEATS[target],
        "dir": "in",
        "kind": "utterance",
        "text": watch.redact(persisted_text),
        "src": CLI_NAMES[target],
        "reply_to": request_id,
    }
    if session_id:
        event["session_id"] = watch.redact(session_id)
    return event


def _recover_inflight(
    state_path: str,
    state: Dict[str, Any],
    ack_path: str,
    inbox_path: str,
) -> Optional[Dict[str, Any]]:
    inflight = state.get("inflight")
    if not inflight:
        return None
    request_id = inflight.get("request_id")
    target = inflight.get("target")
    if not isinstance(request_id, str) or not request_id or target not in SUPPORTED_TARGETS:
        raise BridgeError("delivery state contains an invalid inflight request")

    terminal = _terminal_ack(ack_path, request_id)
    if terminal is None:
        if not _has_started_ack(ack_path, request_id):
            _append_jsonl(ack_path, _ack_event(request_id, target, "started", status="started"))
        error = "interrupted after queue claim; submit a new request ID to retry"
        terminal = _ack_event(request_id, target, "terminal", status="error", text=error)
        _append_jsonl(ack_path, terminal)
    elif terminal.get("status") == "ok" and not _has_reply(inbox_path, request_id):
        try:
            _ensure_jsonl_boundary(inbox_path)
            watch.append_inbox(
                inbox_path,
                [
                    _reply_event(
                        request_id,
                        target,
                        str(terminal.get("text") or ""),
                        str(terminal.get("session_id") or "") or None,
                    )
                ],
            )
        except Exception as exc:
            raise BridgeError("durable terminal exists but inbox delivery is incomplete") from exc

    state["inflight"] = None
    _atomic_write_json(state_path, state)
    return {
        "request_id": request_id,
        "target": target,
        "status": terminal["status"],
        "reply": str(terminal.get("text") or "") if terminal["status"] == "ok" else None,
        "error": str(terminal.get("text") or "") if terminal["status"] == "error" else None,
        "recovered": True,
    }


def _durable_result_locked(
    ack_path: str,
    inbox_path: str,
    request_id: str,
    target: str,
) -> Optional[Dict[str, Any]]:
    terminal = _terminal_ack(ack_path, request_id)
    if terminal is None:
        return None
    if terminal["status"] == "error":
        return {
            "request_id": request_id,
            "target": target,
            "status": "error",
            "error": str(terminal.get("text") or ""),
        }
    reply = _latest_reply(inbox_path, request_id)
    if reply is None:
        return None
    result: Dict[str, Any] = {
        "request_id": request_id,
        "target": target,
        "status": "ok",
        "reply": str(reply.get("text") or ""),
    }
    if reply.get("session_id"):
        result["session_id"] = str(reply["session_id"])
    return result


def _durable_result(root: str, request_id: str, target: str) -> Optional[Dict[str, Any]]:
    bus = watch.ensure_bus(root)
    with _bus_lock(bus, DELIVERY_LOCK_NAME):
        return _durable_result_locked(
            os.path.join(bus, "ack.jsonl"),
            os.path.join(bus, "inbox.jsonl"),
            request_id,
            target,
        )


def _resolve_workspace(raw: Optional[str], *, default: str = SCRIPT_DIR) -> str:
    candidate = raw if raw is not None else default
    if not isinstance(candidate, str) or not candidate.strip():
        raise BridgeError("workspace must be a non-empty path")
    if candidate.startswith("%USERPROFILE%"):
        profile = os.environ.get("USERPROFILE")
        if not profile:
            raise BridgeError("workspace uses %USERPROFILE% but USERPROFILE is unavailable")
        candidate = profile + candidate[len("%USERPROFILE%") :]
    resolved = os.path.realpath(os.path.abspath(candidate))
    if not os.path.isdir(resolved):
        raise BridgeError("workspace must resolve to an existing directory")
    return resolved


def _validate_event(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("request event must be a JSON object")
    required_strings = ("id", "ts", "seat", "dir", "kind", "text", "src", "to")
    for field in required_strings:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise BridgeError(f"request field {field!r} must be a non-empty string")
    if value["seat"] != "bot" or value["dir"] != "out":
        raise BridgeError("request source must have seat='bot' and dir='out'")
    if value["src"] != "grok-bot":
        raise BridgeError("request source must be 'grok-bot'")
    if value["kind"] not in SUPPORTED_KINDS:
        raise BridgeError("request kind is not supported")
    if value["to"] not in SUPPORTED_TARGETS:
        raise BridgeError("request target is not supported")
    cwd = value.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise BridgeError("request cwd must be a non-empty string when present")
    for field in ("model", "effort"):
        if field in value and value[field] not in watch.ROUTING_VALUES[field]:
            raise BridgeError(f"request {field} is not supported")
    _resolve_workspace(cwd)
    return value


def _session_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for nested in value.values():
            found = _session_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _session_id(nested)
            if found:
                return found
    return None


def _natural_text(value: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("result", "output_text", "text"):
            if key in value:
                found = _natural_text(value[key], depth + 1)
                if found:
                    return found
        for key in ("message", "content"):
            if key in value:
                found = _natural_text(value[key], depth + 1)
                if found:
                    return found
    elif isinstance(value, list):
        texts = [_natural_text(item, depth + 1) for item in value]
        return "\n".join(text for text in texts if text)
    return ""


def parse_json_reply(stdout: str) -> ExecutionResult:
    stripped = stdout.strip()
    if not stripped:
        raise BridgeError("CLI returned empty JSON output")
    values: List[Any] = []
    try:
        values.append(json.loads(stripped))
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for value in reversed(values):
        text = _natural_text(value)
        if text:
            return ExecutionResult(text=text, session_id=_session_id(value))
    if values:
        raise BridgeError("CLI JSON output did not contain final natural-language text")
    raise BridgeError("CLI output was not valid JSON")


def _run_process(
    argv: Sequence[str],
    *,
    cli_label: str,
    cwd: str,
    prompt_input: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            input=prompt_input,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError(f"CLI not found: {cli_label}") from exc
    except OSError as exc:
        raise BridgeError(f"could not start {cli_label}") from exc
    if completed.returncode != 0:
        raise BridgeError(f"{cli_label} exited with code {completed.returncode}")
    return completed


def _resolve_cursor_launcher() -> str:
    launcher = shutil.which("cursor-agent")
    if not launcher:
        raise BridgeError("CLI not found: cursor-agent")
    if not os.path.isabs(launcher):
        raise BridgeError("cursor-agent launcher did not resolve to an absolute path")
    return launcher


def _cursor_sandbox_mode() -> str:
    # Cursor's native Windows helper has no filesystem sandbox. Explicitly disable
    # the unavailable feature so user configuration cannot imply otherwise; ask mode
    # remains the read-only boundary. Other platforms require the real sandbox.
    return "disabled" if os.name == "nt" else "enabled"


def dispatch_request(target: str, prompt: str, workspace: str) -> ExecutionResult:
    if target == "cursor":
        launcher = _resolve_cursor_launcher()
        argv = [
            launcher,
            "--print",
            "--output-format",
            "json",
            "--mode",
            "ask",
            "--sandbox",
            _cursor_sandbox_mode(),
            "--workspace",
            workspace,
        ]
        return parse_json_reply(
            _run_process(argv, cli_label="cursor-agent", cwd=workspace, prompt_input=prompt).stdout
        )
    if target == "claude":
        argv = [
            "claude",
            "--print",
            "--safe-mode",
            "--tools",
            "",
            "--input-format",
            "text",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
        ]
        return parse_json_reply(
            _run_process(argv, cli_label="claude", cwd=workspace, prompt_input=prompt).stdout
        )
    if target == "codex":
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "last-message.txt")
            argv = [
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--cd",
                workspace,
                "--output-last-message",
                output_path,
                "-",
            ]
            _run_process(argv, cli_label="codex", cwd=workspace, prompt_input=prompt)
            try:
                with open(output_path, "r", encoding="utf-8") as handle:
                    text = handle.read().strip()
            except OSError as exc:
                raise BridgeError("codex final message file is unavailable") from exc
            if not text:
                raise BridgeError("codex final message file was empty")
            return ExecutionResult(text=text)
    if target == "grok-build":
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = os.path.join(temp_dir, "prompt.txt")
            with open(prompt_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(prompt)
            argv = [
                "grok",
                "--no-auto-update",
                "--prompt-file",
                prompt_path,
                "--output-format",
                "json",
                "--cwd",
                workspace,
                "--permission-mode",
                "dontAsk",
                "--deny",
                "*",
                "--disable-web-search",
                "--no-subagents",
                "--no-memory",
                "--sandbox",
                "read-only",
            ]
            # This is a deny-all model-tool boundary. Grok has no Windows OS sandbox
            # backend, so startup configuration and hooks remain outside this boundary.
            return parse_json_reply(
                _run_process(argv, cli_label="grok", cwd=workspace).stdout
            )
    raise BridgeError("request target is not supported")


def _execution_guardian_worker(
    bus: str,
    runner: Callable[[str, str, str], ExecutionResult],
    target: str,
    prompt: str,
    workspace: str,
    connection: Any,
) -> None:
    payload: Dict[str, Any]
    try:
        # The guardian, rather than the dispatcher, owns this lease. It remains alive
        # in subprocess.run until the launched CLI exits if the dispatcher is killed.
        with _bus_lock(bus, EXECUTION_LOCK_NAME):
            try:
                result = runner(target, prompt, workspace)
                payload = {
                    "status": "ok",
                    "text": result.text,
                    "session_id": result.session_id,
                }
            except BridgeError as exc:
                payload = {"status": "error", "error": _safe_error(exc)}
            except BaseException:
                payload = {"status": "error", "error": "external execution failed"}
            try:
                connection.send(payload)
            except (BrokenPipeError, EOFError, OSError):
                pass
    except BaseException:
        try:
            connection.send({"status": "error", "error": "execution guardian failed"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _run_with_execution_lease(
    bus: str,
    runner: Callable[[str, str, str], ExecutionResult],
    target: str,
    prompt: str,
    workspace: str,
) -> ExecutionResult:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    guardian = context.Process(
        target=_execution_guardian_worker,
        args=(bus, runner, target, prompt, workspace, sender),
        daemon=False,
    )
    try:
        guardian.start()
    except Exception as exc:
        receiver.close()
        sender.close()
        raise BridgeError("could not start execution guardian") from exc
    sender.close()
    try:
        try:
            payload = receiver.recv()
        except (EOFError, OSError) as exc:
            raise BridgeError("execution guardian failed") from exc
    finally:
        receiver.close()
        guardian.join()
    if payload.get("status") != "ok":
        raise BridgeError(str(payload.get("error") or "external execution failed"))
    text = payload.get("text")
    session_id = payload.get("session_id")
    if not isinstance(text, str):
        raise BridgeError("execution guardian returned an invalid result")
    if session_id is not None and not isinstance(session_id, str):
        raise BridgeError("execution guardian returned an invalid session ID")
    return ExecutionResult(text=text, session_id=session_id)


def _next_complete_line(outbox_path: str, offset: int) -> Optional[Tuple[int, str]]:
    size = os.path.getsize(outbox_path)
    if offset > size:
        raise BridgeError("outbox is shorter than the claimed delivery offset")
    lines, new_position = watch.read_new_lines(outbox_path, offset)
    if not lines:
        return None
    line_offset, line = lines[0]
    end_offset = lines[1][0] if len(lines) > 1 else new_position
    if line_offset < offset or end_offset <= line_offset:
        raise BridgeError("outbox line offsets are inconsistent")
    return end_offset, line


def _invalid_request_id(value: Any, offset: int) -> str:
    if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"].strip():
        return value["id"]
    return f"invalid:{offset}"


def _process_once_locked(
    bus: str,
    runner: Callable[[str, str, str], ExecutionResult],
    execution_lease: bool,
) -> Dict[str, Any]:
    outbox_path = os.path.join(bus, "outbox.jsonl")
    inbox_path = os.path.join(bus, "inbox.jsonl")
    ack_path = os.path.join(bus, "ack.jsonl")
    state_path = os.path.join(bus, STATE_NAME)
    state = _load_state(state_path)

    recovered = _recover_inflight(state_path, state, ack_path, inbox_path)
    if recovered is not None:
        return recovered

    next_line = _next_complete_line(outbox_path, state["offset"])
    if next_line is None:
        return {"status": "idle"}
    end_offset, line = next_line
    value: Any = None
    try:
        value = json.loads(line)
        request = _validate_event(value)
    except (json.JSONDecodeError, BridgeError) as exc:
        request_id = _invalid_request_id(value, state["offset"])
        target = value.get("to") if isinstance(value, dict) and value.get("to") in SUPPORTED_TARGETS else "invalid"
        if _terminal_ack(ack_path, request_id) is None:
            try:
                _append_jsonl(
                    ack_path,
                    _ack_event(
                        request_id,
                        target,
                        "terminal",
                        status="error",
                        text=f"invalid request event: {exc}",
                    ),
                )
            except Exception as write_error:
                raise BridgeError("could not persist invalid request terminal error") from write_error
        # The durable terminal is the deduplication record if this state write is interrupted.
        state["offset"] = end_offset
        _atomic_write_json(state_path, state)
        return {"request_id": request_id, "target": target, "status": "error", "error": _safe_error(exc)}

    request_id = request["id"]
    target = request["to"]
    workspace = _resolve_workspace(request.get("cwd"))
    prior_terminal = _terminal_ack(ack_path, request_id)
    if prior_terminal is not None:
        state["offset"] = end_offset
        _atomic_write_json(state_path, state)
        return {
            "request_id": request_id,
            "target": target,
            "status": prior_terminal["status"],
            "error": prior_terminal.get("text") if prior_terminal["status"] == "error" else None,
            "duplicate": True,
        }

    state["offset"] = end_offset
    state["inflight"] = {
        "request_id": request_id,
        "target": target,
        "claimed_ts": watch.now_iso(),
    }
    _atomic_write_json(state_path, state)
    _append_jsonl(ack_path, _ack_event(request_id, target, "started", status="started"))

    try:
        if execution_lease:
            result = _run_with_execution_lease(bus, runner, target, request["text"], workspace)
        else:
            result = runner(target, request["text"], workspace)
        full_text = _redact_full(result.text)
        if not full_text:
            raise BridgeError("CLI returned an empty final reply")
        session_id = _redact_full(result.session_id) if result.session_id else None
    except Exception as exc:
        if isinstance(exc, BridgeError):
            full_error = _safe_error(exc)
        elif isinstance(exc, OSError):
            full_error = "external execution failed"
        else:
            full_error = "external dispatcher failed"
        _append_jsonl(
            ack_path,
            _ack_event(request_id, target, "terminal", status="error", text=full_error),
        )
        response: Dict[str, Any] = {
            "request_id": request_id,
            "target": target,
            "status": "error",
            "error": full_error,
        }
        state["inflight"] = None
        _atomic_write_json(state_path, state)
        return response

    terminal = _ack_event(
        request_id,
        target,
        "terminal",
        status="ok",
        text=full_text,
        session_id=session_id,
    )
    try:
        _append_jsonl(ack_path, terminal)
    except Exception:
        # External execution is already complete. Recovery will produce an interrupted
        # terminal without rerunning it because no successful terminal is durable.
        return {
            "request_id": request_id,
            "target": target,
            "status": "pending",
            "reply": full_text,
        }

    try:
        _ensure_jsonl_boundary(inbox_path)
        watch.append_inbox(inbox_path, [_reply_event(request_id, target, full_text, session_id)])
    except Exception:
        # The terminal ok record contains the persisted redacted reply needed by recovery.
        return {
            "request_id": request_id,
            "target": target,
            "status": "pending",
            "reply": full_text,
        }

    state["inflight"] = None
    _atomic_write_json(state_path, state)
    response = {
        "request_id": request_id,
        "target": target,
        "status": "ok",
        "reply": full_text,
    }
    if session_id:
        response["session_id"] = session_id
    return response


def process_once(
    root: str = SCRIPT_DIR,
    *,
    runner: Callable[[str, str, str], ExecutionResult] = dispatch_request,
    execution_lease: Optional[bool] = None,
) -> Dict[str, Any]:
    bus = watch.ensure_bus(root)
    use_execution_lease = runner is dispatch_request if execution_lease is None else execution_lease
    with _bus_lock(bus, DELIVERY_LOCK_NAME):
        return _process_once_locked(bus, runner, use_execution_lease)


def _append_request(root: str, target: str, prompt: str, workspace: Optional[str]) -> str:
    bus = watch.ensure_bus(root)
    with _bus_lock(bus, OUTBOX_APPEND_LOCK_NAME):
        outbox_path = os.path.join(bus, "outbox.jsonl")
        _ensure_jsonl_boundary(outbox_path)
        request_id = str(uuid.uuid4())
        event: Dict[str, Any] = {
            "id": request_id,
            "ts": watch.now_iso(),
            "seat": "bot",
            "dir": "out",
            "kind": "utterance",
            "text": prompt,
            "src": "grok-bot",
            "to": target,
        }
        if workspace is not None:
            event["cwd"] = watch.env_style_path(workspace)
        _append_jsonl(outbox_path, event)
        return request_id


def ask(target: str, prompt: str, workspace: Optional[str]) -> Dict[str, Any]:
    if target not in SUPPORTED_TARGETS:
        raise BridgeError("request target is not supported")
    if not prompt.strip():
        raise BridgeError("prompt from stdin must be non-empty")
    resolved_workspace = _resolve_workspace(workspace)
    request_id = _append_request(SCRIPT_DIR, target, prompt, resolved_workspace if workspace is not None else None)
    pending_full_reply: Optional[str] = None
    while True:
        result = process_once(SCRIPT_DIR)
        if result.get("request_id") == request_id:
            if result.get("status") in ("ok", "error"):
                if result.get("status") == "ok" and pending_full_reply:
                    result["reply"] = pending_full_reply
                return result
            if result.get("status") == "pending" and isinstance(result.get("reply"), str):
                pending_full_reply = result["reply"]
                continue
        durable = _durable_result(SCRIPT_DIR, request_id, target)
        if durable is not None:
            if durable["status"] == "ok" and pending_full_reply:
                durable["reply"] = pending_full_reply
            return durable
        if result.get("status") == "idle":
            raise BridgeError("queue is idle but the request has no durable terminal result")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="desk-link durable Grok Bot dispatcher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask_parser = subparsers.add_parser("ask", help="append and synchronously dispatch one request")
    ask_parser.add_argument("--to", required=True, choices=SUPPORTED_TARGETS)
    ask_parser.add_argument("--workspace")
    run_parser = subparsers.add_parser("run", help="process queued requests")
    run_parser.add_argument("--once", action="store_true", required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    def operation() -> int:
        if args.command == "ask":
            result = ask(args.to, sys.stdin.read(), args.workspace)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0 if result.get("status") == "ok" else 1
        result = process_once(SCRIPT_DIR)
        safe_result = {key: result[key] for key in ("request_id", "target", "status") if key in result}
        print(json.dumps(safe_result, ensure_ascii=False, separators=(",", ":")))
        return 0

    return watch.run_cli_safely(
        operation,
        known_error_type=BridgeError,
        known_error_formatter=_safe_error,
        storage_error="bridge storage operation failed",
        operation_error="bridge operation failed",
    )


if __name__ == "__main__":
    raise SystemExit(main())
