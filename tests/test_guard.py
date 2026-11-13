"""The order guard hook: no automatic orders, read-only browser on broker pages."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

from _load import ROOT

HOOK = os.path.join(ROOT, "hooks", "order_guard.py")


def call(tool, tool_input=None, event="pre", session=None, response=None):
    payload = {"session_id": session or "s", "tool_name": tool, "tool_input": tool_input or {}}
    if response is not None:
        payload["tool_response"] = response
    out = subprocess.run([sys.executable, HOOK, event], input=json.dumps(payload), capture_output=True,
                         text=True, timeout=20)
    if not out.stdout.strip():
        return None
    return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]


class Guard(unittest.TestCase):
    def setUp(self):
        self.s = "test-" + uuid.uuid4().hex

    def tearDown(self):
        p = os.path.join(tempfile.gettempdir(), "b3-trader-guard", self.s + ".json")
        if os.path.exists(p):
            os.remove(p)

    def test_order_tools_are_denied(self):
        for tool in ("mcp__alpaca__place_stock_order", "mcp__alpaca__cancel_order_by_id",
                     "mcp__ibkr__close_all_positions", "mcp__x__submit_order", "mcp__x__buy_stock",
                     "mcp__x__trade", "mcp__xp__boleta", "mcp__x__sell"):
            self.assertEqual(call(tool, session=self.s), "deny", tool)

    def test_read_only_and_unrelated_tools_pass(self):
        for tool in ("mcp__alpaca__get_orders", "mcp__alpaca__get_all_positions", "mcp__x__list_orders",
                     "mcp__claude_ai_Google_Calendar__create_event", "mcp__plugin_vercel_vercel__buy_domain",
                     "mcp__claude_ai_Google_Drive__update_file"):
            self.assertIsNone(call(tool, session=self.s), tool)

    def test_browser_is_read_only_after_a_broker_page(self):
        click = {"action": "left_click", "coordinate": [10, 10]}
        self.assertIsNone(call("mcp__claude-in-chrome__computer", click, session=self.s))
        self.assertIsNone(call("mcp__claude-in-chrome__navigate", {"url": "https://portal.xpi.com.br/"},
                               session=self.s))
        self.assertEqual(call("mcp__claude-in-chrome__computer", click, session=self.s), "deny")
        self.assertEqual(call("mcp__claude-in-chrome__computer", {"action": "type", "text": "100"},
                              session=self.s), "deny")
        self.assertEqual(call("mcp__claude-in-chrome__form_input", {"ref": "a", "value": "1"}, session=self.s),
                         "deny")
        self.assertEqual(call("mcp__claude-in-chrome__javascript_tool", {"text": "x"}, session=self.s), "deny")
        for ok in ("get_page_text", "read_page", "find"):
            self.assertIsNone(call("mcp__claude-in-chrome__" + ok, {"tabId": 1}, session=self.s), ok)
        for action in ("screenshot", "scroll"):
            self.assertIsNone(call("mcp__claude-in-chrome__computer", {"action": action}, session=self.s))

    def test_a_broker_tab_seen_in_a_tab_list_counts(self):
        call("mcp__claude-in-chrome__tabs_context_mcp", {}, event="post", session=self.s,
             response="tab 7: https://www.rico.com.vc/home")
        self.assertEqual(call("mcp__claude-in-chrome__computer", {"action": "key", "text": "Return"},
                              session=self.s), "deny")

    def test_shell_calls_to_a_broker_are_denied(self):
        self.assertEqual(call("Bash", {"command": "curl -X POST https://api.xpi.com.br/orders"}, session=self.s),
                         "deny")
        self.assertIsNone(call("Bash", {"command": "echo 'see portal.xpi.com.br' >> notes.md"}, session=self.s))
        self.assertIsNone(call("Bash", {"command": "python3 trader-scanner/scan.py orb"}, session=self.s))

    def test_the_hook_is_wired_for_the_plugin_and_the_repo(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json")) as fh:
            plugin = json.load(fh)
        with open(os.path.join(ROOT, ".claude", "settings.json")) as fh:
            repo = json.load(fh)
        for cfg in (plugin, repo):
            pre = cfg["hooks"]["PreToolUse"][0]
            self.assertIn("mcp__", pre["matcher"])
            self.assertIn("Bash", pre["matcher"])
            self.assertIn("order_guard.py", pre["hooks"][0]["command"])
            self.assertIn("order_guard.py", cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
