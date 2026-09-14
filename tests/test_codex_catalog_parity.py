"""Shipped model catalogs must not advertise locally rejected OAuth workers."""
import unittest
from unittest import mock

from puppetmaster import openai_codex as codex, providers
from puppetmaster.static_catalog import curated_catalog


class CodexCatalogParityTests(unittest.TestCase):
    def test_all_shipped_codex_models_pass_wire_validation(self):
        native = curated_catalog("codex")
        oauth = [row for row in curated_catalog("agentic")
                 if (row.get("payload_defaults") or {}).get("provider") == "openai-codex"]
        self.assertTrue(native)
        self.assertTrue(oauth)
        for row in native + oauth:
            model = row["model"]
            with self.subTest(model=model):
                self.assertEqual(codex.harden_codex_model_id(model), (model, None))

    def test_astra_and_legacy_codex_preserve_exact_identity(self):
        for model in ("gpt-6-astra", "gpt-5.3-codex"):
            for prefix in ("", "openai-codex/", "agentic/openai-codex/", "openai-codex:"):
                with self.subTest(model=model, prefix=prefix):
                    self.assertEqual(codex.harden_codex_model_id(prefix + model), (model, None))

    def test_unknown_models_still_fail_closed(self):
        for model in ("gpt-6-imaginary", "gpt-5.6-jupiter", "claude-sonnet-5", "deepseek-v4-flash", ""):
            with self.subTest(model=model):
                with self.assertRaises(codex.UnknownCodexModelError):
                    codex.harden_codex_model_id(model)

    def test_other_provider_catalog_does_not_grant_oauth_support(self):
        with mock.patch("puppetmaster.static_catalog.curated_catalog", return_value=[
            {"model": "gpt-6-imaginary", "payload_defaults": {"provider": "opencode-go"}}
        ]):
            with self.assertRaises(codex.UnknownCodexModelError):
                codex.harden_codex_model_id("gpt-6-imaginary")

    def test_pro_alias_and_effort_are_preserved(self):
        result = codex.harden_agentic_openai_payload({
            "provider": "openai-codex", "model": "gpt-5.6-sol-pro", "reasoning_effort": "low",
        })
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(result["reasoning_effort"], "low")

    def test_gpt6_default_and_explicit_provider_identity(self):
        result = codex.harden_agentic_openai_payload({"model": "gpt-6-astra", "reasoning_effort": "low"})
        self.assertEqual(result["provider"], "openai-codex")
        self.assertEqual(result["model"], "gpt-6-astra")
        for provider in ("openai", "openai-api", "opencode-go"):
            payload = {"provider": provider, "model": "gpt-6-astra",
                       "pinned_model": "agentic/" + provider + "/gpt-6-astra"}
            self.assertEqual(codex.harden_agentic_openai_payload(payload), payload)
        for model in ("gpt-4o", "gpt-6fake", "claude-sonnet-5", ""):
            self.assertFalse(codex.is_codex_class_gpt_model(model))

    def test_astra_low_reaches_oauth_wire_unchanged(self):
        class FakeResponse:
            def __iter__(self):
                return iter([b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1},"output":[]}}\n'])
            def close(self):
                pass
        captured = {}
        def open_stream(url, *, headers, body, timeout):
            captured.update(body)
            return FakeResponse()
        with mock.patch.object(providers, "_open_stream", side_effect=open_stream):
            providers.provider_chat(provider="openai-codex", model="gpt-6-astra",
                                    messages=[{"role": "user", "content": "test"}],
                                    api_key="test-token", extra={"reasoning_effort": "low"})
        self.assertEqual(captured["model"], "gpt-6-astra")
        self.assertEqual(captured["reasoning"]["effort"], "low")


if __name__ == "__main__":
    unittest.main()
