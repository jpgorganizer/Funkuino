#!/usr/bin/env python3
"""Embedded Claude agent sessions for Funkuino Studio (Claude Agent SDK).

Each session is one ``ClaudeSDKClient`` connected with no initial prompt; every
user turn is driven with ``query(text)`` followed by draining
``receive_response()`` (the connect-then-query pattern that keeps the input
stream open so the ``can_use_tool`` callback fires). Tool approvals and
AskUserQuestion prompts are escalated to the browser: the callback emits an
event and awaits an ``asyncio.Future`` that a REST endpoint resolves. Follow-up
turns are queued and run one at a time.

The manager translates SDK messages into the compact ``agent.event`` shapes the
frontend consumes and keeps a per-session ordered replay list (so a reloaded tab
can catch up via ``since=``). Nothing here logs the OAuth token; it is only ever
handed to the SDK via ``ClaudeAgentOptions.env``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import espuino
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                              ClaudeSDKClient, ResultMessage, SystemMessage,
                              TextBlock, ThinkingBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

# Bare allowlist: these are auto-approved deterministically (no classifier
# round-trip, no card). The risky set lives in ASK_RULES instead.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "TodoWrite",
    "Bash(./download*)", "Bash(./prepare*)",
    "Bash(./covers*)",
    "Bash(ffmpeg*)", "Bash(ffprobe*)", "Bash(ls*)", "Bash(mkdir*)", "Bash(mv*)",
    # Read-only inspection commands (same low risk as the tools above).
    "Bash(cat*)", "Bash(head*)", "Bash(tail*)", "Bash(grep*)", "Bash(find*)",
    "Bash(wc*)", "Bash(stat*)", "Bash(file*)", "Bash(du*)",
    "Bash(echo*)", "Bash(sleep*)",
    # The venv python is the workhorse (yt-dlp, mutagen probes, JSON parsing of
    # playlist metadata). Inline `-c` is arbitrary code, but the user opted for
    # auto mode anyway — a deterministic allow beats a per-call classifier trip.
    "Bash(.venv/bin/python*)",
]

# Machine-local allowlist extensions: a git-ignored `.agent-allow.json` at the
# repo root may hold a JSON array of additional patterns (e.g. for extra local
# wrapper scripts). Malformed or absent -> silently no extensions.
try:
    ALLOWED_TOOLS += [
        str(p) for p in
        json.loads((espuino.REPO_ROOT / ".agent-allow.json").read_text())
    ]
except (OSError, ValueError):
    pass

# Ask rules: ALWAYS raise a browser card, even in auto mode — Claude Code
# evaluates permission rules deny -> ask -> mode(classifier) -> allow, so these
# beat the classifier AND the allowlist (which is why ./cards --dry-run is not
# allowlisted above: an ask rule on ./cards* would shadow it anyway).
# AskUserQuestion must be here: auto mode routes around can_use_tool, and the
# question cards only work because can_use_tool fires for that tool.
ASK_RULES = [
    "AskUserQuestion",
    "Bash(./sync*)",        # real upload + full device listing (device lock!)
    "Bash(./cards*)",       # a real print mutates the print manifest
    "Bash(rm*)", "Bash(rmdir*)", "Bash(sudo*)",
    "Bash(git push*)",
]

# Passed to the CLI via --settings (the SDK forwards a JSON string verbatim).
SETTINGS_JSON = json.dumps({"permissions": {"ask": ASK_RULES}})

# Appended to the claude_code preset system prompt.
SYSTEM_APPEND = (
    "You are running inside Funkuino Studio. Never run "
    "`source .venv/bin/activate`; call `.venv/bin/python` directly. Never run a "
    "bare `./sync` (a real upload to the device) unless the user explicitly asks "
    "for it. rm, sudo, git push, ./sync and ./cards always interrupt the user "
    "for approval — avoid them unless genuinely needed."
)


def _system_append() -> str:
    """SYSTEM_APPEND plus machine-local agent knowledge: a git-ignored
    ``CLAUDE.local.md`` at the repo root is appended verbatim, so local tooling
    additions reach the agent without touching the repo's CLAUDE.md
    (``setting_sources=["project"]`` only loads the latter)."""
    try:
        extra = (espuino.REPO_ROOT / "CLAUDE.local.md").read_text()
    except OSError:
        return SYSTEM_APPEND
    return SYSTEM_APPEND + "\n\n" + extra
# ./sync (even --dry-run) is deliberately NOT allowlisted: it does the full
# recursive device listing outside Studio's device_lock, which would violate the
# one-storage-op-at-a-time rule. Agent ./sync calls are routed to a UI approval,
# and auto-denied while a Studio sync job holds the device (see can_use_tool).

URL_PROMPT = """The user pasted this URL into Funkuino Studio: {url}
Handle the complete intake: probe it (yt-dlp flat probe first), classify -- \
single song / music album / Hoerspiel (audiobook) / multi-story Folge -- then \
download with the correct wrapper invocation and flags per the project \
instructions (CLAUDE.md and any local additions).
Verify results: naming conventions, cover placement, tags; do any needed \
renames/regrouping (multi-story Folgen per the documented convention).
When a judgment call is needed (classification unclear, naming/attribution \
choices, Folge number, intro wording), ask the user with the AskUserQuestion \
tool -- questions and options in GERMAN.
Do NOT run a real ./sync or ./cards print unless the user approves; you may \
suggest it at the end. Report a concise German summary of what landed where.
Communicate with the user in German."""

CHAT_PREFIX = ("[Running inside Funkuino Studio (repo at {root}). "
               "Answer in German.]\n\n")


def _summarize_input(tool_name: str, data: dict) -> str:
    """Compact one-line rendering of a tool call's input (Bash command, path)."""
    if tool_name == "Bash":
        return str(data.get("command", "")).strip()
    for key in ("file_path", "path", "pattern", "url", "notebook_path"):
        if data.get(key):
            return str(data[key])
    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    return text[:200]


def _summarize_result(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        content = " ".join(parts)
    text = str(content or "").strip().replace("\n", " ")
    return text[:200]


def _bash_head(command: str) -> str:
    """The executable/wrapper a Bash command runs (its first token), e.g.
    ``./sync`` or ``touch``. Falls back to the raw command if it can't split."""
    try:
        import shlex
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else command.strip()


def _pattern_for(tool_name: str, data: dict) -> str:
    """Stable key for the session's 'always allow' set. For Bash we key on the
    executable (first token) so "always" generalises across its arguments; other
    tools are keyed by tool name."""
    if tool_name == "Bash":
        return f"Bash({_bash_head(str(data.get('command', '')))})"
    return tool_name


def _is_compound_command(command: str) -> bool:
    """True if a shell command chains/pipes multiple commands. Keying "always"
    on the first token would be unsafe here (it could auto-approve an unrelated
    trailing command), so such calls never get an 'always' pattern."""
    return any(op in command for op in (";", "&&", "||", "|", "\n"))


def permission_pattern(tool_name: str, data: dict) -> str | None:
    """The pattern an 'Immer erlauben' would remember, or None when it must not
    be offered (compound/piped Bash commands). None disables the always-button."""
    if tool_name == "Bash" and _is_compound_command(str(data.get("command", ""))):
        return None
    return _pattern_for(tool_name, data)


@dataclass
class Session:
    id: str
    kind: str
    model: str
    label: str
    manager: "SessionManager"
    client: ClaudeSDKClient | None = None
    status: str = "running"
    events: list[dict] = field(default_factory=list)  # {"seq":int,"event":dict}
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, tuple[str, asyncio.Future]] = field(default_factory=dict)
    always: set[str] = field(default_factory=set)
    worker: asyncio.Task | None = None
    progress_file: "Path | None" = None    # $FUNKUINO_PROGRESS_FILE for this session
    progress: dict | None = None            # latest intake snapshot (for reloads)

    def emit(self, event: dict) -> None:
        seq = self.manager.emit(self.id, event)
        self.events.append({"seq": seq, "event": event})

    def set_status(self, status: str) -> None:
        self.status = status
        self.emit({"k": "status", "status": status})

    # -- can_use_tool callback (async: it awaits a browser round-trip) --
    async def can_use_tool(self, tool_name, input_data, context):
        if tool_name == "AskUserQuestion":
            questions = input_data.get("questions", [])
            rid = secrets.token_hex(4)
            fut = self.manager.loop.create_future()
            self.pending[rid] = ("question", fut)
            self.emit({"k": "question", "requestId": rid, "questions": questions})
            self.set_status("waiting_user")
            answers = await fut
            self.pending.pop(rid, None)
            self.set_status("running")
            # Original questions array MUST be passed back; answers keyed by
            # literal question text (see spec / SDK contract).
            return PermissionResultAllow(
                updated_input={"questions": questions, "answers": answers})

        # ./sync while Studio already holds the device: auto-deny (a concurrent
        # device listing would wedge the SD writer) without a UI round-trip.
        if (tool_name == "Bash"
                and _bash_head(str(input_data.get("command", ""))) == "./sync"
                and self.manager.sync_active()):
            return PermissionResultDeny(message="Sync läuft bereits")

        pattern = permission_pattern(tool_name, input_data)
        if pattern is not None and pattern in self.always:
            return PermissionResultAllow(updated_input=input_data)

        rid = secrets.token_hex(4)
        fut = self.manager.loop.create_future()
        self.pending[rid] = ("permission", fut)
        self.emit({"k": "permission", "requestId": rid, "tool": tool_name,
                   "input": input_data,
                   "summary": _summarize_input(tool_name, input_data),
                   "pattern": pattern})  # null -> frontend hides "Immer erlauben"
        self.set_status("waiting_user")
        decision = await fut
        self.pending.pop(rid, None)
        self.set_status("running")
        if decision.get("allow"):
            if decision.get("always") and pattern is not None:
                self.always.add(pattern)
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Vom Nutzer abgelehnt")

    # -- turn worker --
    async def run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            query_text, echo_text = item
            self.set_status("running")
            self.emit({"k": "user", "text": echo_text})
            try:
                await self.client.query(query_text)
                async for msg in self.client.receive_response():
                    self._translate(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface, never crash server
                self.emit({"k": "result", "error": str(exc)})
                self.set_status("error")

    def _translate(self, msg) -> None:
        if isinstance(msg, SystemMessage):
            return  # init/other system chatter is not shown
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text.strip():
                        self.emit({"k": "text", "text": block.text})
                elif isinstance(block, ThinkingBlock):
                    continue
                elif isinstance(block, ToolUseBlock):
                    self.emit({"k": "tool", "name": block.name,
                               "summary": _summarize_input(block.name, block.input)})
        elif isinstance(msg, UserMessage):
            for block in msg.content if isinstance(msg.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    self.emit({"k": "tool_result",
                               "summary": _summarize_result(block)})
        elif isinstance(msg, ResultMessage):
            self.emit({"k": "result", "costUsd": msg.total_cost_usd,
                       "turns": msg.num_turns,
                       "error": msg.result if msg.is_error else None})
            self.set_status("error" if msg.is_error else "done")

    # -- external actions (called from REST handlers on the loop) --
    def post_turn(self, query_text: str, echo_text: str) -> None:
        self.queue.put_nowait((query_text, echo_text))

    def resolve(self, request_id: str, kind: str, value) -> bool:
        entry = self.pending.get(request_id)
        if not entry or entry[0] != kind or entry[1].done():
            return False
        entry[1].set_result(value)
        return True

    async def interrupt(self) -> None:
        for _kind, fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result({"allow": False} if _kind == "permission" else {})
        try:
            await self.client.interrupt()
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
        for _kind, fut in list(self.pending.values()):
            if not fut.done():
                fut.cancel()
        if self.progress_file is not None:
            with contextlib.suppress(OSError):
                self.progress_file.unlink()
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass


class SessionManager:
    """Owns the live agent sessions and their SDK clients."""

    def __init__(self, token: str | None,
                 emit: Callable[[str, dict], int],
                 loop: asyncio.AbstractEventLoop,
                 sync_active: Callable[[], bool] | None = None):
        self.token = token
        self.emit = emit  # (session_id, event) -> global seq
        self.loop = loop
        # Whether a Studio sync job currently holds the device (to auto-deny
        # agent ./sync calls); defaults to "never busy" if not wired.
        self.sync_active = sync_active or (lambda: False)
        self.sessions: dict[str, Session] = {}

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _options(self, model: str, can_use_tool,
                 progress_file: Path | None = None) -> ClaudeAgentOptions:
        env = {"CLAUDE_CODE_OAUTH_TOKEN": self.token}
        if progress_file is not None:
            # ./download (and wrappers built on it) writes live progress here.
            env["FUNKUINO_PROGRESS_FILE"] = str(progress_file)
        return ClaudeAgentOptions(
            cwd=str(espuino.REPO_ROOT),
            model=model,
            # Permission design (user decision 2026-07-23): "auto" + guardrails.
            # The auto-mode classifier (a separate model) silently approves or
            # DENIES routine calls — it never asks, and can_use_tool does not
            # fire for classifier-decided calls (denials surface to the agent
            # as failed tool calls). ASK_RULES above are the risk gate: ask
            # rules are evaluated before the mode, so those still raise our
            # browser cards. A bare "auto" without ask rules is NOT safe — an
            # earlier test let `rm -rf` outside the repo run with no card.
            permission_mode="auto",
            settings=SETTINGS_JSON,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": _system_append()},
            setting_sources=["project"],  # load the repo CLAUDE.md, not user/local
            allowed_tools=ALLOWED_TOOLS,
            can_use_tool=can_use_tool,
            env=env,
        )

    async def create(self, kind: str, model: str,
                     url: str | None = None, text: str | None = None) -> str:
        sid = secrets.token_hex(4)
        if kind == "url":
            query_text = URL_PROMPT.format(url=url or "")
            echo_text = url or ""
            label = (url or "URL")[:60]
        else:
            query_text = CHAT_PREFIX.format(root=espuino.REPO_ROOT) + (text or "")
            echo_text = text or ""
            label = (text or "Chat").strip().splitlines()[0][:60] if text else "Chat"

        # The can_use_tool callback is a bound method, so the session must exist
        # before the options/client that reference it.
        progress_file = espuino.REPO_ROOT / f".studio-progress-{sid}.json"
        with contextlib.suppress(OSError):
            progress_file.unlink()  # drop any stale file from a crashed run
        session = Session(id=sid, kind=kind, model=model, label=label,
                          manager=self, progress_file=progress_file)
        session.client = ClaudeSDKClient(
            options=self._options(model, session.can_use_tool, progress_file))
        await session.client.connect()
        session.worker = self.loop.create_task(session.run())
        self.sessions[sid] = session
        session.post_turn(query_text, echo_text)
        return sid

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    def summaries(self) -> list[dict]:
        return [{"id": s.id, "label": s.label, "model": s.model,
                 "status": s.status, "progress": s.progress}
                for s in self.sessions.values()]

    def replay(self, sid: str, since: int) -> list[dict] | None:
        session = self.sessions.get(sid)
        if session is None:
            return None
        return [{"seq": e["seq"], "event": e["event"]}
                for e in session.events if e["seq"] > since]

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
