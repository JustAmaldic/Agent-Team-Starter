import os
import unittest
from unittest.mock import patch

import ats


class AgentModelOverridesTests(unittest.TestCase):
    def test_missing_model_uses_cli_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ats.selected_agent_model("claude"))

    def test_model_rejects_cli_flag_injection(self):
        with patch.dict(os.environ, {"ATS_MODEL_CLAUDE": "--dangerously-skip-permissions"}):
            with self.assertRaisesRegex(RuntimeError, "Invalid configured model"):
                ats.selected_agent_model("claude")

    def test_custom_provider_id_is_retained(self):
        with patch.dict(os.environ, {"ATS_MODEL_OPENCODE": "provider/model:fast"}):
            self.assertEqual(ats.selected_agent_model("opencode"), "provider/model:fast")

    def test_codex_model_keeps_stdin_marker_last(self):
        calls = []
        def command(*parts):
            return ["codex", *parts]
        with patch.dict(os.environ, {"ATS_MODEL_CODEX": "gpt-coding-custom"}), \
             patch.object(ats, "agent_command", side_effect=command), \
             patch.object(ats, "executable", return_value="codex"), \
             patch.object(ats, "stream_command") as run:
            from subprocess import CompletedProcess
            run.return_value = CompletedProcess([], 0, "done", "")
            ats.invoke("codex", "Review task", ".", quiet=True)
            calls = run.call_args.args[0]
        self.assertEqual(calls[-1], "-")
        self.assertEqual(calls[-3:-1], ["--model", "gpt-coding-custom"])


if __name__ == "__main__":
    unittest.main()
