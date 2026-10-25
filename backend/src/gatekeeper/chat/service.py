"""Grounded, read-only chat about one pull request's review.

- **Context:** the stored analysis (verdict, rationale, signals, verified evidence), the
  scrubbed PR title and body, and the diff of the focus file or of the top evidence
  files within a conservative token budget. Everything except the question sits in
  untrusted blocks.
- **Identity:** the question and the history are scrubbed with the item's author through
  ``gatekeeper.identity``, and the adapter's leak guard gets the item's forbidden terms.
  Nothing identity-related is stored or returned.
- **One model call:** through the adapter, inside an audit run, with role PRIMARY,
  temperature 0.2, CHAT_MAX_OUTPUT_TOKENS and the cache bypassed.
- **Checks:** references are verified with the review verifier's line rules and marked
  ``verified: false`` if they do not hold. An answer that trips the banned-word filter
  is replaced by a deterministic summary of the evidence.
- **Read-only:** the chat cannot trigger any action.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.audit import AuditRecorder
from gatekeeper.chat.prompt import CHAT_PR, ChatAnswer
from gatekeeper.config import Settings
from gatekeeper.db.models import AnalysisStatus, ChatMessage
from gatekeeper.db.repos import find_pr_item, latest_analysis
from gatekeeper.db.session import session_scope
from gatekeeper.errors import GatekeeperError, NotFoundError
from gatekeeper.github.normalize import FileChange, ItemContent
from gatekeeper.identity import forbidden_terms_for_item, scrub_for_item
from gatekeeper.logging import get_logger
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.guards import forbidden_terms_guard
from gatekeeper.models.types import CachePolicy, ModelRole, Reasoning
from gatekeeper.review_core.diff import (
    DiffView,
    citable_lines,
    parse_patch,
    render_file,
    text_tokens,
)
from gatekeeper.review_core.reply import SUMMARY, contains_banned, defuse_mentions
from gatekeeper.review_core.verifier import check_lines

log = get_logger("gatekeeper.chat")

#: Diff budget for one chat answer (the adapter's conservative token estimate).
CHAT_DIFF_BUDGET_TOKENS: Final = 6_000
MAX_HISTORY_CHARS: Final = 1_500
CHAT_TEMPERATURE: Final = 0.2
NO_DIFF: Final = "(no diff is available for these files)"
REVIEWED: Final = frozenset({AnalysisStatus.COMPLETED.value, AnalysisStatus.ABSTAINED.value})


@dataclass(frozen=True)
class ChatQuestion:
    owner: str
    name: str
    number: int
    message: str
    history: Sequence[tuple[str, str]] = ()
    focus_path: str | None = None


@dataclass(frozen=True)
class ChatReply:
    answer: str
    references: list[dict[str, Any]]
    message_id: UUID


def _span(start: Any, end: Any) -> str:
    return f"L{start}" if start == end else f"L{start}-L{end}"


def review_summary(verdict: str | None, status: str, result: Mapping[str, Any]) -> str:
    confidence = result.get("confidence")
    lines = [
        f"Verdict: {SUMMARY.get(verdict or 'needs_human_look', verdict)} (status {status}"
        + (f", confidence {confidence:.2f})" if isinstance(confidence, float) else ")"),
    ]
    if result.get("abstain_reason"):
        lines.append(f"Abstain reason: {result['abstain_reason']}")
    if result.get("claim_match"):
        lines.append(f"Does the change match its description: {result['claim_match']}")
    if result.get("rationale"):
        lines.append(f"Rationale: {result['rationale']}")
    signals = [
        s
        for s in result.get("signals") or []
        if s.get("direction") != "neutral" or (s.get("id") == "truncated_input" and s.get("value"))
    ]
    lines.append("Signals:" if signals else "Signals: none notable")
    lines += [f"- {s['id']} [{s['direction']}]: {s['explanation']}" for s in signals]
    evidence = result.get("evidence") or []
    lines.append("Verified evidence:" if evidence else "Verified evidence: none")
    lines += [
        f"- {e['path']}:{_span(e['line_start'], e['line_end'])} ({e['polarity']}): {e['claim']}"
        for e in evidence
    ]
    if result.get("dropped_evidence_count"):
        lines.append(f"Citations dropped by the verifier: {result['dropped_evidence_count']}")
    return "\n".join(lines)


def chat_diff(content: ItemContent, result: Mapping[str, Any], focus: str | None) -> DiffView:
    """The focus file first, then files with verified evidence, then the rest, each whole,
    until the budget is used."""
    by_path: dict[str, FileChange] = {f.path: f for f in content.files}
    order: list[str] = [focus] if focus in by_path else []
    for entry in result.get("evidence") or []:
        path = entry.get("path")
        if path in by_path and path not in order:
            order.append(path)
    order += [f.path for f in content.files if f.path not in order]
    sections: list[str] = []
    citable: dict[str, Mapping[int, str]] = {}
    included: list[str] = []
    omitted: list[str] = []
    without: list[str] = []
    used = 0
    for path in order:
        file = by_path[path]
        if file.patch is None:
            without.append(path)
            continue
        hunks = parse_patch(file.patch)
        rendered = render_file(file, hunks)
        cost = text_tokens(rendered)
        if used + cost > CHAT_DIFF_BUDGET_TOKENS:
            omitted.append(path)
            continue
        sections.append(rendered)
        citable[path] = citable_lines(hunks)
        included.append(path)
        used += cost
    if omitted:
        sections.append("FILES NOT SHOWN (chat budget): " + ", ".join(omitted))
    text = "\n\n".join(sections)
    return DiffView(
        text=text,
        included=tuple(included),
        omitted=tuple(omitted),
        without_patch=tuple(without),
        citable=citable,
        estimated_tokens=text_tokens(text),
    )


def evidence_summary(verdict: str | None, result: Mapping[str, Any]) -> str:
    """Deterministic fallback answer when the model's answer trips the word filter."""
    label = SUMMARY.get(verdict or "needs_human_look", "Needs a human look")
    evidence = result.get("evidence") or []
    if not evidence:
        return f'The review\'s verdict is "{label}". No verified evidence was recorded.'
    bullets = "\n".join(
        f"- `{e['path']}:{_span(e['line_start'], e['line_end'])}`: {e['claim']}"
        for e in evidence[:4]
    )
    return f'The review\'s verdict is "{label}". Verified evidence:\n{bullets}'


def _history_text(history: Sequence[tuple[str, str]]) -> str:
    if not history:
        return "(none)"
    return "\n".join(
        f"{'Maintainer' if role == 'user' else 'Assistant'}: {text}" for role, text in history
    )


def answer_question(
    question: ChatQuestion,
    *,
    settings: Settings,
    sessions: sessionmaker[Session],
    recorder: AuditRecorder,
    model: ModelAdapter,
) -> ChatReply:
    with session_scope(sessions) as session:
        item = find_pr_item(session, question.owner, question.name, question.number)
        analysis = latest_analysis(session, item.id, REVIEWED) if item else None
        if item is None or analysis is None:
            raise NotFoundError("analyse this pull request before asking about its review")
        content = ItemContent.model_validate(item.content)
        terms = forbidden_terms_for_item(session, item.id)
        message = scrub_for_item(session, item.id, question.message.strip())
        keep = settings.chat_max_history
        turns = list(question.history)[-keep:] if keep else []
        history = [
            (role, scrub_for_item(session, item.id, text[:MAX_HISTORY_CHARS]))
            for role, text in turns
        ]
        result: dict[str, Any] = dict(analysis.result or {})
        item_id, analysis_id = item.id, analysis.id
        verdict, status = analysis.verdict, analysis.status

    focus = question.focus_path if question.focus_path in {f.path for f in content.files} else None
    diff = chat_diff(content, result, focus)
    note = f"focus file: {focus}" if focus else "files with verified evidence first"
    rendered = CHAT_PR.render(
        review=review_summary(verdict, status, result),
        title=content.title,
        body=content.body,
        diff=diff.text or NO_DIFF,
        diff_note=note,
        history=_history_text(history),
        question=message,
    )
    request = rendered.to_request(
        ChatAnswer,
        role=ModelRole.PRIMARY,
        temperature=CHAT_TEMPERATURE,
        max_output_tokens=settings.chat_max_output_tokens,
        reasoning=Reasoning.OFF,
        cache_policy=CachePolicy.BYPASS,
    )
    run = recorder.start_run(
        item_id=item_id,
        payload={
            "kind": "chat",
            "analysis_id": str(analysis_id),
            "prompt_id": CHAT_PR.id,
            "prompt_version": CHAT_PR.version,
            "focus": focus is not None,
            "history_turns": len(history),
        },
    )
    try:
        response = model.with_validators(forbidden_terms_guard(terms)).generate(request, run)
    except GatekeeperError as exc:
        try:
            run.end(status="failed", payload={"code": exc.code})
        except GatekeeperError:
            log.error("could not end the chat audit run")
        raise

    parsed = response.parsed
    references = [
        {
            "path": ref.path,
            "line_start": ref.line_start,
            "line_end": ref.line_end,
            "verified": check_lines(ref.path, ref.line_start, ref.line_end, diff) is None,
        }
        for ref in parsed.references
    ]
    answer = parsed.answer.strip()
    filtered = contains_banned(answer)
    if filtered:
        answer = evidence_summary(verdict, result)
    answer = defuse_mentions(answer)
    run.end(
        status="completed",
        payload={
            "references": len(references),
            "verified_references": sum(1 for r in references if r["verified"]),
            "answer_replaced_by_filter": filtered,
        },
    )
    with session_scope(sessions) as session:
        session.add(
            ChatMessage(item_id=item_id, analysis_id=analysis_id, role="user", content=message)
        )
        reply = ChatMessage(
            item_id=item_id,
            analysis_id=analysis_id,
            role="assistant",
            content=answer,
            refs=references,
        )
        session.add(reply)
        session.flush()
        message_id = reply.id
    return ChatReply(answer=answer, references=references, message_id=message_id)


def chat_history(
    sessions: sessionmaker[Session], owner: str, name: str, number: int, limit: int = 200
) -> list[dict[str, Any]]:
    with session_scope(sessions) as session:
        item = find_pr_item(session, owner, name, number)
        if item is None:
            return []
        rows = session.scalars(
            select(ChatMessage)
            .where(ChatMessage.item_id == item.id)
            .order_by(ChatMessage.created_at, ChatMessage.role.desc())
            .limit(limit)
        )
        return [
            {
                "id": str(row.id),
                "role": row.role,
                "content": row.content,
                "references": list(row.refs or []),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
