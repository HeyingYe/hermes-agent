import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.registry import registry

# Trigger tool discovery so feishu tools get registered
feishu_drive_tool = importlib.import_module("tools.feishu_drive_tool")
importlib.import_module("tools.feishu_doc_tool")


class _FakeResponse:
    def __init__(self, code=0, msg="ok", data=None):
        self.code = code
        self.msg = msg
        payload = {"data": data or {}}
        self.raw = SimpleNamespace(content=json.dumps(payload).encode())


class _FakeClient:
    def __init__(self, response=None):
        self.response = response or _FakeResponse(data={"reply_id": "r1"})
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return self.response


class TestFeishuToolRegistration(unittest.TestCase):
    """Verify feishu tools are registered and have valid schemas."""

    EXPECTED_TOOLS = {
        "feishu_doc_read": "feishu_doc",
        "feishu_drive_list_comments": "feishu_drive",
        "feishu_drive_list_comment_replies": "feishu_drive",
        "feishu_drive_reply_comment": "feishu_drive",
        "feishu_drive_add_comment": "feishu_drive",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            self.assertIsNotNone(entry, f"{tool_name} not registered")
            self.assertEqual(entry.toolset, toolset)

    def test_schemas_have_required_fields(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            schema = entry.schema
            self.assertIn("name", schema)
            self.assertEqual(schema["name"], tool_name)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertIn("type", schema["parameters"])
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_handlers_are_callable(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            self.assertTrue(callable(entry.handler))

    def test_doc_read_schema_params(self):
        entry = registry.get_entry("feishu_doc_read")
        props = entry.schema["parameters"].get("properties", {})
        self.assertIn("doc_token", props)

    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name == "feishu_doc_read":
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            self.assertIn("file_token", props, f"{tool_name} missing file_token param")
            self.assertIn("file_type", props, f"{tool_name} missing file_type param")

    def test_reply_comment_builds_native_reply_request(self):
        fake_client = _FakeClient()
        feishu_drive_tool.set_client(fake_client)
        try:
            with patch.object(feishu_drive_tool, "_do_request", wraps=feishu_drive_tool._do_request) as wrapped:
                result = feishu_drive_tool._handle_reply_comment({
                    "file_token": "doc_token",
                    "comment_id": "comment_1",
                    "content": "收到 <OK> & 继续",
                    "file_type": "docx",
                })
        finally:
            feishu_drive_tool.set_client(None)

        payload = json.loads(result)
        self.assertEqual(payload["reply_id"], "r1")
        wrapped.assert_called_once()
        _client, method, uri = wrapped.call_args.args[:3]
        kwargs = wrapped.call_args.kwargs
        self.assertEqual(method, "POST")
        self.assertEqual(uri, "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies")
        self.assertEqual(kwargs["paths"], {"file_token": "doc_token", "comment_id": "comment_1"})
        self.assertIn(("file_type", "docx"), kwargs["queries"])
        self.assertEqual(
            kwargs["body"],
            {
                "content": {
                    "elements": [
                        {"type": "text_run", "text_run": {"text": "收到 &lt;OK&gt; &amp; 继续"}},
                    ]
                }
            },
        )

    def test_reply_comment_reports_feishu_error_code_for_fallback(self):
        fake_client = _FakeClient(response=_FakeResponse(code=1069302, msg="whole comment cannot reply"))
        feishu_drive_tool.set_client(fake_client)
        try:
            result = feishu_drive_tool._handle_reply_comment({
                "file_token": "doc_token",
                "comment_id": "comment_1",
                "content": "回复",
            })
        finally:
            feishu_drive_tool.set_client(None)

        payload = json.loads(result)
        self.assertIn("1069302", payload["error"])


if __name__ == "__main__":
    unittest.main()
