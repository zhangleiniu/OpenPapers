import unittest
from types import SimpleNamespace

from automation.monitor import Monitor


# Sanitized reduction of the live proceedings.mlr.press homepage: an
# unclosed editors <p> swallows the whole volume list, so the editors'
# links share a parent whose text contains every volume label.
PMLR_HOME = b"""<html><body><main><article>
<p>The Series Editors are <a href="https://editor-one.example">Editor One</a>,
<a href="https://editor-two.example">Editor Two</a>.
<h3>Volumes</h3>
<p><ul>
  <li><a href="v337"><b>Volume 337</b></a> Proceedings of UAI 2026</li>
  <li><a href="v336"><b>Volume 336</b></a> Proceedings of COLT 2026</li>
  <li><a href="v306"><b>Volume 306</b></a> Proceedings of ICML 2026</li>
  %s
</ul>
</article></main></body></html>"""


class FakeSession:
    def __init__(self, content):
        self.content = content

    def get(self, url, **_):
        return SimpleNamespace(content=self.content)


def check_pmlr(content, label):
    monitor = Monitor(session=FakeSession(content))
    return monitor.check("icml", 2026, {
        "type": "pmlr_volume",
        "url": "https://proceedings.mlr.press/",
        "label": label,
    })


class PmlrVolumeTests(unittest.TestCase):
    def test_matches_only_the_labelled_volume_entry(self):
        status, count, _, detail = check_pmlr(PMLR_HOME % b"", "ICML 2026")
        self.assertEqual((status, count, detail), ("available", 1, ""))

    def test_unrelated_new_volume_does_not_change_digest(self):
        before = check_pmlr(PMLR_HOME % b"", "COLT 2026")
        after = check_pmlr(
            PMLR_HOME % b'<li><a href="v338"><b>Volume 338</b></a> '
                        b'Proceedings of Some Workshop 2026</li>',
            "COLT 2026",
        )
        self.assertEqual(before, after)

    def test_missing_label_is_unavailable(self):
        status, count, _, _ = check_pmlr(PMLR_HOME % b"", "AISTATS 2026")
        self.assertEqual((status, count), ("unavailable", 0))


if __name__ == "__main__":
    unittest.main()
