"""Run model calls through the Claude Code CLI, on your own Claude plan.

The analyzer's agents talk to a client with the SDK's `messages.create` shape.
This module provides one that shells out to `claude -p` instead of calling the
API, so analyses draw on the usage included with a logged-in Claude
subscription rather than billing an API key. It is meant for your own use on
your own machine — log in once with `claude` and it uses that session.

Each call is a single tool-less turn: the agent's system prompt replaces
Claude Code's own, no tools, settings, hooks, MCP servers or CLAUDE.md files
are loaded, and nothing is saved as a session. The filing is passed on stdin
and the system prompt through a temp file, because a full annual report is
far larger than the kernel's per-argument limit (128 KB on Linux).

Two CLI behaviours shape how calls are made, both found on the first live run:

- Claude Code thinks by default on every model, and thinking shares the
  output budget. A 4,000-token specialist budget was mostly spent thinking,
  so the output budget is raised the same way it is for thinking models on
  the API (utils.models.THINKING_OUTPUT_BUDGET).
- When a turn hits the output limit the CLI continues in a further turn, and
  its final `result` field holds only the LAST turn's text. The earlier part
  — the section heading and its SCORE line — was silently dropped. Output is
  therefore read as stream-json and the text of every turn is joined.

The CLI does not expose sampling parameters, so temperature is not sent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from utils.models import THINKING_OUTPUT_BUDGET

# Variables that would make the CLI bill an API key or another account instead
# of the logged-in Claude plan.
_API_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_LOGIN_HINTS = ("not logged in", "/login", "invalid api key", "authentication",
                "oauth", "please run")
_TOO_LONG_HINTS = ("prompt is too long", "too many tokens", "context length",
                   "context window")


class ClaudeCodeError(Exception):
    """A CLI call failed. `kind` is one of: missing, auth, too_long, timeout, other."""

    def __init__(self, reason: str, kind: str = "other", status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.status = status


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Response:
    content: list
    usage: _Usage
    model: str
    stop_reason: str | None
    cost_usd: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)


def cli_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def _system_text(system) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(block.get("text", "") for block in system)


def _user_text(messages: list) -> str:
    parts = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(block.get("text", "") for block in content)
    return "\n\n".join(parts)


def _classify(message: str, status: int | None) -> ClaudeCodeError:
    lowered = message.lower()
    if status is not None:
        return ClaudeCodeError(message, "other", status)
    if any(hint in lowered for hint in _TOO_LONG_HINTS):
        return ClaudeCodeError(message, "too_long")
    if any(hint in lowered for hint in _LOGIN_HINTS):
        return ClaudeCodeError(message, "auth")
    return ClaudeCodeError(message, "other")


def _events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _assistant_text(events: list[dict]) -> str | None:
    """Text from every assistant turn, in order — None if there were none."""
    parts, seen = [], set()
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        for index, block in enumerate(message.get("content") or []):
            if block.get("type") != "text":
                continue
            key = (message.get("id"), index, block.get("text"))
            if key in seen:   # guard against a block being re-emitted
                continue
            seen.add(key)
            parts.append(block.get("text") or "")
    # A continuation turn resumes mid-sentence, so turns are joined directly.
    return "".join(parts) if parts else None


def parse_result(stdout: str, returncode: int, stderr: str, model: str) -> Response:
    """Turn `claude -p` json or stream-json output into a Response, or raise."""
    events = _events(stdout)
    results = [e for e in events if e.get("type") == "result" or "is_error" in e]
    data = results[-1] if results else None

    if data is None:
        detail = (stderr or stdout or f"exit code {returncode}").strip()
        raise _classify(f"Claude Code failed: {detail[-300:]}", None)

    if data.get("is_error") or data.get("subtype", "success") != "success":
        detail = str(data.get("result") or data.get("subtype") or "unknown error")
        raise _classify(f"Claude Code error: {detail[:300]}", data.get("api_error_status"))

    text = _assistant_text(events)
    if text is None:
        text = str(data.get("result") or "")
    usage = data.get("usage") or {}
    return Response(
        content=[_Block(text)],
        usage=_Usage(
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
        ),
        model=model,
        stop_reason=data.get("stop_reason"),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        raw=data,
    )


class _Messages:
    def __init__(self, client: "ClaudeCodeClient"):
        self._client = client

    def create(self, *, model: str, max_tokens: int, system=None, messages: list,
               temperature: float | None = None, **_ignored) -> Response:
        return self._client.run(model, max_tokens, _system_text(system), _user_text(messages))


class ClaudeCodeClient:
    """Duck-typed stand-in for `anthropic.Anthropic` backed by `claude -p`."""

    def __init__(self, binary: str = "claude", timeout: float = 600):
        self.binary = binary
        self.timeout = timeout
        self.messages = _Messages(self)

    def command(self, model: str, system_file: str) -> list[str]:
        return [
            self.binary, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", model,
            "--system-prompt-file", system_file,
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]

    def environment(self, max_tokens: int) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _API_CREDENTIAL_VARS}
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(max_tokens, THINKING_OUTPUT_BUDGET))
        return env

    def run(self, model: str, max_tokens: int, system: str, prompt: str) -> Response:
        # A scratch working directory keeps any CLAUDE.md or project settings
        # near the caller out of the analysis.
        with tempfile.TemporaryDirectory(prefix="aea-claude-") as workdir:
            system_file = os.path.join(workdir, "system.txt")
            with open(system_file, "w", encoding="utf-8") as f:
                f.write(system)
            try:
                proc = subprocess.run(
                    self.command(model, system_file),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=workdir,
                    env=self.environment(max_tokens),
                    timeout=self.timeout,
                )
            except FileNotFoundError:
                raise ClaudeCodeError(
                    f"Claude Code CLI '{self.binary}' not found", "missing"
                ) from None
            except subprocess.TimeoutExpired:
                raise ClaudeCodeError(
                    f"Claude Code timed out after {self.timeout:g}s", "timeout"
                ) from None
        return parse_result(proc.stdout, proc.returncode, proc.stderr, model)
