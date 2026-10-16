"""Channel evidence must preserve net amounts and compare only compatible sources."""
from html.parser import HTMLParser
import math
import re

import pytest


def _row(channel="카드", terminal=100, daily=100, vegas=100):
    missing = terminal is None or (isinstance(terminal, float) and math.isnan(terminal))
    return {
        "채널": channel, "한솔": terminal, "일마": daily, "차트": vegas,
        "한솔-차트": None if missing else terminal - vegas,
        "한솔-일마": None if missing else terminal - daily,
        "일마-차트": daily - vegas,
    }


class _Evidence(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if "data-source" in attrs:
            attrs["text"] = []
        if re.search(r"(?:^|;)\s*left\s*:", attrs.get("style", "")):
            for _, parent in reversed(self.stack):
                if "data-value" in parent:
                    parent["fill-style"] = attrs["style"]
                    break
        self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.text.append(data)
        for _, attrs in self.stack:
            if "data-source" in attrs:
                attrs["text"].append(data)

    @property
    def visible_text(self):
        return " ".join(self.text)

    @property
    def bars(self):
        return [attrs for _, attrs in self.elements if "data-value" in attrs]

    @property
    def sources(self):
        return {attrs["data-source"]: " ".join(attrs["text"])
                for _, attrs in self.elements if "data-source" in attrs}

    @property
    def zero_axes(self):
        return [_percent(attrs["style"], "--zero") for _, attrs in self.elements
                if "--zero:" in attrs.get("style", "").replace(" ", "")]


def _percent(style, property_name):
    match = re.search(rf"(?:^|;)\s*{re.escape(property_name)}\s*:\s*([-\d.]+)%", style)
    assert match, f"Missing percentage {property_name!r} in {style!r}"
    return float(match.group(1))


@pytest.mark.parametrize(("row", "expected"), [
    (_row(), "matched"),
    (_row(terminal=90), "review"),
    (_row(daily=90), "review"),
    (_row(terminal=None), "partial"),
    (_row(terminal=float("nan")), "partial"),
    (_row(terminal=None, daily=90), "review"),
    (_row("현금+이체", terminal=30), "matched"),
    (_row("현금+이체", terminal=None), "matched"),
    (_row("현금+이체", terminal=30, daily=90), "review"),
    (_row("플랫폼", terminal=None), "matched"),
    (_row("플랫폼", terminal=None, daily=90), "review"),
])
def test_review_status_uses_only_comparable_sources(app, row, expected):
    kind, label = app._channel_review_status(row)

    assert kind == expected
    assert re.search("[가-힣]", label)


def test_evidence_bars_share_zero_axis_and_preserve_negative_net(app):
    evidence = _Evidence(app._channel_evidence_html(
        _row(terminal=-50, daily=100, vegas=0), verification_count=0))

    assert len(evidence.bars) == 3
    assert set(evidence.sources) == {"한솔", "일마", "차트"}
    assert len(evidence.zero_axes) == 3
    assert evidence.zero_axes == pytest.approx([100 / 3] * 3, abs=0.02)
    expected = {-50: (0, 100 / 3), 100: (100 / 3, 200 / 3), 0: (100 / 3, 0)}
    assert {int(bar["data-value"]) for bar in evidence.bars} == set(expected)
    for bar in evidence.bars:
        left, width = expected[int(bar["data-value"])]
        assert _percent(bar["fill-style"], "left") == pytest.approx(left, abs=0.02)
        assert _percent(bar["fill-style"], "width") == pytest.approx(width, abs=0.02)
    assert "-50" in evidence.visible_text


@pytest.mark.parametrize("missing", [None, float("nan")])
def test_missing_terminal_is_dash_with_explanation_not_zero(app, missing):
    evidence = _Evidence(app._channel_evidence_html(
        _row(terminal=missing, daily=0, vegas=0), verification_count=0))

    assert "미제공" in evidence.sources["한솔"]
    assert any(dash in evidence.sources["한솔"] for dash in ("—", "–", "-"))
    assert len(evidence.bars) == 2
    assert [float(bar["data-value"]) for bar in evidence.bars] == [0, 0]
    assert all(_percent(bar["fill-style"], "width") == 0 for bar in evidence.bars)
    assert all(0 <= zero <= 100 for zero in evidence.zero_axes)


def test_zero_terminal_is_present_and_all_zero_graph_has_finite_geometry(app):
    evidence = _Evidence(app._channel_evidence_html(
        _row(terminal=0, daily=0, vegas=0), verification_count=0))

    assert "미제공" not in evidence.visible_text
    assert len(evidence.bars) == 3
    assert len(evidence.zero_axes) == 3
    for bar in evidence.bars:
        assert float(bar["data-value"]) == 0
        assert _percent(bar["fill-style"], "width") == 0
        assert 0 <= _percent(bar["fill-style"], "left") <= 100


def test_cash_receipt_amount_remains_visible_but_does_not_scale_comparison(app):
    evidence = _Evidence(app._channel_evidence_html(
        _row("현금+이체", terminal=900000, daily=100, vegas=50), verification_count=0))

    assert "900,000" in evidence.sources["한솔"]
    assert {float(bar["data-value"]) for bar in evidence.bars} == {100, 50}
    widths = {float(bar["data-value"]): _percent(bar["fill-style"], "width")
              for bar in evidence.bars}
    assert widths == pytest.approx({100: 100, 50: 50})


def test_platform_terminal_is_marked_not_applicable(app):
    evidence = _Evidence(app._channel_evidence_html(
        _row("플랫폼", terminal=None), verification_count=0))

    assert "비교대상 아님" in evidence.sources["한솔"]
    assert len(evidence.bars) == 2


def test_channel_title_is_html_escaped_and_review_memo_is_deterministic(app):
    title = '<script>alert("x")</script> & 채널'
    row = _row(title)

    markup = app._channel_evidence_html(row, verification_count=7)
    evidence = _Evidence(markup)

    assert title in evidence.visible_text
    assert not any(tag == "script" for tag, _ in evidence.elements)
    assert "&lt;script&gt;" in markup and "&amp;" in markup
    assert "환자" in evidence.visible_text and re.search(r"(?<!\d)7(?!\d)", evidence.visible_text)
    assert markup == app._channel_evidence_html(row, verification_count=7)
