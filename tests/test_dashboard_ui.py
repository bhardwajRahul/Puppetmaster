import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class DashboardUiTests(unittest.TestCase):
    def test_packaged_assets_compose_offline_page(self) -> None:
        from puppetmaster.dashboard import (
            INDEX_HTML,
            RENDERER_JS,
            _PAGE_APP_JS,
            _PAGE_HEAD,
        )

        self.assertEqual(INDEX_HTML, _PAGE_HEAD + RENDERER_JS + _PAGE_APP_JS)
        self.assertIn("Puppetmaster", INDEX_HTML)
        self.assertIn("dashboard disconnected", INDEX_HTML.lower())
        self.assertNotIn("{{DASHBOARD_CSS}}", INDEX_HTML)
        self.assertNotIn("{{DASHBOARD_JS}}", INDEX_HTML)
        self.assertNotIn("https://", _PAGE_HEAD)

    def test_snapshot_exposes_only_recorded_task_dependencies(self) -> None:
        from puppetmaster.dashboard import build_job_snapshot
        from puppetmaster.models import Task
        from puppetmaster.store import SwarmStore

        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp) / ".puppetmaster")
            store.init()
            job = store.create_job("dependency graph")
            root = Task(job_id=job.id, role="explore", instruction="inspect")
            child = Task(
                job_id=job.id,
                role="implement",
                instruction="change",
                depends_on=[root.id],
            )
            store.save_task(root)
            store.save_task(child)

            snapshot = build_job_snapshot(store, job.id)
            rows = {row["id"]: row for row in snapshot["tasks"]}
            self.assertEqual(rows[root.id]["depends_on"], [])
            self.assertEqual(rows[child.id]["depends_on"], [root.id])

    def test_selected_cost_formatter_distinguishes_unknown_from_zero(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        from puppetmaster.dashboard import _PAGE_APP_JS

        start = _PAGE_APP_JS.index("function formatSelectedCost")
        end = _PAGE_APP_JS.index("function setConnection", start)
        prefix = _PAGE_APP_JS[start:end]
        harness = prefix + r"""
const assert = require("assert");
assert.equal(formatSelectedCost(undefined), "unknown");
assert.equal(formatSelectedCost(null), "unknown");
assert.equal(formatSelectedCost(Number.NaN), "unknown");
assert.equal(formatSelectedCost({total_marginal_cost_usd: 0}), "$0.0000");
assert.equal(formatSelectedCost({total_marginal_cost_usd: .42}), "$0.4200");
console.log("selected-cost-ok");
"""
        completed = subprocess.run(
            [node, "-e", harness], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("selected-cost-ok", completed.stdout)

    def test_renderers_keep_dependencies_evidence_and_canonical_cost_honest(
        self,
    ) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        from puppetmaster.dashboard import RENDERER_JS, _PAGE_APP_JS

        app = _PAGE_APP_JS[: _PAGE_APP_JS.index('document.addEventListener("input"')]
        harness = "global.location = {search: ''};\n" + RENDERER_JS + app + r"""
const assert = require("assert");
const tasks = [
  {id: "root", role: "explore", status: "complete", depends_on: [], activity: []},
  {id: "child", role: "implement", status: "running", depends_on: ["root"], activity: []},
];
const graph = renderMap({tasks});
assert.ok(graph.includes('data-edge-mode="dependencies"'));
assert.ok(graph.includes("1 dependency"));
assert.ok(!graph.includes("Coordinator hub membership"));
const hub = renderMap({tasks: tasks.map(task => ({...task, depends_on: []}))});
assert.ok(hub.includes('data-edge-mode="hub"'));
assert.ok(hub.includes("Independent task"));

const evidence = renderEvidence({artifacts: {
  gist: [{statement: "shared", admission: "admitted", evidence: [], source_artifact_ids: ["finding_1"]}],
  finding: [], risk: [], decision: [], verification: [], patch: [],
}});
assert.ok(evidence.includes("admitted"));
assert.ok(evidence.includes("finding_1"));

const canonical = renderRouting({cost: {actual_cost: {
  total_marginal_cost_usd: .42,
  by_model: {m: {calls: 1, tokens_in: 10, tokens_out: 2, marginal_cost_usd: .42}},
}}, tokens_total: 12, primary_model: "m", routing_rollup: []});
assert.ok(canonical.includes("Selected-model usage cost"));
assert.ok(canonical.includes("$0.4200"));
const splitUsage = {
  total_marginal_cost_usd: null,
  by_model: {estimated: {calls: 1, tokens_in: 100, tokens_out: 20, marginal_cost_usd: null}},
  tasks: [{model_id: "estimated", tokens_in: 100, tokens_out: 20,
    cache_read_tokens: 200, cache_write_tokens: 50, tokens_cached: 200,
    tokens_estimated: true, priced: false, marginal_cost_usd: null}],
};
const before = JSON.stringify(splitUsage);
const split = renderRouting({cost: {actual_cost: splitUsage}, tokens_total: 370});
assert.ok(split.includes('<span>Recorded token consumption</span><strong>370</strong>'));
assert.ok(split.includes('<td class="mono">estimated</td><td class="mono">1</td><td class="mono">370</td><td class="mono">unknown</td>'));
assert.ok(split.includes("Recorded usage; may include estimates and unknown costs"));
assert.ok(!split.includes("Measured and priced records only"));
assert.equal(JSON.stringify(splitUsage), before);
for (const [counts, cache, expected] of [
  [{tokens_in: 100, tokens_out: 20}, {cache_read_tokens: 200}, "320"],
  [{tokens_in: 100, tokens_out: 20}, {cache_write_tokens: 50}, "170"],
  [{tokens_in: 100, tokens_out: 20}, {tokens_cached: 200}, "120"],
  [{tokens_in: 0, tokens_out: 0}, {}, "0"],
  [{tokens_in: null, tokens_out: 20}, {cache_read_tokens: 200}, "Unknown"],
  [{tokens_out: 20}, {}, "Unknown"],
  [{tokens_in: 100, tokens_out: 20}, {cache_read_tokens: null}, "Unknown"],
]) {
  const rendered = renderRouting({cost: {actual_cost: {
    by_model: {m: {calls: 1, ...counts, marginal_cost_usd: 0}},
    tasks: [{model_id: "m", ...counts, ...cache},
      {model_id: "other", cache_read_tokens: 999}],
  }}});
  assert.ok(rendered.includes(`<td class="mono">m</td><td class="mono">1</td><td class="mono">${expected}</td><td class="mono">$0.0000</td>`));
}
const legacy = renderRouting({cost: {total_estimated_cost_usd: 0}, tokens_total: 99, routing_rollup: []});
assert.ok(legacy.includes("unknown"));
assert.ok(legacy.includes("99"));
console.log("dashboard-renderers-ok");
"""
        completed = subprocess.run(
            [node, "-e", harness], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("dashboard-renderers-ok", completed.stdout)

    def test_assets_are_declared_as_package_data(self) -> None:
        config = Path("pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"dashboard_assets/**/*"', config)


if __name__ == "__main__":
    unittest.main()
