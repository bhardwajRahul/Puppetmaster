"""Embed mode hides dashboard chrome and keeps job query helpers honest."""
from __future__ import annotations

import re
import unittest

from puppetmaster.dashboard import INDEX_HTML, _PAGE_APP_JS, _PAGE_HEAD


def _embed_css() -> str:
    start = _PAGE_HEAD.index("html.embed")
    end = _PAGE_HEAD.index("@media (prefers-reduced-motion")
    return _PAGE_HEAD[start:end]


class DashboardEmbedModeTests(unittest.TestCase):
    def test_head_applies_embed_class_from_query(self) -> None:
        head = _PAGE_HEAD[: _PAGE_HEAD.index("<body>")]
        self.assertIn("classList.toggle(", head)
        self.assertIn("embed=(1|true|yes)", head)
        self.assertLess(head.index("classList.toggle"), head.index("<style>"))

    def test_embed_hides_rail_and_clears_shell_margin(self) -> None:
        css = _embed_css()
        self.assertIn("html.embed .rail { display: none; }", css)
        self.assertIn("html.embed .app-frame { margin-left: 0; }", css)
        self.assertIn("background: #0d0d0d", css)
        self.assertNotIn(".rail { display: none; }", _PAGE_HEAD[_PAGE_HEAD.index("@media (max-width: 700px)"):_PAGE_HEAD.index("html.embed")])

    def test_embed_stacks_then_uses_compact_columns(self) -> None:
        css = _embed_css()
        self.assertIn("html.embed .overview-grid { grid-template-columns: minmax(0, 1fr);", css)
        self.assertIn("@media (min-width: 680px)", css)
        wide = css[css.index("@media (min-width: 680px)") :]
        self.assertIn("minmax(0, 1fr) minmax(15rem, 36%)", wide)

    def test_embed_sticky_context_keeps_status_goal_and_cost(self) -> None:
        self.assertIn('id="embed-context"', _PAGE_HEAD)
        self.assertIn("function setEmbedContext", _PAGE_APP_JS)
        self.assertIn("formatSelectedCost(actualCost(job))", _PAGE_APP_JS)
        css = _embed_css()
        self.assertIn("html.embed .topbar { position: sticky;", css)
        self.assertIn("html.embed .embed-context", css)
        self.assertIn("html.embed .embed-cost", css)
        self.assertIn("html.embed .breadcrumb[hidden]", css)

    def test_embed_keeps_highlights_frontier_and_evidence(self) -> None:
        self.assertIn("function renderHighlights", _PAGE_APP_JS)
        self.assertIn("${renderHighlights(job)}", _PAGE_APP_JS)
        self.assertIn("function renderFrontier", _PAGE_APP_JS)
        self.assertIn("function renderEvidence", _PAGE_APP_JS)
        self.assertIn("html.embed .highlights { padding:", _embed_css())

    def test_job_rows_preserve_embed_on_deep_links(self) -> None:
        self.assertIn("jobHref(job.id, embedMode)", _PAGE_APP_JS)
        self.assertNotIn("?job=${encodeURIComponent(job.id)}", _PAGE_APP_JS)

    def test_live_polling_is_unchanged(self) -> None:
        self.assertIn("window.setInterval(tick, 1500)", _PAGE_APP_JS)
        self.assertIn('requestJson("/api/job?id=" + encodeURIComponent(jobId))', _PAGE_APP_JS)
        self.assertIn('requestJson("/api/jobs")', _PAGE_APP_JS)

    def test_packaged_page_includes_embed_contract(self) -> None:
        self.assertIn("html.embed .rail { display: none; }", INDEX_HTML)
        self.assertEqual(len(re.findall(r"embed=\(1\|true\|yes\)", INDEX_HTML)), 2)


    def test_display_title_skips_marionette_provenance_label(self) -> None:
        self.assertIn("function isProvenanceLabel", _PAGE_APP_JS)
        self.assertIn("function jobDisplayTitle", _PAGE_APP_JS)
        self.assertIn("jobDisplayTitle(job)", _PAGE_APP_JS)
        self.assertNotIn(
            "const headline = job.job.label || job.job.title || job.job.id;",
            _PAGE_APP_JS,
        )


if __name__ == "__main__":
    unittest.main()
