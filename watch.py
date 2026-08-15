#!/usr/bin/env python3
# desk-link Phase 0: read-only tail of local seat JSONL into bus/inbox.jsonl.
"""desk-link Phase 0 read-only seat watcher.

Polls Cursor / Claude Code / Codex / Grok Build conversation JSONL and
appends one short summary event per new line to bus/inbox.jsonl.
Never writes seat files. Never opens state.vscdb, mcp.json, cookies, or .env.

First-seen policy (not a full history dump):
  - Files untouched for FIRST_SEEN_MAX_AGE_SEC (2h) are seeked to EOF.
  - Recently modified files emit at most the last FIRST_SEEN_BACKFILL_LINES
    (20) complete lines so a first --once can produce a live event quickly.
  - Later scans only consume bytes after the stored offset in bus/.offsets.json.
  - Dedup key is src + byte offset of the line.

Usage:
  py -3 watch.py --once     # one scan, then exit
  py -3 watch.py            # poll every POLL_SEC seconds
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type

JST = timezone(timedelta(hours=9))
POLL_SEC = 1.5
FIRST_SEEN_BACKFILL_LINES = 20
FIRST_SEEN_MAX_AGE_SEC = 2 * 3600
TEXT_MAX = 200
OFFSET_READ_WINDOW = 512 * 1024

ROUTING_VALUES = {
    "to": ("cursor", "claude", "codex", "grok-build"),
    "model": ("grok-4.6", "gpt-5.6-sol"),
    "effort": ("xhigh", "ultra"),
}

SEATS = (
    ("cursor", os.path.join(".cursor", "projects", "*", "agent-transcripts", "*", "*.jsonl")),
    ("claude", os.path.join(".claude", "projects", "*", "*.jsonl")),
    ("codex", os.path.join(".codex", "sessions", "*", "*", "*", "rollout-*.jsonl")),
    ("grok_build", os.path.join(".grok", "sessions", "*", "*", "chat_history.jsonl")),
)

SKIP_NAME_PARTS = (
    "state.vscdb",
    "mcp.json",
    ".env",
    "cookie",
    "cookies",
    "credential",
    "secrets",
    "secret",
    "token",
    ".sqlite",
    "id_rsa",
    ".pem",
    "auth.json",
)

# Obvious token / cookie shapes only. Do not try to be a secrets scanner.
REDACT_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b[A-Za-z0-9_]*((session|token|cookie|auth)[_-]?(id|key|secret)?)\s*[:=]\s*[^\s,;]{8,}"),
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_cli_safely(
    operation: Callable[[], int],
    *,
    storage_error: str,
    operation_error: str,
    known_error_type: Optional[Type[BaseException]] = None,
    known_error_formatter: Optional[Callable[[BaseException], str]] = None,
) -> int:
    """Run one CLI entry point without exposing raw exception diagnostics."""

    try:
        return operation()
    except Exception as exc:
        if known_error_type is not None and isinstance(exc, known_error_type):
            error = known_error_formatter(exc) if known_error_formatter is not None else operation_error
        elif isinstance(exc, OSError):
            error = storage_error
        else:
            error = operation_error
        if not isinstance(error, str) or not error.strip():
            error = operation_error
        print(
            json.dumps({"status": "error", "error": error}, ensure_ascii=False, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2


def now_iso() -> str:
    return datetime.now(JST).replace(microsecond=0).isoformat()


def env_style_path(path: str) -> str:
    up = os.environ.get("USERPROFILE") or ""
    norm = os.path.normpath(path)
    if up and norm.lower().startswith(os.path.normpath(up).lower()):
        rest = norm[len(os.path.normpath(up)) :]
        return ("%USERPROFILE%" + rest).replace("\\", "/")
    return norm.replace("\\", "/")


def looks_secret(path: str) -> bool:
    low = path.lower().replace("\\", "/")
    name = os.path.basename(low)
    for part in SKIP_NAME_PARTS:
        if part in name or f"/{part}" in low:
            return True
    return False


def redact(text: str) -> str:
    out = text.replace("\n", " ").replace("\r", " ")
    out = re.sub(r"\s+", " ", out).strip()
    for pat in REDACT_PATTERNS:
        out = pat.sub("[redacted]", out)
    if len(out) > TEXT_MAX:
        out = out[: TEXT_MAX - 1] + "…"
    return out


def first_text(obj: Any, depth: int = 0) -> str:
    if depth > 4 or obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        # Never surface encrypted blobs.
        for k in ("text", "message", "last_agent_message", "prompt", "customTitle", "agentName"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            typ = str(obj.get("type") or "")
            if typ in ("tool_result", "tool_use"):
                return typ
            return content.strip()
        if isinstance(content, list):
            texts: List[str] = []
            tools: List[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    texts.append(item.strip())
                elif isinstance(item, dict):
                    t = str(item.get("type") or "")
                    if t in ("text", "output_text", "input_text", "summary_text") and isinstance(
                        item.get("text"), str
                    ):
                        texts.append(item["text"].strip())
                    elif t == "tool_use":
                        tools.append(str(item.get("name") or "tool"))
                    elif t == "tool_result":
                        tools.append("tool_result")
                    elif t == "thinking":
                        continue
            if texts:
                return " ".join(x for x in texts if x)
            if tools:
                return "tool_use: " + ", ".join(tools[:5])
        for nest in ("message", "payload", "summary"):
            if nest in obj:
                got = first_text(obj[nest], depth + 1)
                if got:
                    return got
        if obj.get("type") == "tool_use" or obj.get("name"):
            name = obj.get("name")
            if name:
                return f"tool_use: {name}"
    if isinstance(obj, list):
        for item in obj:
            got = first_text(item, depth + 1)
            if got:
                return got
    return ""


def classify(obj: Dict[str, Any], text: str) -> str:
    typ = str(obj.get("type") or obj.get("role") or "").lower()
    if typ in ("user", "assistant", "human"):
        if text.startswith("tool_use:") or text in ("tool_result", "tool_use"):
            return "meta"
        return "utterance" if text else "meta"
    if typ == "response_item":
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        role = str(payload.get("role") or "")
        if role in ("user", "assistant") and text and not text.startswith("tool_use:"):
            return "utterance"
        return "meta"
    return "meta"


def role_label(obj: Dict[str, Any]) -> str:
    if obj.get("role"):
        return str(obj["role"])
    payload = obj.get("payload")
    if isinstance(payload, dict) and payload.get("role"):
        return str(payload["role"])
    if obj.get("type"):
        return str(obj["type"])
    return "line"


def record_ts(obj: Dict[str, Any]) -> str:
    raw = obj.get("timestamp") or obj.get("ts")
    if isinstance(obj.get("payload"), dict) and not raw:
        raw = obj["payload"].get("timestamp")
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), JST).replace(microsecond=0).isoformat()
        except (OSError, OverflowError, ValueError):
            return now_iso()
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        try:
            if s.endswith("Z"):
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            return dt.astimezone(JST).replace(microsecond=0).isoformat()
        except ValueError:
            return now_iso()
    return now_iso()


def ensure_bus(root: str) -> str:
    bus = os.path.join(root, "bus")
    os.makedirs(bus, exist_ok=True)
    gitkeep = os.path.join(bus, ".gitkeep")
    if not os.path.exists(gitkeep):
        with open(gitkeep, "a", encoding="utf-8"):
            pass
    for name in ("inbox.jsonl", "outbox.jsonl", "ack.jsonl"):
        path = os.path.join(bus, name)
        if not os.path.exists(path):
            with open(path, "a", encoding="utf-8"):
                pass
    return bus


def load_offsets(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"files": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"files": {}}


def save_offsets(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def iter_seat_files() -> Iterable[Tuple[str, str]]:
    up = os.environ.get("USERPROFILE") or ""
    if not up:
        return
    for seat, rel in SEATS:
        pattern = os.path.join(up, rel)
        for path in glob.glob(pattern):
            if not os.path.isfile(path):
                continue
            if looks_secret(path):
                continue
            yield seat, path


def last_n_line_start(path: str, n: int, size: int) -> int:
    if size <= 0 or n <= 0:
        return size
    window = min(size, OFFSET_READ_WINDOW)
    with open(path, "rb") as fh:
        fh.seek(size - window)
        data = fh.read(window)
    base = size - window
    start = 0
    if size > window:
        nl = data.find(b"\n")
        if nl == -1:
            return size
        start = nl + 1
    complete = data[start:]
    if not complete.endswith(b"\n"):
        last_nl = complete.rfind(b"\n")
        if last_nl == -1:
            return size
        complete = complete[: last_nl + 1]
    parts = complete.splitlines(keepends=True)
    take = parts[-n:]
    taken = sum(len(p) for p in take)
    return base + start + (len(complete) - taken)


def read_new_lines(path: str, start_pos: int) -> Tuple[List[Tuple[int, str]], int]:
    """Return (offset, text) for each complete line, and the new file position."""
    size = os.path.getsize(path)
    if start_pos > size:
        start_pos = 0
    if start_pos == size:
        return [], start_pos
    out: List[Tuple[int, str]] = []
    with open(path, "rb") as fh:
        fh.seek(start_pos)
        buf = fh.read()
    pos = start_pos
    i = 0
    while i < len(buf):
        nl = buf.find(b"\n", i)
        if nl == -1:
            break
        raw = buf[i:nl]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        line_off = pos
        pos = start_pos + nl + 1
        i = nl + 1
        if not raw.strip():
            continue
        out.append((line_off, raw.decode("utf-8", errors="replace")))
    return out, pos


def routing_fields(
    to: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for name, value in (("to", to), ("model", model), ("effort", effort)):
        if value is None or value == "":
            continue
        if value not in ROUTING_VALUES[name]:
            allowed = "|".join(ROUTING_VALUES[name])
            raise ValueError(f"invalid {name}: expected {allowed}")
        fields[name] = value
    return fields


def make_event(
    seat: str,
    src: str,
    obj: Dict[str, Any],
    *,
    to: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> Dict[str, str]:
    text = first_text(obj)
    kind = classify(obj, text)
    label = role_label(obj)
    if text:
        summary = f"[{label}] {text}"
    else:
        summary = f"[{label}]"
        kind = "meta"
    event = {
        "id": str(uuid.uuid4()),
        "ts": record_ts(obj),
        "seat": seat,
        "dir": "in",
        "kind": kind,
        "text": redact(summary),
        "src": src,
    }
    event.update(routing_fields(to=to, model=model, effort=effort))
    return event


def scan_file(
    seat: str,
    path: str,
    files_state: Dict[str, Any],
    seen_emit: set,
) -> List[Dict[str, str]]:
    src = env_style_path(path)
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    rec = files_state.get(src) or files_state.get(path) or {}
    first_seen = "pos" not in rec
    if first_seen:
        age = time.time() - mtime
        if age <= FIRST_SEEN_MAX_AGE_SEC:
            start = last_n_line_start(path, FIRST_SEEN_BACKFILL_LINES, size)
        else:
            start = size
    else:
        start = int(rec.get("pos") or 0)
        if start > size:
            start = last_n_line_start(path, FIRST_SEEN_BACKFILL_LINES, size)
    try:
        lines, new_pos = read_new_lines(path, start)
    except OSError:
        return []
    events: List[Dict[str, str]] = []
    for off, line in lines:
        key = (src, off)
        if key in seen_emit:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            seen_emit.add(key)
            continue
        if not isinstance(obj, dict):
            seen_emit.add(key)
            continue
        events.append(make_event(seat, src, obj))
        seen_emit.add(key)
    files_state[src] = {"pos": new_pos}
    if path in files_state and path != src:
        files_state.pop(path, None)
    return events


def append_inbox(inbox_path: str, events: List[Dict[str, str]]) -> None:
    if not events:
        return
    payload = "".join(
        json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n" for ev in events
    )
    with open(inbox_path, "a", encoding="utf-8", newline="\n") as fh:
        # Append one complete payload so the watcher and dispatcher cannot split
        # another writer's JSON object from its terminating newline.
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def one_scan(bus: str, offsets_path: str, seen_emit: set) -> List[Dict[str, str]]:
    state = load_offsets(offsets_path)
    files_state = state.setdefault("files", {})
    inbox = os.path.join(bus, "inbox.jsonl")
    all_events: List[Dict[str, str]] = []
    for seat, path in iter_seat_files():
        all_events.extend(scan_file(seat, path, files_state, seen_emit))
    append_inbox(inbox, all_events)
    save_offsets(offsets_path, state)
    return all_events


def _run_watcher(args: argparse.Namespace) -> int:
    bus = ensure_bus(SCRIPT_DIR)
    offsets_path = os.path.join(bus, ".offsets.json")
    seen_emit: set = set()

    if args.once:
        events = one_scan(bus, offsets_path, seen_emit)
        print(f"once: events={len(events)}", flush=True)
        return 0

    print(f"watching seats -> {os.path.join(bus, 'inbox.jsonl')} every {args.interval}s", flush=True)
    while True:
        events = one_scan(bus, offsets_path, seen_emit)
        if events:
            print(f"scan: new_events={len(events)}", flush=True)
        time.sleep(max(0.2, args.interval))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="desk-link Phase 0 read-only seat watcher")
    parser.add_argument("--once", action="store_true", help="single scan then exit")
    parser.add_argument("--interval", type=float, default=POLL_SEC, help="poll seconds (default 1.5)")
    args = parser.parse_args(argv)
    return run_cli_safely(
        lambda: _run_watcher(args),
        storage_error="watch storage operation failed",
        operation_error="watch operation failed",
    )


if __name__ == "__main__":
    raise SystemExit(main())
