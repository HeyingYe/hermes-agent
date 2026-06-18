"""Tests for feishu_comment — event filtering, access control integration, wiki reverse lookup."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway.platforms.feishu_comment import (
    parse_drive_comment_event,
    _ALLOWED_NOTICE_TYPES,
    _sanitize_comment_text,
)


class TestCommentAgentRuntime(unittest.TestCase):
    @patch("tools.feishu_drive_tool.set_client")
    @patch("tools.feishu_doc_tool.set_client")
    @patch("run_agent.AIAgent")
    @patch("gateway.platforms.feishu_comment._resolve_fallback_model")
    @patch("gateway.platforms.feishu_comment._resolve_model_and_runtime")
    def test_comment_agent_passes_fallback_chain(
        self, mock_runtime, mock_fallback, mock_agent_cls, mock_set_doc, mock_set_drive,
    ):
        from gateway.platforms.feishu_comment import _run_comment_agent

        fallback = [{"provider": "custom:subcode", "model": "gpt-5.5"}]
        mock_runtime.return_value = ("claude-opus-4-8", {"provider": "anthropic", "api_key": "k"})
        mock_fallback.return_value = fallback
        mock_agent = Mock()
        mock_agent.run_conversation.return_value = {"final_response": "ok", "api_calls": 1, "messages": []}
        mock_agent_cls.return_value = mock_agent

        response = _run_comment_agent("prompt", Mock(), "comment-doc:docx:d1")

        self.assertEqual(response, "ok")
        self.assertEqual(mock_agent_cls.call_args.kwargs["fallback_model"], fallback)


class TestInternalApiErrorFiltering(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("gateway.platforms.feishu_comment.delete_comment_reaction", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.deliver_comment_reply", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment._run_comment_agent")
    @patch("gateway.platforms.feishu_comment.list_comment_replies", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.batch_query_comment", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.query_document_meta", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.add_comment_reaction", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=True)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_internal_api_error_response_is_not_delivered_to_comment(
        self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys,
        mock_reaction, mock_meta, mock_batch, mock_replies, mock_agent,
        mock_deliver, mock_delete_reaction,
    ):
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(True, "pairing", frozenset(), "top")
        mock_load.return_value = Mock()
        mock_meta.return_value = {"title": "Doc", "url": "https://example/docx/d1"}
        mock_batch.return_value = {"is_whole": False, "quote": "quoted"}
        mock_replies.return_value = [
            {"reply_id": "r1", "user_id": "ou_user", "content": {"elements": [{"type": "text_run", "text_run": {"text": "@Jarvis why"}}]}},
        ]
        mock_agent.return_value = "API call failed after 3 retries: HTTP 429: monthly spend limit"

        self._run(handle_drive_comment_event(Mock(), _make_event(), self_open_id="ou_bot"))

        mock_deliver.assert_not_called()


def _make_event(
    comment_id="c1",
    reply_id="r1",
    notice_type="add_reply",
    file_token="docx_token",
    file_type="docx",
    from_open_id="ou_user",
    to_open_id="ou_bot",
    is_mentioned=True,
):
    """Build a minimal drive comment event SimpleNamespace."""
    return SimpleNamespace(event={
        "event_id": "evt_1",
        "comment_id": comment_id,
        "reply_id": reply_id,
        "is_mentioned": is_mentioned,
        "timestamp": "1713200000",
        "notice_meta": {
            "file_token": file_token,
            "file_type": file_type,
            "notice_type": notice_type,
            "from_user_id": {"open_id": from_open_id},
            "to_user_id": {"open_id": to_open_id},
        },
    })


class TestParseEvent(unittest.TestCase):
    def test_parse_valid_event(self):
        evt = _make_event()
        parsed = parse_drive_comment_event(evt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["comment_id"], "c1")
        self.assertEqual(parsed["file_type"], "docx")
        self.assertEqual(parsed["from_open_id"], "ou_user")
        self.assertEqual(parsed["to_open_id"], "ou_bot")

    def test_parse_missing_event_attr(self):
        self.assertIsNone(parse_drive_comment_event(object()))

    def test_parse_none_event(self):
        self.assertIsNone(parse_drive_comment_event(SimpleNamespace()))


class TestEventFiltering(unittest.TestCase):
    """Test the filtering logic in handle_drive_comment_event."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("gateway.platforms.feishu_comment_rules.load_config")
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed")
    def test_self_reply_filtered(self, mock_allowed, mock_resolve, mock_load):
        """Events where from_open_id == self_open_id should be dropped."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event

        evt = _make_event(from_open_id="ou_bot", to_open_id="ou_bot")
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        mock_load.assert_not_called()

    @patch("gateway.platforms.feishu_comment_rules.load_config")
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed")
    def test_wrong_receiver_filtered(self, mock_allowed, mock_resolve, mock_load):
        """Events where to_open_id != self_open_id should be dropped."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event

        evt = _make_event(to_open_id="ou_other_bot")
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        mock_load.assert_not_called()

    @patch("gateway.platforms.feishu_comment_rules.load_config")
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed")
    def test_empty_to_open_id_filtered(self, mock_allowed, mock_resolve, mock_load):
        """Events with empty to_open_id should be dropped."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event

        evt = _make_event(to_open_id="")
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        mock_load.assert_not_called()

    @patch("gateway.platforms.feishu_comment_rules.load_config")
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed")
    def test_invalid_notice_type_filtered(self, mock_allowed, mock_resolve, mock_load):
        """Events with unsupported notice_type should be dropped."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event

        evt = _make_event(notice_type="resolve_comment")
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        mock_load.assert_not_called()

    def test_allowed_notice_types(self):
        self.assertIn("add_comment", _ALLOWED_NOTICE_TYPES)
        self.assertIn("add_reply", _ALLOWED_NOTICE_TYPES)
        self.assertNotIn("resolve_comment", _ALLOWED_NOTICE_TYPES)


class TestAccessControlIntegration(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_denied_user_no_side_effects(self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys):
        """Denied user should not trigger typing reaction or agent."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(True, "allowlist", frozenset(), "top")
        mock_load.return_value = Mock()

        client = Mock()
        evt = _make_event()
        self._run(handle_drive_comment_event(client, evt, self_open_id="ou_bot"))

        # No API calls should be made for denied users
        client.request.assert_not_called()

    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_disabled_comment_skipped(self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys):
        """Disabled comments should return immediately."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(False, "allowlist", frozenset(), "top")
        mock_load.return_value = Mock()

        evt = _make_event()
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        mock_allowed.assert_not_called()


class TestMentionGate(unittest.TestCase):
    """The @-mention gate: only reply when is_mentioned, unless require_mention=false."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=True)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_non_mentioned_dropped_before_access_control(
        self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys,
    ):
        """require_mention=true + is_mentioned=false → drop before access-control and any API call."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(
            True, "pairing", frozenset(), "top", require_mention=True,
        )
        mock_load.return_value = Mock()

        client = Mock()
        evt = _make_event(is_mentioned=False)
        self._run(handle_drive_comment_event(client, evt, self_open_id="ou_bot"))

        mock_allowed.assert_not_called()
        client.request.assert_not_called()

    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_mentioned_proceeds_to_access_control(
        self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys,
    ):
        """require_mention=true + is_mentioned=true → mention gate passes, access-control runs."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(
            True, "pairing", frozenset(), "top", require_mention=True,
        )
        mock_load.return_value = Mock()

        evt = _make_event(is_mentioned=True)
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))

        mock_allowed.assert_called_once()

    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=False)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    def test_require_mention_false_allows_non_mentioned(
        self, mock_load, mock_resolve, mock_allowed, mock_wiki_keys,
    ):
        """require_mention=false for an exact doc → non-mentioned event still reaches access-control."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        mock_resolve.return_value = ResolvedCommentRule(
            True, "pairing", frozenset(), "exact:docx:docx_token", require_mention=False,
        )
        mock_load.return_value = Mock()

        evt = _make_event(is_mentioned=False)
        self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))

        mock_allowed.assert_called_once()


class TestSanitizeCommentText(unittest.TestCase):
    def test_angle_brackets_escaped(self):
        self.assertEqual(_sanitize_comment_text("List<String>"), "List&lt;String&gt;")

    def test_ampersand_escaped_first(self):
        self.assertEqual(_sanitize_comment_text("a & b"), "a &amp; b")

    def test_ampersand_not_double_escaped(self):
        result = _sanitize_comment_text("a < b & c > d")
        self.assertEqual(result, "a &lt; b &amp; c &gt; d")
        self.assertNotIn("&amp;lt;", result)
        self.assertNotIn("&amp;gt;", result)

    def test_plain_text_unchanged(self):
        self.assertEqual(_sanitize_comment_text("hello world"), "hello world")

    def test_empty_string(self):
        self.assertEqual(_sanitize_comment_text(""), "")

    def test_code_snippet(self):
        text = 'if (a < b && c > 0) { return "ok"; }'
        result = _sanitize_comment_text(text)
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertIn("&lt;", result)
        self.assertIn("&gt;", result)


class TestWikiReverseLookup(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("gateway.platforms.feishu_comment._exec_request")
    def test_reverse_lookup_success(self, mock_exec):
        from gateway.platforms.feishu_comment import _reverse_lookup_wiki_token

        mock_exec.return_value = (0, "Success", {
            "node": {"node_token": "WIKI_TOKEN_123", "obj_token": "docx_abc"},
        })
        result = self._run(_reverse_lookup_wiki_token(Mock(), "docx", "docx_abc"))
        self.assertEqual(result, "WIKI_TOKEN_123")
        # Verify correct API params
        call_args = mock_exec.call_args
        queries = call_args[1].get("queries") or call_args[0][3]
        query_dict = dict(queries)
        self.assertEqual(query_dict["token"], "docx_abc")
        self.assertEqual(query_dict["obj_type"], "docx")

    @patch("gateway.platforms.feishu_comment._exec_request")
    def test_reverse_lookup_not_wiki(self, mock_exec):
        from gateway.platforms.feishu_comment import _reverse_lookup_wiki_token

        mock_exec.return_value = (131001, "not found", {})
        result = self._run(_reverse_lookup_wiki_token(Mock(), "docx", "docx_abc"))
        self.assertIsNone(result)

    @patch("gateway.platforms.feishu_comment._exec_request")
    def test_reverse_lookup_service_error(self, mock_exec):
        from gateway.platforms.feishu_comment import _reverse_lookup_wiki_token

        mock_exec.return_value = (500, "internal error", {})
        result = self._run(_reverse_lookup_wiki_token(Mock(), "docx", "docx_abc"))
        self.assertIsNone(result)

    @patch("gateway.platforms.feishu_comment._reverse_lookup_wiki_token", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment_rules.has_wiki_keys", return_value=True)
    @patch("gateway.platforms.feishu_comment_rules.is_user_allowed", return_value=True)
    @patch("gateway.platforms.feishu_comment_rules.resolve_rule")
    @patch("gateway.platforms.feishu_comment_rules.load_config")
    @patch("gateway.platforms.feishu_comment.add_comment_reaction", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.batch_query_comment", new_callable=AsyncMock)
    @patch("gateway.platforms.feishu_comment.query_document_meta", new_callable=AsyncMock)
    def test_wiki_lookup_triggered_when_no_exact_match(
        self, mock_meta, mock_batch, mock_reaction,
        mock_load, mock_resolve, mock_allowed, mock_wiki_keys, mock_lookup,
    ):
        """Wiki reverse lookup should fire when rule falls to wildcard/top and wiki keys exist."""
        from gateway.platforms.feishu_comment import handle_drive_comment_event
        from gateway.platforms.feishu_comment_rules import ResolvedCommentRule

        # First resolve returns wildcard (no exact match), second returns exact wiki match
        mock_resolve.side_effect = [
            ResolvedCommentRule(True, "allowlist", frozenset(), "wildcard"),
            ResolvedCommentRule(True, "allowlist", frozenset(), "exact:wiki:WIKI123"),
        ]
        mock_load.return_value = Mock()
        mock_lookup.return_value = "WIKI123"
        mock_meta.return_value = {"title": "Test", "url": ""}
        mock_batch.return_value = {"is_whole": False, "quote": ""}

        evt = _make_event()
        # Will proceed past access control but fail later — that's OK, we just test the lookup
        try:
            self._run(handle_drive_comment_event(Mock(), evt, self_open_id="ou_bot"))
        except Exception:
            pass

        mock_lookup.assert_called_once_with(unittest.mock.ANY, "docx", "docx_token")
        self.assertEqual(mock_resolve.call_count, 2)
        # Second call should include wiki_token
        second_call_kwargs = mock_resolve.call_args_list[1]
        self.assertEqual(second_call_kwargs[1].get("wiki_token") or second_call_kwargs[0][3], "WIKI123")


if __name__ == "__main__":
    unittest.main()
