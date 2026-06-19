"""Local-verifiable Jarvis MVP helpers.

This module intentionally stays small and dependency-light.  It does not send
messages, mutate user state, or call external services; it only turns explicit
local inputs into structured prompts/summaries the user can inspect before any
future automation is enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

_PRIVACY_NOTICE = (
    "Privacy boundary: local dry-run only. Jarvis reads only files/JSON paths "
    "you explicitly pass, never sends outbound messages, and never writes to "
    "external systems from this command. Review output before wiring delivery "
    "or automation."
)

_DOC_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".csv", ".docx", ".xlsx", ".pptx", ".pdf"}
_STOPWORDS = {
    "about", "after", "again", "all", "also", "and", "any", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "for", "from", "had", "has", "have", "how", "into", "its", "more", "not", "now", "of",
    "on", "or", "our", "out", "over", "please", "the", "their", "them", "then", "there", "this", "to", "we",
    "what", "when", "where", "which", "who", "why", "will", "with", "you", "your", "a", "an", "in", "is", "it",
}


@dataclass(frozen=True)
class ProfileSignal:
    label: str
    score: int
    evidence: list[str]


@dataclass(frozen=True)
class RoutedTask:
    kind: str
    confidence: float
    safety: str
    profile_signals: list[ProfileSignal]
    suggested_next_step: str
    agent_prompt: str


@dataclass(frozen=True)
class BriefItem:
    title: str
    source: str
    priority: int = 3
    due: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class DocumentSummary:
    path: str
    title: str
    kind: str
    word_count: int
    summary: list[str]
    key_points: list[str]
    action_items: list[str]
    risks: list[str]
    evidence: list[str]


class JarvisError(RuntimeError):
    """User-correctable Jarvis command error."""


def privacy_notice() -> str:
    return _PRIVACY_NOTICE


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}|[\u4e00-\u9fff]{2,}", text)]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]


def _top_keywords(text: str, limit: int = 10) -> list[str]:
    counts = Counter(tok for tok in _tokenize(text) if tok not in _STOPWORDS and not tok.isdigit())
    return [word for word, _count in counts.most_common(limit)]


def _score_sentence(sentence: str, keywords: Iterable[str]) -> int:
    low = sentence.lower()
    score = sum(2 for kw in keywords if kw.lower() in low)
    score += min(len(sentence) // 80, 3)
    if re.search(r"\b(need|must|risk|block|deadline|urgent|decision|todo|action|required|关键|风险|必须|截止|待办)\b", low):
        score += 3
    return score


def infer_profile_and_route(message: str) -> RoutedTask:
    """Classify a chat message into a safe local Jarvis task route."""
    text = message.strip()
    if not text:
        raise JarvisError("message is required")

    dimensions: list[tuple[str, tuple[str, ...], str]] = [
        ("product_strategy", ("roadmap", "mvp", "priority", "feature", "产品", "需求", "路线图", "优先级"), "Frame this as product work with goals, users, constraints, and acceptance criteria."),
        ("engineering", ("bug", "code", "test", "deploy", "api", "repo", "debug", "实现", "代码", "测试", "修复"), "Treat this as an engineering task; inspect the codebase before changing anything."),
        ("executive_brief", ("summarize", "summary", "brief", "ppt", "pdf", "doc", "deck", "总结", "摘要", "汇报"), "Produce an executive summary with decisions, risks, and next actions."),
        ("schedule_focus", ("today", "tomorrow", "calendar", "meeting", "deadline", "remind", "今天", "明天", "会议", "日程"), "Turn this into a focus plan with explicit time/date assumptions."),
        ("external_action", ("send", "email", "post", "publish", "invite", "delete", "share", "发送", "发布", "删除", "邀请", "外发"), "Do not execute external side effects without explicit confirmation."),
    ]

    lowered = text.lower()
    signals: list[ProfileSignal] = []
    best_label = "general_chat"
    best_score = 0
    for label, keywords, _hint in dimensions:
        evidence = [kw for kw in keywords if kw.lower() in lowered]
        if evidence:
            score = len(evidence)
            signals.append(ProfileSignal(label, score, evidence[:5]))
            if score > best_score:
                best_label, best_score = label, score

    if best_score == 0:
        signals.append(ProfileSignal("general_chat", 1, ["no strong domain keyword matched"]))

    external = any(sig.label == "external_action" for sig in signals)
    safety = "requires-confirmation" if external else "local-dry-run-safe"
    confidence = min(0.95, 0.45 + best_score * 0.12)
    suggested = next((hint for label, _kw, hint in dimensions if label == best_label), "Answer directly, ask only for missing non-retrievable context.")

    profile_block = "; ".join(f"{sig.label}:{sig.score}({', '.join(sig.evidence)})" for sig in signals)
    prompt = (
        f"Jarvis routed task kind: {best_label}\n"
        f"Safety boundary: {safety}. If action would send/share/write/delete or change permissions, stop and ask for confirmation.\n"
        f"User/profile signals: {profile_block}\n"
        f"Suggested approach: {suggested}\n\n"
        f"User request:\n{text}"
    )
    return RoutedTask(best_label, round(confidence, 2), safety, signals, suggested, prompt)


def _load_items(path: str | None) -> list[BriefItem]:
    if not path:
        return []
    raw_path = Path(path).expanduser()
    if not raw_path.exists():
        raise JarvisError(f"brief input not found: {raw_path}")
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise JarvisError("brief input must be a JSON list of objects")
    items: list[BriefItem] = []
    for idx, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise JarvisError(f"brief item #{idx} must be an object")
        title = str(item.get("title") or item.get("summary") or "").strip()
        if not title:
            raise JarvisError(f"brief item #{idx} missing title")
        source = str(item.get("source") or "manual").strip()
        try:
            priority = int(item.get("priority", 3))
        except (TypeError, ValueError) as exc:
            raise JarvisError(f"brief item #{idx} priority must be an integer") from exc
        items.append(BriefItem(title=title, source=source, priority=priority, due=item.get("due"), notes=item.get("notes")))
    return items


def build_morning_brief(items: list[BriefItem], *, for_date: date | None = None) -> str:
    today = for_date or date.today()
    sorted_items = sorted(items, key=lambda it: (it.priority, it.due or "9999-99-99", it.title.lower()))
    urgent = [it for it in sorted_items if it.priority <= 2]
    meetings = [it for it in sorted_items if "calendar" in it.source.lower() or "meeting" in it.title.lower() or "会议" in it.title]
    focus = sorted_items[:3]

    lines = [privacy_notice(), "", f"Morning Brief — {today.isoformat()}"]
    if not items:
        lines.extend([
            "No local data source was provided.",
            "Today’s MVP-safe next step: pass a JSON list via --items to preview a real brief before enabling cron or platform delivery.",
        ])
        return "\n".join(lines)

    lines.append(f"Inputs: {len(items)} local item(s). No external systems were contacted.")
    lines.append("")
    lines.append("Today’s focus:")
    for idx, item in enumerate(focus, 1):
        due = f" due {item.due}" if item.due else ""
        lines.append(f"{idx}. P{item.priority} {item.title} ({item.source}{due})")
        if item.notes:
            lines.append(f"   - {item.notes}")
    lines.append("")
    lines.append("Urgent / needs attention:")
    if urgent:
        for item in urgent:
            lines.append(f"- {item.title} [{item.source}]" + (f" due {item.due}" if item.due else ""))
    else:
        lines.append("- None marked priority 1-2 in local inputs.")
    lines.append("")
    lines.append("Calendar-like items:")
    if meetings:
        for item in meetings[:5]:
            lines.append(f"- {item.title}" + (f" at/due {item.due}" if item.due else ""))
    else:
        lines.append("- No calendar-like local items provided.")
    return "\n".join(lines)


def _extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix not in _DOC_EXTENSIONS:
        raise JarvisError(f"unsupported document type {suffix!r}; supported: {', '.join(sorted(_DOC_EXTENSIONS))}")
    if suffix in {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".csv"}:
        return path.read_text(encoding="utf-8", errors="replace"), suffix.lstrip(".")
    if suffix in {".docx", ".xlsx"}:
        from tools.read_extract import extract_document_text
        return extract_document_text(str(path)), suffix.lstrip(".")
    if suffix == ".pptx":
        return _extract_pptx_text(path), "pptx"
    if suffix == ".pdf":
        return _extract_pdf_text(path), "pdf"
    raise JarvisError(f"unsupported document type: {suffix}")


def _extract_pptx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(name for name in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name))
            if not names:
                raise JarvisError("PPTX contains no slide XML")
            out: list[str] = []
            for idx, name in enumerate(names, 1):
                root = ET.fromstring(zf.read(name))
                texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
                slide_text = " ".join(t.strip() for t in texts if t.strip())
                if slide_text:
                    out.append(f"Slide {idx}: {slide_text}")
            if not out:
                raise JarvisError("PPTX contains no extractable text")
            return "\n".join(out) + "\n"
    except zipfile.BadZipFile as exc:
        raise JarvisError(f"not a valid PPTX: {path}") from exc
    except ET.ParseError as exc:
        raise JarvisError(f"malformed PPTX XML: {exc}") from exc


def _extract_pdf_text(path: Path) -> str:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception:
        try:
            import fitz as pymupdf  # type: ignore[import-not-found,no-redef]
        except Exception as exc:
            raise JarvisError("PDF extraction requires pymupdf/fitz; install pymupdf or pass text/docx/pptx for the local MVP") from exc
    try:
        doc = pymupdf.open(str(path))
        return "\n".join(page.get_text("text") for page in doc).strip() + "\n"
    except Exception as exc:
        raise JarvisError(f"failed to extract PDF text: {exc}") from exc


def summarize_document(path: str, *, max_points: int = 5) -> DocumentSummary:
    raw_path = Path(path).expanduser()
    if not raw_path.exists() or not raw_path.is_file():
        raise JarvisError(f"document not found: {raw_path}")
    text, kind = _extract_text(raw_path)
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        raise JarvisError("document contains no extractable text")
    sentences = _sentences(text)
    keywords = _top_keywords(text, 12)
    ranked = sorted(sentences, key=lambda s: _score_sentence(s, keywords), reverse=True)
    key_points = ranked[:max_points] or [compact[:240]]
    action_items = [s for s in sentences if re.search(r"\b(todo|action|next step|owner|must|need|required|deadline|待办|行动|负责人|必须|截止)\b", s, re.I)][:max_points]
    risks = [s for s in sentences if re.search(r"\b(risk|blocker|issue|concern|dependency|fail|风险|阻塞|问题|依赖)\b", s, re.I)][:max_points]
    word_count = len(_tokenize(text))
    title = sentences[0][:100] if sentences else raw_path.stem
    summary = [
        f"This {kind.upper()} has about {word_count} keyword/token(s) and centers on: {', '.join(keywords[:6]) or 'no dominant keywords'}.",
        "Top evidence-weighted points are extracted locally; review before using externally.",
    ]
    evidence = [point[:280] for point in key_points[:3]]
    return DocumentSummary(str(raw_path), title, kind, word_count, summary, key_points, action_items, risks, evidence)


def _cmd_route(args: argparse.Namespace) -> int:
    routed = infer_profile_and_route(args.message)
    if args.json:
        print(json.dumps(asdict(routed), ensure_ascii=False, indent=2))
    else:
        print(privacy_notice())
        print(f"Kind: {routed.kind}  confidence={routed.confidence}  safety={routed.safety}")
        print("Signals:")
        for sig in routed.profile_signals:
            print(f"- {sig.label}: score={sig.score}; evidence={', '.join(sig.evidence)}")
        print("Suggested next step:")
        print(routed.suggested_next_step)
        print("\nAgent prompt:")
        print(routed.agent_prompt)
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    items = _load_items(args.items)
    brief_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    print(build_morning_brief(items, for_date=brief_date))
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    summary = summarize_document(args.path, max_points=args.max_points)
    if args.json:
        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    else:
        print(privacy_notice())
        print(f"Executive Summary — {summary.path}")
        print(f"Type: {summary.kind.upper()}  words/tokens: {summary.word_count}")
        print("\nSummary:")
        for line in summary.summary:
            print(f"- {line}")
        print("\nKey points:")
        for point in summary.key_points:
            print(f"- {point}")
        print("\nAction items:")
        for item in summary.action_items or ["None detected locally."]:
            print(f"- {item}")
        print("\nRisks / blockers:")
        for risk in summary.risks or ["None detected locally."]:
            print(f"- {risk}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes jarvis", description="Jarvis P0 local MVP utilities")
    parser.set_defaults(func=lambda _args: (parser.print_help(), 0)[1])
    sub = parser.add_subparsers(dest="jarvis_command")

    route = sub.add_parser("route", help="Classify a chat request and build a safe agent prompt")
    route.add_argument("message", help="User chat message to classify")
    route.add_argument("--json", action="store_true", help="Emit structured JSON")
    route.set_defaults(func=_guarded(_cmd_route))

    brief = sub.add_parser("brief", help="Render a local dry-run morning brief from optional JSON items")
    brief.add_argument("--items", help="Path to JSON list with title/source/priority/due/notes fields")
    brief.add_argument("--date", help="Override date as YYYY-MM-DD for reproducible output")
    brief.set_defaults(func=_guarded(_cmd_brief))

    summ = sub.add_parser("summarize", help="Create an executive summary for a local document")
    summ.add_argument("path", help="Local document path (.txt/.md/.docx/.xlsx/.pptx/.pdf)")
    summ.add_argument("--max-points", type=int, default=5, help="Maximum points/items per section")
    summ.add_argument("--json", action="store_true", help="Emit structured JSON")
    summ.set_defaults(func=_guarded(_cmd_summarize))
    return parser


def cmd_jarvis(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        build_parser().print_help()
        return 0
    try:
        return int(func(args) or 0)
    except JarvisError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return 2


def register_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "jarvis",
        help="Jarvis P0 local MVP: route chat, preview morning brief, summarize docs",
        description=(
            "Local-verifiable Jarvis MVP utilities. Privacy-first: no external sends, "
            "no permission changes, and no automation wiring from this command."
        ),
    )
    sub = parser.add_subparsers(dest="jarvis_command")

    route = sub.add_parser("route", help="Classify a chat request and build a safe agent prompt")
    route.add_argument("message", help="User chat message to classify")
    route.add_argument("--json", action="store_true", help="Emit structured JSON")
    route.set_defaults(func=_guarded(_cmd_route))

    brief = sub.add_parser("brief", help="Render a local dry-run morning brief from optional JSON items")
    brief.add_argument("--items", help="Path to JSON list with title/source/priority/due/notes fields")
    brief.add_argument("--date", help="Override date as YYYY-MM-DD for reproducible output")
    brief.set_defaults(func=_guarded(_cmd_brief))

    summ = sub.add_parser("summarize", help="Create an executive summary for a local document")
    summ.add_argument("path", help="Local document path (.txt/.md/.docx/.xlsx/.pptx/.pdf)")
    summ.add_argument("--max-points", type=int, default=5, help="Maximum points/items per section")
    summ.add_argument("--json", action="store_true", help="Emit structured JSON")
    summ.set_defaults(func=_guarded(_cmd_summarize))
    parser.set_defaults(func=cmd_jarvis)


def _guarded(func):
    def _run(args: argparse.Namespace) -> int:
        try:
            return int(func(args) or 0)
        except JarvisError as exc:
            print(f"jarvis: {exc}", file=sys.stderr)
            return 2
    return _run


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except JarvisError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
