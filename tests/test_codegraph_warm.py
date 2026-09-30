"""Warm CodeGraph explore helper: same answers as the CLI call, far fewer spawns."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from puppetmaster import codegraph_warm

NODE = shutil.which("node")

FAKE_DIRECTORY = """
const fs = require('fs'); const path = require('path');
exports.isInitialized = (p) => fs.existsSync(path.join(p, '.codegraph', 'codegraph.db'));
"""
FAKE_INDEX = """
class CodeGraph { static async open(root) { const g = new CodeGraph(); g.root = root; return g; } destroy() {} }
exports.default = CodeGraph;
"""
FAKE_TOOLS = """
class ToolHandler {
  constructor(cg) { this.cg = cg; }
  async execute(name, args) {
    if (args.query === 'sleep') { await new Promise(r => setTimeout(r, 2000)); }
    if (args.query === 'fail') return { isError: true, content: [{ text: 'boom' }] };
    return { content: [{ text: `${name}|${args.query}|${args.maxFiles}|${process.pid}|${this.cg.root}` }] };
  }
}
exports.ToolHandler = ToolHandler;
"""


def _fake_install(tmp: Path) -> list:
    bundle = tmp / "cg" / "node_modules" / "@colbymchenry" / "codegraph-test"
    lib = bundle / "lib" / "dist"
    (lib / "mcp").mkdir(parents=True)
    (lib / "directory.js").write_text(FAKE_DIRECTORY)
    (lib / "index.js").write_text(FAKE_INDEX)
    (lib / "mcp" / "tools.js").write_text(FAKE_TOOLS)
    shim = tmp / "cg" / "npm-shim.js"
    shim.write_text("")
    return [NODE, str(shim)]


@unittest.skipUnless(NODE, "requires node")
class WarmExploreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.invocation = _fake_install(self.tmp)
        self.repo = self.tmp / "repo"
        (self.repo / ".codegraph").mkdir(parents=True)
        (self.repo / ".codegraph" / "codegraph.db").write_text("db")
        (self.repo / "sub").mkdir()

    def tearDown(self):
        codegraph_warm.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_locate_finds_the_platform_bundle(self):
        node, lib = codegraph_warm.locate(self.invocation)
        self.assertEqual(node, NODE)
        self.assertTrue(lib.endswith(os.path.join("lib", "dist")))

    def test_one_helper_answers_repeated_queries_from_the_resolved_root(self):
        first = codegraph_warm.explore(self.invocation, str(self.repo / "sub"), "a b", 15, 10)
        second = codegraph_warm.explore(self.invocation, str(self.repo / "sub"), "c", 15, 10)
        name, query, max_files, pid, root = first.split("|")
        self.assertEqual((name, query, max_files), ("codegraph_explore", "a b", "15"))
        self.assertEqual(Path(root).resolve(), self.repo.resolve())
        self.assertEqual(second.split("|")[3], pid)

    def test_index_change_restarts_the_helper(self):
        pid = codegraph_warm.explore(self.invocation, str(self.repo), "q", 5, 10).split("|")[3]
        db = self.repo / ".codegraph" / "codegraph.db"
        db.write_text("reindexed")
        os.utime(db, ns=(time.time_ns(), time.time_ns() + 10_000_000))
        self.assertNotEqual(codegraph_warm.explore(self.invocation, str(self.repo), "q", 5, 10).split("|")[3], pid)

    def test_errors_and_timeouts_fall_back(self):
        with self.assertRaises(codegraph_warm.Unavailable):
            codegraph_warm.explore(self.invocation, str(self.repo), "fail", 5, 10)
        with self.assertRaises(codegraph_warm.Unavailable):
            codegraph_warm.explore(self.invocation, str(self.repo), "sleep", 5, 0.3)
        self.assertIn("|q|", codegraph_warm.explore(self.invocation, str(self.repo), "q", 5, 10))

    def test_uninitialized_workspace_and_unknown_layout_fall_back(self):
        with self.assertRaises(codegraph_warm.Unavailable):
            codegraph_warm.explore(self.invocation, str(self.tmp), "q", 5, 10)
        with self.assertRaises(codegraph_warm.Unavailable):
            codegraph_warm.explore(["codegraph"], str(self.repo), "q", 5, 10)


if __name__ == "__main__":
    unittest.main()
