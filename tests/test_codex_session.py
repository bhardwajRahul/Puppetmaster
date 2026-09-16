"""Hermetic protocol tests for the intentionally small Codex session transport."""
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from puppetmaster.adapters.codex_session import run_codex_session


FAKE = r'''
import json, os, sys, threading, time
mode=os.environ.get("FAKE_MODE","normal")
beta=[False]
def send(x): print(json.dumps(x), flush=True)
def finish(word):
  send({"jsonrpc":"2.0","method":"thread/tokenUsage/updated","params":{"threadId":"th","turnId":"one","tokenUsage":{"last":{"inputTokens":3,"outputTokens":5,"cachedInputTokens":1,"reasoningOutputTokens":0,"totalTokens":8},"total":{"inputTokens":7,"outputTokens":11,"cachedInputTokens":2,"reasoningOutputTokens":0,"totalTokens":18}}}})
  send({"jsonrpc":"2.0","method":"item/completed","params":{"item":{"type":"agentMessage","text":word}}})
  send({"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":"th","turnId":"one"}})
def finish_later():
  time.sleep(0.4)
  finish("BETA" if beta[0] else "ALPHA")
for raw in sys.stdin:
 x=json.loads(raw); m=x.get("method"); i=x.get("id")
 if m == "initialize": send({"jsonrpc":"2.0","id":i,"result":{}})
 elif m == "initialized": pass
 elif m == "thread/start": send({"jsonrpc":"2.0","id":i,"result":{"thread":{"id":"th"}}})
 elif m == "turn/start":
  send({"jsonrpc":"2.0","id":i,"result":{"turn":{"id":"one"}}})
  send({"jsonrpc":"2.0","method":"turn/started","params":{"threadId":"th","turnId":"one"}})
  if mode == "malformed": print("not json", flush=True)
  if mode == "stderr": sys.stderr.write("x"*200000);sys.stderr.flush()
  if mode == "timeout": time.sleep(30)
  if mode == "error": send({"jsonrpc":"2.0","method":"turn/failed","params":{"message":"bad"}}); continue
  if mode == "normal":
   threading.Thread(target=finish_later, daemon=True).start()
   continue
  finish("ALPHA")
 elif m == "turn/steer":
  beta[0]=True
  send({"jsonrpc":"2.0","id":i,"result":{"turnId":"one"}})
 elif m == "turn/interrupt": send({"jsonrpc":"2.0","id":i,"result":{}})
'''


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake.py"
        self.script.write_text(textwrap.dedent(FAKE))

    def tearDown(self): self.tmp.cleanup()

    def invoke(self, mode="normal", **kwargs):
        env = dict(kwargs.pop("env", {})); env["FAKE_MODE"] = mode
        return run_codex_session([sys.executable, str(self.script)], self.tmp.name, "hello", model="m", sandbox="read-only", env=env, **kwargs)

    def test_initialize_order_completion_usage_and_stderr_pressure(self):
        result = self.invoke("stderr")
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.input_tokens, result.output_tokens, result.cached_input_tokens), (7, 11, 2))
        self.assertIn('"method":"initialize"', result.raw_protocol)
        self.assertLess(result.raw_protocol.index('"method":"initialize"'), result.raw_protocol.index('"method":"thread/start"'))
        self.assertEqual(result.messages, ["ALPHA"])

    def test_steer_is_correlated_and_acceptance_is_not_model_claim(self):
        acknowledgements = []
        sent = [False]
        def pending():
            if sent[0]: return []
            sent[0] = True
            return [("mail-1", "final BETA", lambda *args: acknowledgements.append(args))]
        result = self.invoke(pending_steering=pending)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.turn_id, "one")
        self.assertEqual(result.messages, ["BETA"])
        self.assertEqual(result.accepted_steering, ["mail-1"])
        self.assertTrue(any(x[1] == "accepted" for x in acknowledgements))

    def test_error_malformed_timeout_and_cancellation_cleanup(self):
        self.assertEqual(self.invoke("error").status, "failed")
        malformed = self.invoke("malformed")
        self.assertEqual(malformed.status, "failed")
        self.assertIn("malformed", malformed.error)
        self.assertEqual(self.invoke("timeout", timeout=.15).status, "timeout")
        self.assertEqual(self.invoke("timeout", timeout=3, cancellation_check=lambda: True).status, "cancelled")

    @unittest.skipUnless(os.environ.get("PUPPETMASTER_LIVE_CODEX_STEERING") == "1", "opt-in live Codex smoke")
    def test_live_codex_same_turn_steering_smoke(self):
        """Run manually with a logged-in Codex CLI; never part of hermetic CI."""
        delivered = [False]
        def pending():
            if delivered[0]: return []
            delivered[0] = True
            return [("live-steer", "Change your final answer to exactly BETA.", lambda *_: None)]
        result = run_codex_session(
            [os.environ.get("CODEX_COMMAND", "codex")], self.tmp.name,
            "Run `pwd` only, then finish with exactly ALPHA.", model="gpt-5.6-luna",
            sandbox="read-only", timeout=120, pending_steering=pending,
        )
        self.assertEqual(result.status, "completed", result.error)
        self.assertIn("live-steer", result.accepted_steering)
        self.assertEqual(result.turn_id is not None, True)
        self.assertTrue(any(message.strip() == "BETA" for message in result.messages), result.messages)
        self.assertIsNotNone(result.input_tokens)
        self.assertIsNotNone(result.output_tokens)


if __name__ == "__main__": unittest.main()
