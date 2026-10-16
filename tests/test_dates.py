"""하루 분석의 날짜 일치와 검증 불가 입력을 구분한다."""
from datetime import date

import pandas as pd
import pytest


def _rows(*days):
    return pd.DataFrame({"날짜": days, "차트번호": ["100"] * len(days)})


@pytest.mark.parametrize("p_days,h_days,picked", [
    (["2026-05-11"], ["2026-05-11"], date(2026, 5, 12)),
    (["2026-05-12"], ["2026-05-11"], date(2026, 5, 12)),
    (["2026-05-12"], ["2026-05-11"], None),
])
def test_single_day_mismatch_blocks_even_when_chart_numbers_overlap(app, p_days, h_days, picked):
    _, _, _, error = app.filter_to_single_date(
        _rows(*p_days), _rows(*h_days), _rows(""), picked)
    assert error and "2026-05-12" in error
    assert "검증 불가" not in error


@pytest.mark.parametrize("invalid", ["", None, "not-a-date", "2026-02-30"])
@pytest.mark.parametrize("source", ["patient", "hansol"])
def test_missing_or_invalid_dates_block_without_dropping_rows(app, invalid, source):
    patient = _rows("2026-05-12")
    hansol = _rows("2026-05-12")
    if source == "patient":
        patient = _rows("2026-05-12", invalid)
    else:
        hansol = _rows("2026-05-12", invalid)
    p2, h2, _, error = app.filter_to_single_date(
        patient, hansol, _rows(""), date(2026, 5, 12))
    assert "검증 불가" in error
    assert len(p2) == len(patient) and len(h2) == len(hansol)


def test_date_column_missing_is_unverifiable(app):
    patient = pd.DataFrame({"차트번호": ["100"]})
    _, _, _, error = app.filter_to_single_date(patient, pd.DataFrame(), patient)
    assert "검증 불가" in error


def test_matching_single_day_and_optional_terminal_are_accepted(app):
    patient = _rows("2026-05-12")
    p2, h2, note, error = app.filter_to_single_date(
        patient, pd.DataFrame(), patient, date(2026, 5, 12))
    assert not error and not note and len(p2) == 1 and h2.empty


def test_tied_overlap_does_not_choose_an_arbitrary_day(app):
    patient = _rows("2026-05-11", "2026-05-12")
    _, _, _, error = app.filter_to_single_date(patient, pd.DataFrame(), _rows(""))
    assert error and "여러 수납일" in error
