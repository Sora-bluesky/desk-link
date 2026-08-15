import io
import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bridge
import watch


def _concurrency_worker(root, ready, entered, release, results):
    ready.set()

    def holding_runner(target, prompt, workspace):
        marker = Path(root, "launches.txt")
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("launch\n")
            handle.flush()
            os.fsync(handle.fileno())
        entered.set()
        release.wait()
        return bridge.ExecutionResult("concurrent answer")

    try:
        results.put(bridge.process_once(root, runner=holding_runner))
    except BaseException as exc:
        results.put({"worker_error": f"{type(exc).__name__}: {exc}"})


def _abandon_lock_worker(bus, acquired):
    with bridge._bus_lock(bus):
        acquired.set()
        os._exit(0)


def _lease_overlap_runner(target, prompt, workspace):
    root = Path(workspace)
    old_running = root / "old-execution-running"
    release_old = root / "release-old-execution"
    if prompt == "old":
        old_running.write_text("running", encoding="utf-8")
        while not release_old.exists():
            time.sleep(0.02)
        old_running.unlink()
        return bridge.ExecutionResult("old answer")
    if old_running.exists():
        (root / "old_execution_overlapped_second").write_text("overlap", encoding="utf-8")
    (root / "second-execution-started").write_text("started", encoding="utf-8")
    return bridge.ExecutionResult("second answer")


def _leased_dispatch_worker(root, results=None):
    try:
        result = bridge.process_once(
            root,
            runner=_lease_overlap_runner,
            execution_lease=True,
        )
        if results is not None:
            results.put(result)
    except BaseException as exc:
        if results is not None:
            results.put({"worker_error": type(exc).__name__})


class AdapterTests(unittest.TestCase):
    def test_all_adapters_use_exact_safe_argv_shell_false_and_expected_prompt_channel(self):
        prompt = "Explain 日本語 safely"
        dangerous = (
            "--force",
            "--trust",
            "--yolo",
            "danger-full-access",
            "bypasspermissions",
            "dangerously-skip-permissions",
            "dangerously-bypass-approvals-and-sandbox",
        )
        with tempfile.TemporaryDirectory() as workspace:
            calls = []
            cursor_launcher = os.path.join(workspace, "cursor-agent.CMD")

            def fake_run(argv, **kwargs):
                calls.append((list(argv), dict(kwargs)))
                self.assertIs(kwargs["shell"], False)
                self.assertEqual(kwargs["cwd"], workspace)
                self.assertTrue(kwargs["capture_output"])
                self.assertEqual(kwargs["encoding"], "utf-8")
                executable = os.path.basename(argv[0]).lower()
                if executable == "codex":
                    output_path = argv[argv.index("--output-last-message") + 1]
                    Path(output_path).write_text("codex reply", encoding="utf-8")
                    stdout = ""
                elif executable == "grok":
                    prompt_path = argv[argv.index("--prompt-file") + 1]
                    raw_prompt = Path(prompt_path).read_bytes()
                    self.assertFalse(raw_prompt.startswith(b"\xef\xbb\xbf"))
                    self.assertEqual(raw_prompt.decode("utf-8"), prompt)
                    stdout = json.dumps({"result": "grok reply"})
                elif executable == "cursor-agent.cmd":
                    stdout = json.dumps({"result": "cursor-agent reply", "session_id": "session-1"})
                else:
                    stdout = json.dumps({"result": f"{argv[0]} reply", "session_id": "session-1"})
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            with mock.patch("bridge.shutil.which", return_value=cursor_launcher), mock.patch(
                "bridge.subprocess.run", side_effect=fake_run
            ):
                results = {
                    target: bridge.dispatch_request(target, prompt, workspace)
                    for target in bridge.SUPPORTED_TARGETS
                }

        self.assertEqual(results["cursor"].text, "cursor-agent reply")
        self.assertEqual(results["claude"].text, "claude reply")
        self.assertEqual(results["codex"].text, "codex reply")
        self.assertEqual(results["grok-build"].text, "grok reply")
        by_cli = {
            "cursor-agent" if os.path.basename(argv[0]).lower() == "cursor-agent.cmd" else argv[0]: (argv, kwargs)
            for argv, kwargs in calls
        }
        self.assertEqual(
            by_cli["cursor-agent"][0],
            [
                cursor_launcher, "--print", "--output-format", "json", "--mode", "ask",
                "--sandbox", "disabled" if os.name == "nt" else "enabled",
                "--workspace", workspace,
            ],
        )
        self.assertEqual(
            by_cli["claude"][0],
            [
                "claude", "--print", "--safe-mode", "--tools", "", "--input-format",
                "text", "--output-format", "json", "--permission-mode", "dontAsk",
                "--no-session-persistence",
            ],
        )
        codex_argv, codex_kwargs = by_cli["codex"]
        self.assertEqual(
            codex_argv[:10],
            [
                "codex", "exec", "--ephemeral", "--sandbox", "read-only",
                "--ignore-user-config", "--ignore-rules", "--cd", workspace,
                "--output-last-message",
            ],
        )
        self.assertEqual(codex_argv[-1], "-")
        self.assertEqual(codex_kwargs["input"], prompt)
        grok_argv, grok_kwargs = by_cli["grok"]
        self.assertEqual(
            grok_argv,
            [
                "grok", "--no-auto-update", "--prompt-file", grok_argv[3],
                "--output-format", "json", "--cwd", workspace, "--permission-mode", "dontAsk",
                "--deny", "*", "--disable-web-search", "--no-subagents", "--no-memory",
                "--sandbox", "read-only",
            ],
        )
        self.assertIsNone(grok_kwargs["input"])
        self.assertEqual(by_cli["cursor-agent"][1]["input"], prompt)
        self.assertEqual(by_cli["claude"][1]["input"], prompt)
        for argv, _ in calls:
            self.assertNotIn(prompt, argv)
            lowered = " ".join(argv).lower()
            for flag in dangerous:
                self.assertNotIn(flag, lowered)

    def test_cursor_sandbox_is_explicit_for_native_windows_and_non_windows(self):
        prompt = "read-only question"
        with tempfile.TemporaryDirectory() as workspace:
            launcher = os.path.join(workspace, "cursor-agent.CMD")
            for platform_name, sandbox_value in (("nt", "disabled"), ("posix", "enabled")):
                with self.subTest(platform=platform_name):
                    completed = subprocess.CompletedProcess(
                        [launcher],
                        0,
                        stdout=json.dumps({"result": "answer"}),
                        stderr="",
                    )
                    with mock.patch("bridge.os.name", platform_name), mock.patch(
                        "bridge.shutil.which", return_value=launcher
                    ), mock.patch("bridge.subprocess.run", return_value=completed) as run:
                        result = bridge.dispatch_request("cursor", prompt, workspace)
                    self.assertEqual(result.text, "answer")
                    argv = run.call_args.args[0]
                    self.assertEqual(
                        argv,
                        [
                            launcher, "--print", "--output-format", "json", "--mode", "ask",
                            "--sandbox", sandbox_value, "--workspace", workspace,
                        ],
                    )
                    self.assertEqual(run.call_args.kwargs["input"], prompt)
                    self.assertIs(run.call_args.kwargs["shell"], False)
                    self.assertNotIn(prompt, argv)
                    self.assertNotIn("--trust", argv)
                    self.assertNotIn("--force", argv)
                    self.assertNotIn("--yolo", argv)

    def test_non_windows_cursor_fails_when_required_sandbox_is_unavailable(self):
        with tempfile.TemporaryDirectory() as workspace:
            launcher = os.path.join(workspace, "cursor-agent")
            unavailable = subprocess.CompletedProcess(
                [launcher],
                1,
                stdout="",
                stderr="sandbox unavailable",
            )
            with mock.patch("bridge.os.name", "posix"), mock.patch(
                "bridge.shutil.which", return_value=launcher
            ), mock.patch("bridge.subprocess.run", return_value=unavailable) as run:
                with self.assertRaisesRegex(bridge.BridgeError, "cursor-agent exited with code 1"):
                    bridge.dispatch_request("cursor", "question", workspace)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--sandbox") + 1], "enabled")

    def test_json_reply_parses_established_nested_keys_and_ndjson(self):
        nested = json.dumps(
            {
                "message": {"content": [{"type": "text", "text": "final answer"}]},
                "sessionId": "s-123",
            }
        )
        parsed = bridge.parse_json_reply(nested)
        self.assertEqual(parsed.text, "final answer")
        self.assertEqual(parsed.session_id, "s-123")
        ndjson = '{"type":"progress"}\n{"output_text":"last text"}\n'
        self.assertEqual(bridge.parse_json_reply(ndjson).text, "last text")
        with self.assertRaisesRegex(bridge.BridgeError, "natural-language"):
            bridge.parse_json_reply('{"status":"ok"}')
        with self.assertRaisesRegex(bridge.BridgeError, "not valid JSON"):
            bridge.parse_json_reply("not-json")

    def test_cursor_public_launcher_resolution_is_absolute_and_rejects_missing_or_relative(self):
        resolved = bridge._resolve_cursor_launcher()
        self.assertTrue(os.path.isabs(resolved))
        if os.name == "nt":
            self.assertIn(Path(resolved).suffix.lower(), (".cmd", ".ps1"))
        with mock.patch("bridge.shutil.which", return_value=None):
            with self.assertRaisesRegex(bridge.BridgeError, "CLI not found: cursor-agent"):
                bridge._resolve_cursor_launcher()
        with mock.patch("bridge.shutil.which", return_value="cursor-agent.CMD"):
            with self.assertRaisesRegex(bridge.BridgeError, "absolute path"):
                bridge._resolve_cursor_launcher()

    def test_missing_cli_and_nonzero_exit_are_terminal_execution_errors(self):
        with mock.patch("bridge.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(bridge.BridgeError, "CLI not found"):
                bridge._run_process(["missing"], cli_label="missing", cwd=os.getcwd())
        raw_diagnostic = "private stderr secret"
        failed = subprocess.CompletedProcess(["tool"], 7, stdout="private stdout", stderr=raw_diagnostic)
        with mock.patch("bridge.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(bridge.BridgeError, "exited with code 7"):
                bridge._run_process(["tool"], cli_label="tool", cwd=os.getcwd())
            try:
                bridge._run_process(["tool"], cli_label="tool", cwd=os.getcwd())
            except bridge.BridgeError as exc:
                self.assertEqual(str(exc), "tool exited with code 7")
                self.assertNotIn(raw_diagnostic, str(exc))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.bus = watch.ensure_bus(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def append_raw(self, raw):
        path = os.path.join(self.bus, "outbox.jsonl")
        with open(path, "ab") as handle:
            handle.write(raw)

    def append_event(self, request_id="req-1", **overrides):
        event = {
            "id": request_id,
            "ts": "2026-08-15T00:00:00+09:00",
            "seat": "bot",
            "dir": "out",
            "kind": "utterance",
            "text": "private prompt",
            "src": "grok-bot",
            "to": "cursor",
        }
        event.update(overrides)
        self.append_raw((json.dumps(event) + "\n").encode("utf-8"))
        return event

    def read_jsonl(self, name):
        path = os.path.join(self.bus, name)
        with open(path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_windows_mutex_name_is_global_across_logon_sessions(self):
        mutex_name = bridge._windows_mutex_name(self.bus, bridge.DELIVERY_LOCK_NAME)
        self.assertRegex(mutex_name, r"^Global\\desk-link-[0-9a-f]{64}$")
        self.assertNotIn("Local\\", mutex_name)

    def test_each_valid_kind_gets_started_one_correlated_terminal_and_reply(self):
        expected_targets = ("cursor", "claude", "codex", "grok-build", "cursor")
        for index, kind in enumerate(bridge.SUPPORTED_KINDS):
            self.append_event(f"req-{index}", kind=kind, to=expected_targets[index])
        runner = mock.Mock(return_value=bridge.ExecutionResult("answer", "session-9"))
        for _ in bridge.SUPPORTED_KINDS:
            result = bridge.process_once(self.root, runner=runner)
            self.assertEqual(result["status"], "ok")
        self.assertEqual(bridge.process_once(self.root, runner=runner), {"status": "idle"})
        acks = self.read_jsonl("ack.jsonl")
        replies = self.read_jsonl("inbox.jsonl")
        for index in range(len(bridge.SUPPORTED_KINDS)):
            request_id = f"req-{index}"
            matching = [ack for ack in acks if ack["reply_to"] == request_id]
            self.assertEqual([ack["kind"] for ack in matching], ["started", "terminal"])
            self.assertEqual(matching[-1]["status"], "ok")
            correlated = [reply for reply in replies if reply["reply_to"] == request_id]
            self.assertEqual(len(correlated), 1)
            self.assertEqual(correlated[0]["seat"], bridge.INTERNAL_SEATS[expected_targets[index]])
        for call in runner.call_args_list:
            self.assertEqual(call.args[1], "private prompt")

    def test_invalid_json_and_invalid_source_advance_and_get_terminal_errors(self):
        self.append_raw(b"not-json\n")
        self.append_event("wrong-source", seat="claude")
        runner = mock.Mock()
        first = bridge.process_once(self.root, runner=runner)
        second = bridge.process_once(self.root, runner=runner)
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["request_id"], "invalid:0")
        self.assertEqual(second["request_id"], "wrong-source")
        self.assertEqual(bridge.process_once(self.root, runner=runner), {"status": "idle"})
        runner.assert_not_called()
        terminals = [ack for ack in self.read_jsonl("ack.jsonl") if ack["kind"] == "terminal"]
        self.assertEqual({ack["reply_to"] for ack in terminals}, {"invalid:0", "wrong-source"})
        state = json.loads(Path(self.bus, bridge.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["offset"], os.path.getsize(os.path.join(self.bus, "outbox.jsonl")))

    def test_optional_routing_values_accept_only_watch_contract_values(self):
        self.append_event(
            "valid-routing",
            model=watch.ROUTING_VALUES["model"][0],
            effort=watch.ROUTING_VALUES["effort"][0],
        )
        self.append_event("invalid-model", model="unlisted-model")
        self.append_event("invalid-effort", effort="unlisted-effort")
        runner = mock.Mock(return_value=bridge.ExecutionResult("valid answer"))

        valid = bridge.process_once(self.root, runner=runner)
        invalid_model = bridge.process_once(self.root, runner=runner)
        invalid_effort = bridge.process_once(self.root, runner=runner)
        self.assertEqual(valid["status"], "ok")
        self.assertEqual(invalid_model["status"], "error")
        self.assertEqual(invalid_effort["status"], "error")
        runner.assert_called_once()
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual(
            [(event["reply_to"], event["status"]) for event in terminals],
            [("valid-routing", "ok"), ("invalid-model", "error"), ("invalid-effort", "error")],
        )

    def test_invalid_terminal_is_durable_before_offset_advance(self):
        self.append_raw(b"not-json\n")
        original_append = bridge._append_jsonl

        def fail_terminal(path, event):
            if path.endswith("ack.jsonl") and event.get("kind") == "terminal":
                raise OSError("injected ack failure")
            return original_append(path, event)

        with mock.patch("bridge._append_jsonl", side_effect=fail_terminal):
            with self.assertRaisesRegex(bridge.BridgeError, "invalid request terminal"):
                bridge.process_once(self.root, runner=mock.Mock())
        state_path = Path(self.bus, bridge.STATE_NAME)
        if state_path.exists():
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["offset"], 0)
        self.assertEqual(self.read_jsonl("ack.jsonl"), [])

        recovered = bridge.process_once(self.root, runner=mock.Mock())
        self.assertEqual(recovered["request_id"], "invalid:0")
        self.assertEqual(recovered["status"], "error")
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual(len(terminals), 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["offset"], os.path.getsize(os.path.join(self.bus, "outbox.jsonl")))

    def test_reply_is_redacted_and_truncated_only_in_persistent_files(self):
        self.append_event()
        secret = "sk-" + "A" * 24
        full = "x" * (watch.TEXT_MAX + 80) + " " + secret
        result = bridge.process_once(
            self.root,
            runner=mock.Mock(return_value=bridge.ExecutionResult(full)),
        )
        self.assertEqual(result["status"], "ok")
        self.assertGreater(len(result["reply"]), watch.TEXT_MAX)
        self.assertNotIn(secret, result["reply"])
        self.assertIn("[redacted]", result["reply"])
        reply = self.read_jsonl("inbox.jsonl")[0]
        terminal = [ack for ack in self.read_jsonl("ack.jsonl") if ack["kind"] == "terminal"][0]
        self.assertLessEqual(len(reply["text"]), watch.TEXT_MAX)
        self.assertLessEqual(len(terminal["text"]), watch.TEXT_MAX)
        self.assertNotIn(secret, json.dumps(reply))
        self.assertNotIn(secret, json.dumps(terminal))

    def test_offset_and_inflight_are_claimed_before_execution_and_crash_is_not_retried(self):
        self.append_event("crash-request")
        outbox_size = os.path.getsize(os.path.join(self.bus, "outbox.jsonl"))

        def crash_after_claim(target, prompt, workspace):
            state = json.loads(Path(self.bus, bridge.STATE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(state["offset"], outbox_size)
            self.assertEqual(state["inflight"]["request_id"], "crash-request")
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            bridge.process_once(self.root, runner=crash_after_claim)
        replacement_runner = mock.Mock(return_value=bridge.ExecutionResult("must not run"))
        recovered = bridge.process_once(self.root, runner=replacement_runner)
        self.assertEqual(recovered["status"], "error")
        self.assertTrue(recovered["recovered"])
        self.assertIn("new request ID", recovered["error"])
        replacement_runner.assert_not_called()
        self.assertEqual(bridge.process_once(self.root, runner=replacement_runner), {"status": "idle"})
        terminals = [
            ack for ack in self.read_jsonl("ack.jsonl")
            if ack["kind"] == "terminal" and ack["reply_to"] == "crash-request"
        ]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["status"], "error")
        matching = [ack for ack in self.read_jsonl("ack.jsonl") if ack["reply_to"] == "crash-request"]
        self.assertEqual([ack["kind"] for ack in matching], ["started", "terminal"])

    def test_execution_failure_gets_one_correlated_terminal_error(self):
        self.append_event("failed-request", to="claude")
        result = bridge.process_once(
            self.root,
            runner=mock.Mock(side_effect=bridge.BridgeError("claude exited with code 4")),
        )
        self.assertEqual(result["status"], "error")
        matching = [ack for ack in self.read_jsonl("ack.jsonl") if ack["reply_to"] == "failed-request"]
        self.assertEqual([ack["kind"] for ack in matching], ["started", "terminal"])
        self.assertEqual(matching[-1]["status"], "error")
        self.assertEqual([reply for reply in self.read_jsonl("inbox.jsonl") if reply.get("reply_to") == "failed-request"], [])

    def test_cli_stdout_and_stderr_are_never_persisted_on_nonzero_exit(self):
        self.append_event("private-diagnostic", to="claude")
        stdout_secret = "private stdout should not persist"
        stderr_secret = "private stderr should not persist"
        failed = subprocess.CompletedProcess(
            ["claude"],
            9,
            stdout=stdout_secret,
            stderr=stderr_secret,
        )
        with mock.patch("bridge.subprocess.run", return_value=failed):
            result = bridge.process_once(self.root, execution_lease=False)
        self.assertEqual(result["error"], "claude exited with code 9")
        persisted = Path(self.bus, "ack.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(stdout_secret, persisted)
        self.assertNotIn(stderr_secret, persisted)
        terminal = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual(terminal[0]["text"], "claude exited with code 9")
        self.assertEqual(self.read_jsonl("inbox.jsonl"), [])

    def test_inbox_failure_after_terminal_ok_recovers_without_terminal_error(self):
        self.append_event("delivery-failure", to="grok-build")
        runner = mock.Mock(return_value=bridge.ExecutionResult("durable answer", "session-x"))
        with mock.patch("bridge.watch.append_inbox", side_effect=OSError("injected inbox failure")):
            pending = bridge.process_once(self.root, runner=runner)
        self.assertEqual(pending["status"], "pending")
        state = json.loads(Path(self.bus, bridge.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["inflight"]["request_id"], "delivery-failure")
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual([(event["reply_to"], event["status"]) for event in terminals], [("delivery-failure", "ok")])

        replacement_runner = mock.Mock(return_value=bridge.ExecutionResult("must not execute"))
        recovered = bridge.process_once(self.root, runner=replacement_runner)
        self.assertEqual(recovered["status"], "ok")
        replacement_runner.assert_not_called()
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual([(event["reply_to"], event["status"]) for event in terminals], [("delivery-failure", "ok")])
        replies = [event for event in self.read_jsonl("inbox.jsonl") if event["reply_to"] == "delivery-failure"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["seat"], "grok_build")
        state = json.loads(Path(self.bus, bridge.STATE_NAME).read_text(encoding="utf-8"))
        self.assertIsNone(state["inflight"])

    def test_cross_process_lock_allows_only_one_external_launch(self):
        self.append_event("concurrent-request")
        context = multiprocessing.get_context("spawn")
        ready = [context.Event(), context.Event()]
        entered = context.Event()
        release = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_concurrency_worker,
                args=(self.root, ready[index], entered, release, results),
            )
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        try:
            self.assertTrue(ready[0].wait(10), "first worker did not start")
            self.assertTrue(ready[1].wait(10), "second worker did not start")
            self.assertTrue(entered.wait(10), "no worker entered the external runner")
            time.sleep(0.25)
            launches = Path(self.root, "launches.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(launches, ["launch"])
        finally:
            release.set()
            for worker in workers:
                worker.join(10)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(10)
        self.assertEqual([worker.exitcode for worker in workers], [0, 0])
        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        self.assertEqual(sorted(outcome["status"] for outcome in outcomes), ["idle", "ok"])
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual(len(terminals), 1)

    def test_bus_lock_is_released_when_owner_process_dies(self):
        context = multiprocessing.get_context("spawn")
        acquired = context.Event()
        worker = context.Process(target=_abandon_lock_worker, args=(self.bus, acquired))
        worker.start()
        self.assertTrue(acquired.wait(10), "worker did not acquire the bus lock")
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(10)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(bridge.process_once(self.root, runner=mock.Mock()), {"status": "idle"})

    def test_dispatcher_crash_does_not_overlap_surviving_old_cli_with_second(self):
        self.append_event("old-request", text="old", cwd=self.root)
        self.append_event("second-request", text="second", cwd=self.root)
        context = multiprocessing.get_context("spawn")
        first_dispatcher = context.Process(target=_leased_dispatch_worker, args=(self.root,))
        first_dispatcher.start()
        old_running = Path(self.root, "old-execution-running")
        for _ in range(200):
            if old_running.exists():
                break
            time.sleep(0.025)
        self.assertTrue(old_running.exists(), "old execution did not start")

        first_dispatcher.terminate()
        first_dispatcher.join(10)
        self.assertFalse(first_dispatcher.is_alive())
        recovered = bridge.process_once(self.root, runner=mock.Mock())
        self.assertEqual(recovered["request_id"], "old-request")
        self.assertEqual(recovered["status"], "error")

        results = context.Queue()
        second_dispatcher = context.Process(
            target=_leased_dispatch_worker,
            args=(self.root, results),
        )
        second_dispatcher.start()
        time.sleep(0.35)
        second_started = Path(self.root, "second-execution-started")
        overlap = Path(self.root, "old_execution_overlapped_second")
        self.assertFalse(second_started.exists())
        self.assertFalse(overlap.exists())

        Path(self.root, "release-old-execution").write_text("release", encoding="utf-8")
        second_dispatcher.join(15)
        if second_dispatcher.is_alive():
            second_dispatcher.terminate()
            second_dispatcher.join(10)
        self.assertEqual(second_dispatcher.exitcode, 0)
        self.assertEqual(results.get(timeout=5)["status"], "ok")
        self.assertTrue(second_started.exists())
        self.assertFalse(overlap.exists(), "old_execution_overlapped_second must remain false")
        terminals = [event for event in self.read_jsonl("ack.jsonl") if event["kind"] == "terminal"]
        self.assertEqual(
            [(event["reply_to"], event["status"]) for event in terminals],
            [("old-request", "error"), ("second-request", "ok")],
        )

    def test_producers_enqueue_complete_unique_lines_while_delivery_is_blocked(self):
        self.append_event("request-a")
        runner_entered = threading.Event()
        release_runner = threading.Event()
        consumer_result = {}

        def blocked_runner(target, prompt, workspace):
            runner_entered.set()
            release_runner.wait()
            return bridge.ExecutionResult("answer a")

        def consume_a():
            consumer_result.update(bridge.process_once(self.root, runner=blocked_runner))

        consumer = threading.Thread(target=consume_a)
        consumer.start()
        self.assertTrue(runner_entered.wait(5), "request A did not reach its runner")

        producer_start = threading.Event()
        produced = []
        producer_errors = []

        def enqueue(prompt):
            producer_start.wait()
            try:
                produced.append(bridge._append_request(self.root, "codex", prompt, None))
            except BaseException as exc:
                producer_errors.append(exc)

        producers = [threading.Thread(target=enqueue, args=(f"request {index}",)) for index in range(4)]
        for producer in producers:
            producer.start()
        producer_start.set()
        for producer in producers:
            producer.join(5)
        try:
            self.assertTrue(all(not producer.is_alive() for producer in producers))
            self.assertEqual(producer_errors, [])
            self.assertEqual(len(produced), 4)
            self.assertEqual(len(set(produced)), 4)
            lines = Path(self.bus, "outbox.jsonl").read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]
            self.assertEqual(len(events), 5)
            self.assertEqual(events[0]["id"], "request-a")
            self.assertEqual({event["id"] for event in events[1:]}, set(produced))
            self.assertTrue(consumer.is_alive(), "request A runner was not still blocked")
        finally:
            release_runner.set()
            consumer.join(5)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(consumer_result["status"], "ok")

    def test_workspace_must_exist_and_runner_is_not_called(self):
        missing = os.path.join(self.root, "does-not-exist")
        self.append_event(cwd=missing)
        runner = mock.Mock()
        result = bridge.process_once(self.root, runner=runner)
        self.assertEqual(result["status"], "error")
        runner.assert_not_called()
        with self.assertRaisesRegex(bridge.BridgeError, "existing directory"):
            bridge._resolve_workspace(missing)

    def test_incomplete_line_is_not_claimed(self):
        self.append_raw(b'{"id":"partial"}')
        self.assertEqual(bridge.process_once(self.root, runner=mock.Mock()), {"status": "idle"})
        state_path = os.path.join(self.bus, bridge.STATE_NAME)
        state = json.loads(Path(state_path).read_text(encoding="utf-8")) if os.path.exists(state_path) else {"offset": 0}
        self.assertEqual(state["offset"], 0)


class AskTests(unittest.TestCase):
    def test_ask_reads_full_prompt_stores_request_and_waits_for_its_id(self):
        with tempfile.TemporaryDirectory() as root:
            observed = {}

            def fake_process(process_root):
                outbox = Path(process_root, "bus", "outbox.jsonl")
                event = json.loads(outbox.read_text(encoding="utf-8").splitlines()[-1])
                observed.update(event)
                return {
                    "request_id": event["id"],
                    "target": event["to"],
                    "status": "ok",
                    "reply": "answer",
                }

            with mock.patch.object(bridge, "SCRIPT_DIR", root), mock.patch(
                "bridge.process_once", side_effect=fake_process
            ):
                result = bridge.ask("claude", "line one\nline two", root)
            self.assertEqual(result["reply"], "answer")
            self.assertEqual(observed["text"], "line one\nline two")
            self.assertEqual(observed["seat"], "bot")
            self.assertEqual(observed["dir"], "out")
            self.assertEqual(observed["src"], "grok-bot")
            self.assertEqual(observed["to"], "claude")
            self.assertIn("cwd", observed)
        with self.assertRaisesRegex(bridge.BridgeError, "non-empty"):
            bridge.ask("cursor", " \n", None)

    def test_run_main_prints_only_content_safe_status(self):
        result = {
            "request_id": "r1",
            "target": "cursor",
            "status": "ok",
            "reply": "must not print",
        }
        stdout = io.StringIO()
        with mock.patch("bridge.process_once", return_value=result), mock.patch("sys.stdout", stdout):
            code = bridge.main(["run", "--once"])
        self.assertEqual(code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed, {"request_id": "r1", "target": "cursor", "status": "ok"})

    def test_main_catches_permission_error_without_traceback_or_private_path(self):
        private_path = "PRIVATE_PATH_SENTINEL"
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            bus = watch.ensure_bus(root)
            request = {
                "id": "permission-failure",
                "ts": "2026-08-15T00:00:00+09:00",
                "seat": "bot",
                "dir": "out",
                "kind": "utterance",
                "text": "private prompt",
                "src": "grok-bot",
                "to": "claude",
            }
            Path(bus, "outbox.jsonl").write_text(json.dumps(request) + "\n", encoding="utf-8")
            with mock.patch.object(bridge, "SCRIPT_DIR", root), mock.patch(
                "bridge._append_jsonl",
                side_effect=PermissionError(13, "permission denied", private_path),
            ), mock.patch("sys.stderr", stderr):
                code = bridge.main(["run", "--once"])
        self.assertEqual(code, 2)
        output = stderr.getvalue()
        self.assertEqual(
            json.loads(output),
            {"status": "error", "error": "bridge storage operation failed"},
        )
        self.assertNotIn(private_path, output)
        self.assertNotIn("Traceback", output)

    def test_ask_uses_durable_result_when_an_external_consumer_returns_it_to_idle(self):
        with tempfile.TemporaryDirectory() as root:
            consumed = False

            def external_consumer(_root):
                nonlocal consumed
                if not consumed:
                    consumed = True
                    bus = Path(root, "bus")
                    request = json.loads(Path(bus, "outbox.jsonl").read_text(encoding="utf-8").splitlines()[-1])
                    bridge._append_jsonl(
                        str(Path(bus, "ack.jsonl")),
                        bridge._ack_event(
                            request["id"], request["to"], "terminal", status="ok", text="external answer"
                        ),
                    )
                    watch.append_inbox(
                        str(Path(bus, "inbox.jsonl")),
                        [bridge._reply_event(request["id"], request["to"], "external answer")],
                    )
                return {"status": "idle"}

            with mock.patch.object(bridge, "SCRIPT_DIR", root), mock.patch(
                "bridge.process_once", side_effect=external_consumer
            ):
                result = bridge.ask("codex", "external correlation", root)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reply"], "external answer")


if __name__ == "__main__":
    unittest.main()
