"""Harden: openai-codex *-pro remap, refuse openai-api, 400 => FAILED, alerts body."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class CodexProRemapTests(unittest.TestCase):
    def test_openai_api_identity_provider_segments(self) -> None:
        from puppetmaster.openai_codex import openai_api_identity_provider

        self.assertEqual(
            openai_api_identity_provider("agentic/openai/gpt-5-6-sol"),
            "openai",
        )
        self.assertEqual(
            openai_api_identity_provider("agentic/openai-api/gpt-5-6-sol"),
            "openai-api",
        )
        self.assertIsNone(
            openai_api_identity_provider("agentic/openai-codex/gpt-5.6-luna")
        )
        self.assertIsNone(openai_api_identity_provider("agentic/gpt-5.6-luna"))
        self.assertIsNone(openai_api_identity_provider("gpt-5.6-sol"))

    def test_remap_luna_sol_terra_pro_to_base(self) -> None:
        from puppetmaster.openai_codex import harden_codex_model_id, remap_codex_pro_model

        for tier in ("luna", "sol", "terra"):
            raw = f"gpt-5.6-{tier}-pro"
            wire, from_id = remap_codex_pro_model(raw)
            self.assertEqual(wire, f"gpt-5.6-{tier}")
            self.assertEqual(from_id, raw)
            hardened, remapped = harden_codex_model_id(raw)
            self.assertEqual(hardened, f"gpt-5.6-{tier}")
            self.assertEqual(remapped, raw)

    def test_harden_rejects_unknown_codex_model(self) -> None:
        from puppetmaster.openai_codex import UnknownCodexModelError, harden_codex_model_id

        with self.assertRaises(UnknownCodexModelError) as ctx:
            harden_codex_model_id("gpt-5.6-jupiter")
        self.assertIn("not supported", str(ctx.exception))

    def test_agentic_pin_remaps_pro_keeps_reasoning_effort(self) -> None:
        from puppetmaster.model_registry import ModelSpec, apply_agentic_model_pin

        registry = [
            ModelSpec(
                id="agentic/openai-codex/gpt-5.6-luna",
                adapter="agentic",
                adapter_model_name="gpt-5.6-luna",
                enabled=True,
                payload_defaults={"provider": "openai-codex"},
            )
        ]
        stamped = apply_agentic_model_pin(
            {"mode": "implement", "reasoning_effort": "max"},
            "gpt-5.6-luna-pro",
            registry=registry,
        )
        self.assertEqual(stamped["model"], "gpt-5.6-luna")
        self.assertEqual(stamped["provider"], "openai-codex")
        self.assertEqual(stamped["reasoning_effort"], "max")
        self.assertEqual(stamped.get("codex_pro_remapped_from"), "gpt-5.6-luna-pro")

    def test_agentic_pin_refuses_openai_api_for_gpt56(self) -> None:
        from puppetmaster.model_registry import ModelSpec, apply_agentic_model_pin

        registry = [
            ModelSpec(
                id="agentic/gpt-5.6-luna",
                adapter="agentic",
                adapter_model_name="gpt-5.6-luna",
                enabled=True,
                payload_defaults={"provider": "openai-api"},
            )
        ]
        stamped = apply_agentic_model_pin(
            {"mode": "implement", "reasoning_effort": "max"},
            "gpt-5.6-luna",
            registry=registry,
        )
        self.assertEqual(stamped["provider"], "openai-codex")
        self.assertEqual(stamped["model"], "gpt-5.6-luna")
        self.assertEqual(stamped["reasoning_effort"], "max")

    def test_exact_openai_identity_survives_agentic_pin(self) -> None:
        from puppetmaster.model_registry import ModelSpec, apply_agentic_model_pin

        for provider, registry_id in (
            ("openai", "agentic/openai/gpt-5-6-sol"),
            ("openai-api", "agentic/openai-api/gpt-5-6-sol"),
        ):
            registry = [
                ModelSpec(
                    id=registry_id,
                    adapter="agentic",
                    adapter_model_name="gpt-5.6-sol",
                    enabled=True,
                    billing="api",
                    payload_defaults={"provider": provider},
                )
            ]
            stamped = apply_agentic_model_pin(
                {"mode": "analyze", "provider": provider},
                registry_id,
                registry=registry,
            )
            self.assertEqual(stamped["provider"], provider)
            self.assertEqual(stamped["model"], "gpt-5.6-sol")
            self.assertEqual(stamped["pinned_model"], registry_id)
            self.assertEqual(stamped["billing"], "api")

    def test_exact_openai_identity_survives_durable_bind(self) -> None:
        from puppetmaster.model_registry import ModelSpec, save_registry
        from puppetmaster.routing_authority import resolve_and_bind_explicit_pin

        registry_id = "agentic/openai/gpt-5-6-sol"
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "models.json"
            save_registry(
                [
                    ModelSpec(
                        id=registry_id,
                        adapter="agentic",
                        adapter_model_name="gpt-5.6-sol",
                        enabled=True,
                        billing="api",
                        payload_defaults={"provider": "openai"},
                    )
                ],
                registry_path,
            )
            payload = resolve_and_bind_explicit_pin(
                {"model": registry_id, "provider": "openai", "mode": "analyze"},
                adapter="agentic",
                registry_path=registry_path,
            )
        self.assertEqual(payload["provider"], "openai")
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["pinned_model"], registry_id)
        self.assertEqual(payload["billing"], "api")

    def test_wire_path_remaps_pro_before_codex_request(self) -> None:
        from puppetmaster import providers

        captured: dict = {}
        events = [
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "output": [{
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }],
                },
            },
        ]

        def fake_open_stream(url, *, headers, body, timeout):
            captured.update(url=url, headers=headers, body=body, timeout=timeout)
            lines = [f"data: {json.dumps(event)}\n".encode("utf-8") for event in events]

            class FakeResp:
                def __iter__(self):
                    return iter(lines)

                def close(self):
                    pass

            return FakeResp()

        with mock.patch.object(providers, "_open_stream", side_effect=fake_open_stream):
            providers.provider_chat(
                provider="openai-codex",
                model="gpt-5.6-luna-pro",
                messages=[{"role": "user", "content": "hi"}],
                extra={"reasoning_effort": "max"},
                api_key="tok-test",
            )
        self.assertEqual(captured["body"]["model"], "gpt-5.6-luna")
        self.assertEqual(
            captured["body"].get("reasoning"),
            {"effort": "max", "summary": "auto"},
        )

    def test_wire_path_rejects_unknown_model(self) -> None:
        from puppetmaster import providers
        from puppetmaster.providers import ProviderError

        with self.assertRaises(ProviderError) as ctx:
            providers.provider_chat(
                provider="openai-codex",
                model="gpt-99-nope",
                messages=[{"role": "user", "content": "hi"}],
                api_key="tok-test",
            )
        self.assertEqual(ctx.exception.reason, "unsupported_model")
        self.assertEqual(ctx.exception.failure, "model_unavailable")


class FailedVerificationStatusTests(unittest.TestCase):
    def test_http_400_verification_marks_task_failed(self) -> None:
        from puppetmaster.models import (
            AgentRun,
            Artifact,
            ArtifactType,
            JobStatus,
            Task,
            TaskStatus,
        )
        from puppetmaster.store import SwarmStore
        from puppetmaster.worker_runtime import WorkerRuntime

        body = (
            '{"detail":"The \'gpt-5.6-luna-pro\' model is not supported when '
            'using Codex with a ChatGPT account."}'
        )
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            job = store.create_job("harden")
            store.update_job_status(job.id, JobStatus.RUNNING)
            task = Task(
                job_id=job.id,
                role="implement",
                instruction="do",
                adapter="agentic",
                status=TaskStatus.QUEUED,
            )
            store.save_task(task)

            class _FakeWorker:
                def __init__(self, role, worker_id=None):
                    self.role = role

                def run(self, t, goal):
                    run = AgentRun(
                        job_id=t.job_id,
                        task_id=t.id,
                        role=t.role,
                        worker_id="w",
                        status=TaskStatus.COMPLETE,
                    )
                    art = Artifact(
                        job_id=t.job_id,
                        task_id=t.id,
                        type=ArtifactType.VERIFICATION,
                        created_by="w",
                        confidence=0.55,
                        evidence=["adapter:agentic", "http_status:400"],
                        payload={
                            "check": "x",
                            "result": "failed",
                            "failure": "unknown",
                            "http_status": 400,
                            "provider_reason": "http_status:400",
                            "stderr": body,
                            "provider_body": body,
                            "execution_status": "failed",
                        },
                    )
                    return run, [art]

            runtime = WorkerRuntime(
                store=store, job_id=job.id, role="implement", worker_id="w"
            )
            with mock.patch("puppetmaster.worker_runtime.LocalWorker", _FakeWorker):
                runtime.run_once()
            self.assertEqual(store.get_task_by_id(task.id).status, TaskStatus.FAILED)

    def test_all_http_400_tasks_make_job_failed(self) -> None:
        from puppetmaster.models import JobStatus, Task, TaskStatus
        from puppetmaster.orchestrator import Orchestrator
        from puppetmaster.store import SwarmStore

        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            job = store.create_job("all 400")
            for role in ("explore", "implement"):
                store.save_task(
                    Task(
                        job_id=job.id,
                        role=role,
                        instruction="x",
                        adapter="agentic",
                        status=TaskStatus.FAILED,
                    )
                )
            orch = Orchestrator(store)
            self.assertEqual(orch._final_job_status(job), JobStatus.FAILED)


class AlertsBodyExcerptTests(unittest.TestCase):
    def test_alerts_include_provider_body_excerpt(self) -> None:
        from puppetmaster.models import Artifact, ArtifactType
        from puppetmaster.stitcher import Stitcher

        body = (
            '{"detail":"The \'gpt-5.6-luna-pro\' model is not supported when '
            'using Codex with a ChatGPT account."}'
        )
        art = Artifact(
            job_id="j",
            task_id="t",
            type=ArtifactType.VERIFICATION,
            created_by="w",
            confidence=0.55,
            evidence=["adapter:agentic"],
            payload={
                "check": "x",
                "result": "failed",
                "failure": "model_unavailable",
                "adapter": "agentic",
                "http_status": 400,
                "provider_body": body,
                "stderr": body,
            },
        )
        summary = Stitcher(None)._render_summary("T", "goal", [art], [])
        self.assertIn("## Alerts (action required)", summary)
        self.assertIn("provider_body:", summary)
        self.assertIn("gpt-5.6-luna-pro", summary)
        self.assertIn("not supported", summary)


class AgenticResolveProviderTests(unittest.TestCase):
    def test_resolve_provider_forces_codex_for_gpt56(self) -> None:
        from puppetmaster.adapters.agentic import AgenticAdapter
        from puppetmaster.models import Task

        adapter = AgenticAdapter()
        task = Task(
            job_id="j",
            id="t",
            role="implement",
            instruction="x",
            adapter="agentic",
            payload={"provider": "openai-api", "model": "gpt-5.6-luna"},
        )
        self.assertEqual(adapter._resolve_provider(task), "openai-codex")

    def test_resolve_provider_keeps_exact_openai_identity(self) -> None:
        from puppetmaster.adapters.agentic import AgenticAdapter
        from puppetmaster.models import Task

        adapter = AgenticAdapter()
        for provider, pinned in (
            ("openai", "agentic/openai/gpt-5-6-sol"),
            ("openai-api", "agentic/openai-api/gpt-5-6-sol"),
        ):
            task = Task(
                job_id="j",
                id="t",
                role="analyze",
                instruction="x",
                adapter="agentic",
                payload={
                    "provider": provider,
                    "model": "gpt-5.6-sol",
                    "pinned_model": pinned,
                    "router_model_id": pinned,
                },
            )
            self.assertEqual(adapter._resolve_provider(task), provider)


class FailPayloadPersistsBodyTests(unittest.TestCase):
    def test_fail_persists_http_status_and_provider_body(self) -> None:
        from puppetmaster.adapters.agentic import AgenticAdapter
        from puppetmaster.models import Task

        adapter = AgenticAdapter()
        task = Task(
            job_id="j", id="t", role="implement", instruction="x", adapter="agentic"
        )
        body = '{"detail":"The \'gpt-5.6-luna-pro\' model is not supported"}'
        art = adapter._fail(
            task,
            "w",
            ["adapter:agentic"],
            "model_unavailable",
            body,
            status=400,
            provider_reason="http_status:400",
        )
        self.assertEqual(art.payload["result"], "failed")
        self.assertEqual(art.payload["http_status"], 400)
        self.assertEqual(art.payload["provider_body"], body)
        self.assertEqual(art.payload["stderr"], body)


if __name__ == "__main__":
    unittest.main()
