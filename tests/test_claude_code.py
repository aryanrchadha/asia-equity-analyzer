"""The claude-code backend: model calls through `claude -p` on a Claude plan.

A fake `claude` executable stands in for the CLI. It records its argv, stdin,
environment and working directory, and replies with configurable JSON — so
these tests check exactly what the real CLI would be handed, without running
it or spending any plan usage.
"""

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest

from tests.support import install_stubs, quiet, temp_workspace

install_stubs()

import agents.orchestrator as orch  # noqa: E402
import main  # noqa: E402
from agents.claude_code import ClaudeCodeClient, ClaudeCodeError, parse_result  # noqa: E402
from agents.orchestrator import PipelineError  # noqa: E402
from prompts.section_prompts import SPECIALISTS  # noqa: E402
from utils.report_writer import write_report  # noqa: E402

FAKE_CLI = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    args = sys.argv[1:]
    system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
    record = {{"argv": args, "stdin": sys.stdin.read(), "system": system,
              "cwd": os.getcwd(), "env": dict(os.environ)}}
    with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\\n")
    if os.environ.get("FAKE_CLI_SLEEP"):
        time.sleep(float(os.environ["FAKE_CLI_SLEEP"]))
    reply = os.environ.get("FAKE_CLI_REPLY")
    if reply is None:
        text = "## SECTION\\nSCORE: 3/5"
        reply = json.dumps({{"type": "system", "subtype": "init"}}) + "\\n" + json.dumps({{
            "type": "assistant",
            "message": {{"id": "m1", "content": [{{"type": "text", "text": text}}]}},
        }}) + "\\n" + json.dumps({{
            "type": "result", "subtype": "success", "is_error": False,
            "result": "## SECTION\\nSCORE: 3/5", "stop_reason": "end_turn",
            "api_error_status": None, "total_cost_usd": 0.01,
            "usage": {{"input_tokens": 11, "output_tokens": 22,
                      "cache_creation_input_tokens": 33,
                      "cache_read_input_tokens": 44}},
        }})
    print(reply)
    sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))
""")


def result_json(**overrides) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False,
            "result": "text", "stop_reason": "end_turn", "api_error_status": None,
            "usage": {"input_tokens": 1, "output_tokens": 2}}
    data.update(overrides)
    return json.dumps(data)


class FakeCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aea-fake-cli-")
        self.cli = os.path.join(self.dir, "claude")
        with open(self.cli, "w") as f:
            f.write(FAKE_CLI)
        os.chmod(self.cli, os.stat(self.cli).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir, "calls.jsonl")
        self.saved_env = {k: os.environ.get(k) for k in (
            "FAKE_CLI_LOG", "FAKE_CLI_REPLY", "FAKE_CLI_EXIT", "FAKE_CLI_SLEEP",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        os.environ["FAKE_CLI_LOG"] = self.log
        for k in ("FAKE_CLI_REPLY", "FAKE_CLI_EXIT", "FAKE_CLI_SLEEP"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def calls(self) -> list:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def client(self, timeout=30) -> ClaudeCodeClient:
        return ClaudeCodeClient(self.cli, timeout=timeout)

    def create(self, **kwargs):
        params = {"model": "claude-sonnet-4-6", "max_tokens": 4000,
                  "system": [{"type": "text", "text": "SYSTEM PART"}],
                  "messages": [{"role": "user", "content": "USER PART"}]}
        params.update(kwargs)
        return self.client().messages.create(**params)


class TestClientInvocation(FakeCliTest):
    def test_response_has_the_sdk_shape_the_agents_read(self):
        response = self.create()
        assert response.content[0].type == "text"
        assert response.content[0].text == "## SECTION\nSCORE: 3/5"
        assert response.stop_reason == "end_turn"
        assert response.model == "claude-sonnet-4-6"
        usage = response.usage
        assert (usage.input_tokens, usage.output_tokens) == (11, 22)
        assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (33, 44)

    def test_a_full_filing_goes_through_stdin_and_a_file_not_argv(self):
        # Regression guard: Linux caps a single argument at 128 KB, and an
        # annual report is routinely larger. Passing it in argv fails with E2BIG.
        filing = "年報 revenue grew " * 20_000   # ~340 KB
        self.create(system=[{"type": "text", "text": "BASE"},
                            {"type": "text", "text": filing}],
                    messages=[{"role": "user", "content": "Analyze " + filing}])
        call = self.calls()[0]
        assert filing in call["system"] and "BASE" in call["system"]
        assert call["stdin"] == "Analyze " + filing
        assert all(len(arg) < 1000 for arg in call["argv"])

    def test_runs_as_a_single_tool_less_isolated_turn(self):
        self.create()
        argv = self.calls()[0]["argv"]
        assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
        assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"

    def test_uses_the_plan_login_not_an_api_key(self):
        # With ANTHROPIC_API_KEY in its environment the CLI bills that key
        # instead of the logged-in plan — the opposite of this backend's point.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-be-used"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "also-not"
        self.create()
        env = self.calls()[0]["env"]
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env

    def test_output_budget_leaves_room_for_thinking(self):
        # Regression (live run): the CLI thinks by default, and 2,500 of a
        # 4,000-token specialist budget went on thinking.
        self.create(max_tokens=4000)
        assert int(self.calls()[0]["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]) >= 16000
        self.calls().clear()
        os.remove(self.log)
        self.create(max_tokens=20000)
        assert self.calls()[0]["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "20000"

    def test_runs_outside_the_project_so_no_claude_md_leaks_in(self):
        self.create()
        cwd = self.calls()[0]["cwd"]
        assert os.path.realpath(cwd) != os.path.realpath(os.getcwd())
        assert not os.path.exists(cwd), "scratch directory should be cleaned up"

    def test_temperature_is_accepted_but_not_sent(self):
        self.create(temperature=0.2)
        assert not any("temperature" in arg for arg in self.calls()[0]["argv"])

    def test_missing_binary(self):
        with self.assertRaises(ClaudeCodeError) as caught:
            ClaudeCodeClient(os.path.join(self.dir, "nope")).run("m", 1, "", "")
        assert caught.exception.kind == "missing"

    def test_timeout(self):
        os.environ["FAKE_CLI_SLEEP"] = "5"
        with self.assertRaises(ClaudeCodeError) as caught:
            self.client(timeout=0.5).run("m", 1, "", "")
        assert caught.exception.kind == "timeout"

    def test_error_result_raises(self):
        os.environ["FAKE_CLI_REPLY"] = result_json(
            is_error=True, subtype="success", result="API Error: 429 rate_limit_error",
            api_error_status=429)
        os.environ["FAKE_CLI_EXIT"] = "1"
        with self.assertRaises(ClaudeCodeError) as caught:
            self.create()
        assert caught.exception.status == 429


class TestParseResult(unittest.TestCase):
    def parse_error(self, stdout, stderr="", code=1):
        with self.assertRaises(ClaudeCodeError) as caught:
            parse_result(stdout, code, stderr, "m")
        return caught.exception

    def test_not_logged_in(self):
        e = self.parse_error(result_json(is_error=True, result="Not logged in · Please run /login"))
        assert e.kind == "auth"

    def test_prompt_too_long_is_the_filings_fault(self):
        e = self.parse_error(result_json(is_error=True, result="Prompt is too long"))
        assert e.kind == "too_long"

    def test_status_wins_over_message_text(self):
        e = self.parse_error(result_json(is_error=True, result="authentication_error",
                                         api_error_status=401))
        assert e.status == 401

    def test_non_json_output(self):
        e = self.parse_error("segfault", stderr="boom")
        assert e.kind == "other" and "boom" in e.reason

    def test_non_success_subtype_is_an_error(self):
        e = self.parse_error(result_json(subtype="error_during_execution", result=None))
        assert e.kind == "other"

    def test_refusal_passes_through_for_the_agents_to_flag(self):
        response = parse_result(result_json(stop_reason="refusal", result=""), 0, "", "m")
        assert response.stop_reason == "refusal"

    def test_every_turn_is_kept_when_the_cli_continues(self):
        # Regression (live run): on hitting the output limit the CLI continues
        # in a second turn, and `result` holds only that last turn. Sections 3
        # and 4 lost their headings and SCORE lines, and nothing was flagged.
        def assistant(msg_id, *texts):
            return json.dumps({"type": "assistant", "message": {
                "id": msg_id,
                "content": [{"type": "thinking", "thinking": ""}]
                + [{"type": "text", "text": t} for t in texts]}})
        stdout = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            assistant("m1", "## SECTION 3: MARGINS\nSCORE: 4/5\nThe dr"),
            json.dumps({"type": "stream_event"}),
            assistant("m2", "iver is mix."),
            result_json(result="iver is mix.", num_turns=2),
        ])
        text = parse_result(stdout, 0, "", "m").content[0].text
        assert text == "## SECTION 3: MARGINS\nSCORE: 4/5\nThe driver is mix."

    def test_a_re_emitted_block_is_not_duplicated(self):
        event = json.dumps({"type": "assistant", "message": {
            "id": "m1", "content": [{"type": "text", "text": "once"}]}})
        stdout = "\n".join([event, event, result_json(result="once")])
        assert parse_result(stdout, 0, "", "m").content[0].text == "once"

    def test_log_lines_before_the_json_are_ignored(self):
        response = parse_result("warming up\n" + result_json(result="ok"), 0, "", "m")
        assert response.content[0].text == "ok"


class TestErrorClassification(unittest.TestCase):
    def classify(self, error):
        try:
            with quiet(), orch.api_errors():
                raise error
        except PipelineError as e:
            return e
        raise AssertionError("not converted")

    def test_login_problems_need_a_restart(self):
        for kind in ("auth", "missing"):
            e = self.classify(ClaudeCodeError("x", kind))
            assert e.systemic and e.needs_restart, kind

    def test_plan_rate_limit_pauses_and_retries(self):
        e = self.classify(ClaudeCodeError("usage limit", status=429))
        assert e.systemic and not e.needs_restart

    def test_rejected_key_status_needs_a_restart(self):
        assert self.classify(ClaudeCodeError("x", status=401)).needs_restart

    def test_oversized_filing_is_blamed_on_the_filing(self):
        assert not self.classify(ClaudeCodeError("too long", "too_long")).systemic

    def test_unknown_failures_are_not_counted_against_filings(self):
        e = self.classify(ClaudeCodeError("weird", "other"))
        assert e.systemic and not e.needs_restart
        e = self.classify(ClaudeCodeError("slow", "timeout"))
        assert e.systemic and not e.needs_restart


class BackendTest(FakeCliTest):
    def setUp(self):
        super().setUp()
        self.real = (orch.current_backend(), orch.CLAUDE_CLI)
        orch.set_backend("claude-code")
        orch.CLAUDE_CLI = self.cli

    def tearDown(self):
        orch.set_backend(self.real[0])
        orch.CLAUDE_CLI = self.real[1]
        super().tearDown()


class TestPipelineOnClaudeCode(BackendTest):
    def test_full_pipeline_runs_through_the_cli(self):
        with quiet():
            result = orch.analyze_document_multi("THE FILING TEXT", model="claude-sonnet-4-6")
        calls = self.calls()
        assert len(calls) == len(SPECIALISTS) + 1
        specialists = [c for c in calls if "THE FILING TEXT" in c["system"]]
        assert len(specialists) == len(SPECIALISTS)
        assert result.output_tokens == 22 * len(calls)
        assert "SCORE: 3/5" in result.text

    def test_needs_no_api_key(self):
        real = orch.ANTHROPIC_API_KEY
        orch.ANTHROPIC_API_KEY = None
        try:
            assert orch.has_credentials()
            assert isinstance(orch.make_client(), ClaudeCodeClient)
        finally:
            orch.ANTHROPIC_API_KEY = real

    def test_missing_cli_is_reported_with_install_guidance(self):
        orch.CLAUDE_CLI = os.path.join(self.dir, "not-installed")
        assert not orch.has_credentials()
        with quiet() as out, self.assertRaises(PipelineError) as caught:
            orch.make_client()
        assert caught.exception.needs_restart
        assert "Claude Code CLI" in out.getvalue()

    def test_single_agent_mode_uses_the_cli_too(self):
        from agents.analyst import analyze_document
        with quiet():
            result = analyze_document("THE FILING TEXT", model="claude-sonnet-4-6")
        assert len(self.calls()) == 1 and "SCORE: 3/5" in result.text


class TestReportsOnClaudeCode(BackendTest):
    def write(self):
        temp_workspace()
        with quiet():
            path = write_report("## A\nbody", "x.pdf", 10, 20, 1.0, 0.1234)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_cost_is_labelled_as_not_billed(self):
        report = self.write()
        assert "API-Equivalent Cost (not billed" in report
        assert "billed_to: claude-plan" in report
        assert "Estimated API Cost" not in report

    def test_api_backend_keeps_the_billed_label(self):
        orch.set_backend("api")
        report = self.write()
        assert "Estimated API Cost" in report and "billed_to: api" in report


class TestDryRunOnClaudeCode(BackendTest):
    def test_ceiling_includes_the_cli_thinking_budget(self):
        from utils.cost_preview import estimate_pipeline
        on_plan = estimate_pipeline("filing " * 5000, "claude-sonnet-4-6")
        orch.set_backend("api")
        on_api = estimate_pipeline("filing " * 5000, "claude-sonnet-4-6")
        assert on_plan.ceiling > on_api.ceiling * 2


class TestBackendFlag(unittest.TestCase):
    def setUp(self):
        self.real = orch.current_backend()

    def tearDown(self):
        orch.set_backend(self.real)

    def test_flag_selects_the_backend(self):
        sys.argv = ["main.py", "--backend", "claude-code", "--show-watchlist",
                    "--watchlist-path", os.path.join(tempfile.mkdtemp(), "w.json")]
        with quiet():
            main.main()
        assert orch.current_backend() == "claude-code"

    def test_bad_env_value_is_rejected_up_front(self):
        real = main.BACKEND
        main.BACKEND = "claude_code"   # a plausible typo
        sys.argv = ["main.py", "--show-watchlist"]
        try:
            with quiet() as out, self.assertRaises(SystemExit) as caught:
                main.main()
        finally:
            main.BACKEND = real
        assert caught.exception.code == 1
        assert "claude_code" in out.getvalue()


if __name__ == "__main__":
    unittest.main()
