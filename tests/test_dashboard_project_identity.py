"""The dashboard header must say which project the board is serving.

The bug this closes: ``default_state_dir()`` hashes the git root of the
caller's cwd, so a session opened at ``C:\\Projects\\puppetmaster`` (which is
not itself a git repo -- the repo is one level down) took the fallback branch
and forked ONE logical project into two state dirs,
``projects\\puppetmaster-c3177e6032c4`` (1 stale job) and
``projects\\Puppetmaster-b92145e840c8`` (24 jobs). Two dashboards then came up
on :8787 and :8788, pixel-identical, with nothing on screen to say which was
which -- so "where did my jobs go?" had no answer you could read off the page.

/api/meta already carried the identity on the wire (see
tests/test_dashboard_ports.py); this exercises the last mile -- rendering it in
the header.

A new file, deliberately: tests/test_puppetmaster.py is enormous and its
existing node harness (``test_renderer_js_neutralizes_xss_and_preserves_digits``)
is left untouched, so the purity harness below is additive rather than an edit
to a hot, contended file.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from puppetmaster.dashboard import (
    _PAGE_APP_JS,
    _PAGE_HEAD,
    RENDERER_JS,
    serve,
)
from puppetmaster.store_factory import create_store

MIDDLE_DOT = "\u00b7"


def _store_dir(root) -> Path:
    """An initialised sqlite state dir -- there are no shared fixtures in
    tests/conftest.py, so each test builds its own."""
    store_dir = Path(root) / ".puppetmaster"
    create_store("sqlite", store_dir).init()
    return store_dir


def _mobile_block() -> str:
    """The narrowest viewport rules with comments stripped, so the
    narrow-viewport rules can be asserted on without matching the desktop rules
    of the same name -- or a prose mention of a selector inside a comment."""
    start = _PAGE_HEAD.index("@media (max-width: 700px)")
    block = _PAGE_HEAD[start : _PAGE_HEAD.index("</style>", start)]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)


class HeaderSlotTests(unittest.TestCase):
    def test_project_slot_lives_in_the_persistent_workspace_rail(self) -> None:
        self.assertIn('id="project"', _PAGE_HEAD)
        rail = _PAGE_HEAD[_PAGE_HEAD.index('<aside class="rail"') : _PAGE_HEAD.index("</aside>")]
        self.assertIn('class="workspace-block"', rail)
        self.assertIn('id="project"', rail)
        self.assertIn("Workspace", rail)

    def test_project_slot_is_outside_poll_replaced_content(self) -> None:
        project = _PAGE_HEAD.index('id="project"')
        content = _PAGE_HEAD.index('id="content"')
        self.assertLess(project, content)

    def test_project_identity_has_workspace_specific_style(self) -> None:
        self.assertIn(".workspace-name", _PAGE_HEAD)
        self.assertIn("text-overflow: ellipsis", _PAGE_HEAD)


class MobileHeaderTests(unittest.TestCase):
    def test_mobile_keeps_workspace_identity_visible(self) -> None:
        block = _mobile_block()
        self.assertIn(".workspace-name", block)
        workspace_rule = re.search(r"\.workspace-name\s*\{([^}]*)\}", block)
        self.assertIsNotNone(workspace_rule)
        self.assertNotIn("display: none", workspace_rule.group(1))
        self.assertIn("max-width", workspace_rule.group(1))

    def test_mobile_rail_becomes_a_compact_top_strip(self) -> None:
        block = _mobile_block()
        rail_rule = re.search(r"\.rail\s*\{([^}]*)\}", block)
        self.assertIsNotNone(rail_rule)
        self.assertIn("position: static", rail_rule.group(1))
        self.assertIn("flex-direction: row", rail_rule.group(1))


class ClientWiringTests(unittest.TestCase):
    def test_meta_is_fetched_once_at_bootstrap_not_polled(self) -> None:
        for needle in (
            "/api/meta",
            "function loadMeta",
            "loadMeta()",
            'getElementById("project")',
            "document.title",
        ):
            self.assertIn(needle, _PAGE_APP_JS, needle)
        # The one-shot call sits in the bootstrap seam after tick()'s
        # definition -- if it had been folded INTO tick() it would re-fetch
        # immutable identity every 1.5s forever.
        self.assertGreater(_PAGE_APP_JS.rindex("loadMeta()"), _PAGE_APP_JS.index("async function tick()"))
        self.assertEqual(_PAGE_APP_JS.count("loadMeta()"), 2)

    def test_load_meta_owns_its_own_failure(self) -> None:
        """tick()'s bare catch is not an umbrella for a sibling called from the
        bootstrap, so loadMeta must guard itself: try/catch around the fetch
        AND an r.ok check (a pre-upgrade server 404s /api/meta). Sliced to
        loadMeta's own body so this can never accidentally see tick()'s catch
        or applyMeta's guards."""
        start = _PAGE_APP_JS.index("async function loadMeta")
        end = _PAGE_APP_JS.index("async function loadDiagnostics", start)
        body = _PAGE_APP_JS[start:end]
        for needle in ("try {", "catch", 'requestJson("/api/meta")'):
            self.assertIn(needle, body, f"{needle} missing from loadMeta body")

    def test_project_tag_is_set_as_text_not_markup(self) -> None:
        """With an explicit --state-dir the basename is filesystem-derived and
        /api/meta never sanitises it, so the tag must go in as text."""
        start = _PAGE_APP_JS.index("async function loadMeta")
        body = _PAGE_APP_JS[start:_PAGE_APP_JS.index("async function loadDiagnostics", start)]
        # // comments are stripped first: applyMeta deliberately NAMES innerHTML
        # in prose, to record why it is avoided, so a raw substring scan would
        # fire on the explanation. Strip the prose, then look for the thing that
        # is actually dangerous -- an assignment -- rather than the mention.
        code = re.sub(r"//.*", "", body)
        self.assertIn("textContent", code)
        self.assertIsNone(
            re.search(r"innerHTML\s*=", code),
            "applyMeta assigns to innerHTML",
        )
        self.assertIn("if (label)", code)


class ServedHtmlTests(unittest.TestCase):
    def _serve(self, state_dir: Path) -> int:
        httpd = serve(
            state_dir,
            backend="sqlite",
            host="127.0.0.1",
            port=0,
            open_browser=False,
            serve_forever=False,
        )
        # addCleanup is LIFO, so server_close must be registered FIRST for
        # shutdown to run before it -- closing the listening socket out from
        # under a blocked serve_forever is the wrong order (same registration
        # order as tests/test_dashboard_ports.py).
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def test_real_server_ships_the_project_slot_and_the_renderer(self) -> None:
        with TemporaryDirectory() as tmp:
            port = self._serve(_store_dir(tmp))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                body = resp.read().decode("utf-8")
        self.assertIn('id="project"', body)
        self.assertIn("projectLabel(", body)
        self.assertIn(".workspace-name", body)


class ProjectLabelSourceTests(unittest.TestCase):
    def test_separator_is_a_real_middle_dot_not_mojibake(self) -> None:
        """Checked in Python, with no subprocess in the way: a UTF-8 round-trip
        accident would leave "Â·" (U+00C2 U+00B7) or an "&middot;" entity, and
        the entity would render literally since the tag is set via
        textContent."""
        self.assertIn(f" {MIDDLE_DOT} ", RENDERER_JS)
        self.assertNotIn("\u00c2\u00b7", RENDERER_JS)
        self.assertNotIn("&middot;", RENDERER_JS)
        self.assertNotIn("&#183;", RENDERER_JS)


class ProjectLabelPurityTests(unittest.TestCase):
    """RENDERER_JS is executed verbatim under node, so projectLabel may not
    touch a single browser API. Merely EVALUATING the constant cannot prove
    that -- JavaScript resolves free identifiers at call time -- so this
    poisons the globals with throwing getters and then calls the function."""

    def test_project_label_is_pure_and_preserves_the_digest(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        harness = RENDERER_JS + r"""
const assert = require("assert");

// Throwing getters, not deletions: a free `document` inside projectLabel
// resolves through globalThis at CALL time and trips the getter.
const POISON = ["document", "window", "location", "fetch", "navigator", "localStorage"];
for (const name of POISON) {
  try {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() { throw new Error("projectLabel touched browser global: " + name); },
    });
  } catch (e) { /* non-configurable in this node build; the ones below suffice */ }
}
// Prove the poison actually took for the two names no node build owns
// natively (so defineProperty cannot have been refused) -- otherwise this test
// could silently degrade into "we called a function".
for (const name of ["document", "window"]) {
  assert.throws(() => globalThis[name], /touched browser global/, name + " not poisoned");
}

const forked_b = projectLabel({project: "Puppetmaster-b92145e840c8"});
const forked_c = projectLabel({project: "puppetmaster-c3177e6032c4"});
assert.strictEqual(forked_b, "Puppetmaster \u00b7 b92145e8");
assert.strictEqual(forked_c, "puppetmaster \u00b7 c3177e60");
// The whole point: these two are the SAME slug in different case, so the
// digest is the only thing that can tell the boards apart.
assert.notStrictEqual(forked_b.toLowerCase(), forked_c.toLowerCase());
assert.ok(/[0-9a-f]{8}/.test(forked_b));
assert.ok(/[0-9a-f]{8}/.test(forked_c));

// all_projects wins over an incidental project name: serve() always passes a
// state_dir, so an --all-projects board carries one too.
assert.strictEqual(
  projectLabel({project: "Puppetmaster-b92145e840c8", all_projects: true}),
  "all projects"
);
assert.strictEqual(projectLabel({all_projects: true}), "all projects");

// --mobile / non-loopback: /api/meta withholds the basename, hash only.
const remote = projectLabel({project: null, state_dir_id: "abcdef123456"});
assert.strictEqual(remote, "state abcdef12");
for (const bad of ["null", "undefined", "NaN"]) {
  assert.ok(!remote.includes(bad), "rendered " + bad);
}

assert.strictEqual(projectLabel(null), "");
assert.strictEqual(projectLabel({}), "");
// A name that isn't <slug>-<12 hex> is passed through, not mangled.
assert.strictEqual(projectLabel({project: "plain-name"}), "plain-name");

console.log("project-label-ok");
"""
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("project-label-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
