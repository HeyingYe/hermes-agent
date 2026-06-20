import io
import json
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cli.jarvis import infer_profile_and_route, main, summarize_document


def test_route_chat_infers_profile_and_preserves_safety_boundary():
    routed = infer_profile_and_route("Summarize this PDF for tomorrow's exec meeting and send it to the team")

    assert routed.kind == "executive_brief"
    assert routed.safety == "requires-confirmation"
    labels = {signal.label for signal in routed.profile_signals}
    assert "executive_brief" in labels
    assert "external_action" in labels
    assert "stop and ask for confirmation" in routed.agent_prompt


def test_brief_cli_loads_json_items_and_sorts_focus(tmp_path: Path):
    items_path = tmp_path / "items.json"
    items_path.write_text(
        json.dumps(
            [
                {"title": "Low priority cleanup", "source": "manual", "priority": 4},
                {"title": "9am customer meeting", "source": "calendar", "priority": 1, "due": "09:00"},
                {"title": "Ship Jarvis MVP", "source": "kanban", "priority": 2},
            ]
        ),
        encoding="utf-8",
    )

    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["brief", "--items", str(items_path), "--date", "2026-06-16"]) == 0

    text = out.getvalue()
    assert "Morning Brief — 2026-06-16" in text
    assert "1. P1 9am customer meeting" in text
    assert "Ship Jarvis MVP" in text
    assert "No external systems were contacted" in text


def test_summarize_markdown_extracts_key_points_actions_and_risks(tmp_path: Path):
    doc = tmp_path / "strategy.md"
    doc.write_text(
        "Jarvis MVP plan. The product must provide one chat entry for tasks. "
        "Action: create morning brief preview before cron automation. "
        "Risk: external sends need explicit confirmation and permission boundaries. "
        "Next step: summarize PPT and PDF inputs locally.",
        encoding="utf-8",
    )

    summary = summarize_document(str(doc), max_points=3)

    assert summary.kind == "md"
    assert summary.word_count > 10
    assert any("chat entry" in point for point in summary.key_points)
    assert any("Action:" in item for item in summary.action_items)
    assert any("Risk:" in risk for risk in summary.risks)


def test_summarize_pptx_extracts_slide_text(tmp_path: Path):
    pptx = tmp_path / "deck.pptx"
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr(
            "ppt/slides/slide1.xml",
            """
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                   xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Jarvis executive summary</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
            </p:sld>
            """,
        )

    summary = summarize_document(str(pptx))

    assert summary.kind == "pptx"
    assert any("Jarvis executive summary" in point for point in summary.key_points)


def test_interactive_slash_command_routes_to_jarvis_handler():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {"quick_commands": {}}
    cli.console = MagicMock()
    cli.agent = None
    cli.conversation_history = []
    cli.session_id = "test-session"
    cli._pending_resume_sessions = None

    with patch.object(cli, "_handle_jarvis_command") as handler:
        assert cli.process_command('/jarvis route "Build MVP"') is True

    handler.assert_called_once_with('/jarvis route "Build MVP"')


def test_route_cli_json_output_is_structured():
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["route", "Build the Jarvis MVP", "--json"]) == 0

    payload = json.loads(out.getvalue())
    assert payload["kind"] in {"product_strategy", "engineering"}
    assert payload["safety"] == "local-dry-run-safe"


def test_chinese_external_send_phrase_requires_confirmation():
    routed = infer_profile_and_route("帮我总结这个PPT并发给团队")

    assert routed.kind == "executive_brief"
    assert routed.safety == "requires-confirmation"
    assert any(signal.label == "external_action" for signal in routed.profile_signals)
