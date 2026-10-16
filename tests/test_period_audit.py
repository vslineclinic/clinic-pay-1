"""기간 분석에서 연결된 거래의 실제 차이가 사라지지 않는 회귀 사례."""
from datetime import date
import io

import pandas as pd
import pytest
import factories as F


def _files(app, h_rows, p_rows):
    h_raw, p_raw = F.period_raw_files(h_rows, p_rows)
    return app.parse_hansol(h_raw), app.parse_patient(p_raw)


def test_period_keeps_approval_amount_difference(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성가', '카드', 60000, '100111')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert uh['금액'].tolist() == [50000]
    assert up['금액'].tolist() == [60000]


def test_period_preserves_offsetting_approval_differences(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드'),
                      (60000, '200222', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성가', '카드', 60000, '100111'),
                   ('2026-05-11', '200', '합성나', '카드', 50000, '200222')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == len(up) == 2
    table = app.compute_period_recon(h, p)
    assert table.iloc[0]['카드차이'] == 0
    assert app.period_summary(table)['bad_days'] == ['2026-05-11']


def test_period_partial_self_cancel_is_not_zero(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드'),
                      (20000, '100111', '취소승인', '카드')],
                  [('2026-05-12', '200', '합성나', '카드', 1, '')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == 2 and up.empty


def test_period_approval_channel_or_direction_mismatch_stays(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드'),
                      (50000, '200222', '정상승인', '현금')],
                  [('2026-05-11', '100', '합성가', '현금', 50000, '100111'),
                   ('2026-05-11', '200', '합성나', '환불-현금', 50000, '200222')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == len(up) == 2


def test_period_amount_fallback_requires_same_channel_and_unique_both_sides(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성가', '현금', 50000, ''),
                   ('2026-05-11', '200', '합성나', '카드', 50000, ''),
                   ('2026-05-11', '300', '합성다', '카드', 50000, '')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == 1 and len(up) == 3


def test_period_same_approval_on_different_dates_does_not_match(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드', '260511')],
                  [('2026-05-12', '100', '합성가', '카드', 50000, '100111')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == 1 and up.empty
    uh, up = app.find_period_day_detail(h, p, '2026-05-12')
    assert uh.empty and len(up) == 1


def test_period_split_approval_group_with_equal_net_is_reconciled(app):
    h, p = _files(app, [(30000, '100111', '정상승인', '카드'),
                      (20000, '200222', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성가', '카드', 50000, '100111 / 200222')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert uh.empty and up.empty


def test_period_platform_difference_and_offsetting_patient_errors(app):
    h, p = _files(app, [(1000, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성가', '카드', 1000, '100111'),
                   ('2026-05-11', '200', '합성나', '기타(기타)', 50000, '여신티켓'),
                   ('2026-05-12', '300', '합성다', '카드', 20000, ''),
                   ('2026-05-12', '400', '합성라', '카드', 30000, '')])
    table = app.compute_period_recon(h, p)
    # 두 번째 날의 단말기 합계 차이는 지워 환자 상쇄만으로 선택되는지 확인한다.
    table.loc[table['날짜'] == '2026-05-12', '카드차이'] = 0
    tabs = {
        date(2026, 5, 11): F.table_raw(['내원순서', '차트번호', '성명', '카드', '여신티켓'],
                                     [1, '100', '합성가', 1000, 0], [2, '200', '합성나', 0, 30000]),
        date(2026, 5, 12): F.table_raw(['내원순서', '차트번호', '성명', '카드', '여신티켓'],
                                     [1, '300', '합성다', 30000, 0], [2, '400', '합성라', 20000, 0]),
    }
    def loader(url, day, **kwargs):
        return tabs[day], day.isoformat()
    table, skipped, _ = app.augment_period_with_gsheet(table, p, 'synthetic', loader=loader)
    assert not skipped
    assert table['일마-차트플랫폼차이'].tolist() == [-20000, 0]
    assert table['일마검증건수'].tolist() == [1, 2]
    summary = app.period_summary(table)
    assert summary['platform_days'] == ['2026-05-11']
    assert summary['bad_days'] == ['2026-05-11', '2026-05-12']
    assert summary['platform_abs_gap'] == 20000


def test_excel_summary_reuses_channel_labels_sign_and_nonapplicable_cells(app):
    totals = dict(_has_hansol=True, h_card=0, d_card=0, p_card=0,
                  h_cash=0, d_cashxfer=0, p_cashxfer=0, d_plat=100000, p_plat=120000)
    channel = app.compute_channel_recon(totals)
    empty = pd.DataFrame()
    data = app.build_ai_excel(empty, empty, empty, totals, channel)
    summary = pd.read_excel(io.BytesIO(data), sheet_name='4_합계')
    pd.testing.assert_frame_equal(summary, pd.read_excel(io.BytesIO(data), sheet_name='0_채널대사'))
    platform = summary[summary['채널'] == '플랫폼'].iloc[0]
    assert platform['일마-차트'] == -20000
    assert pd.isna(platform['한솔']) and pd.isna(platform['한솔-차트'])


def test_period_negative_cancel_amount_and_same_group_partial_refund(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드'),
                      (20000, '100111', '취소승인', '카드')],
                  [('2026-05-11', '100', '합성가', '카드', 50000, '100111'),
                   ('2026-05-11', '100', '합성가', '환불-카드', 10000, '100111')])
    # 파서의 표준 양수 취소와 부호 있는 취소가 같은 순액이어야 한다.
    h.loc[h['tx_status'] == '취소', '금액'] *= -1
    table = app.compute_period_recon(h, p)
    assert table.iloc[0]['한솔카드'] == 30000
    assert table.iloc[0]['카드차이'] == -10000
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == len(up) == 2


def test_period_refund_only_daily_sheet_is_compared(app):
    h, p = _files(app, [(20000, '100111', '취소승인', '카드')],
                  [('2026-05-11', '100', '합성가', '환불-카드', 20000, '100111')])
    raw = F.table_raw(['내원순서', '차트번호', '성명', '구분', '카드'],
                      [1, '100', '합성가', '환불', 20000])
    table, skipped, day_data = app.augment_period_with_gsheet(
        app.compute_period_recon(h, p), p, 'synthetic', loader=lambda *a, **k: (raw, '26.05.11'))
    assert not skipped and '2026-05-11' in day_data
    assert table.iloc[0]['일마카드'] == -20000
    assert table.iloc[0]['일마-차트카드차이'] == 0


def test_period_rematches_unique_amount_after_unlinked_full_cancel(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드'),
                      (50000, '100111', '취소승인', '카드'),
                      (50000, '200222', '정상승인', '카드')],
                  [('2026-05-11', '200', '합성나', '카드', 50000, '')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert uh.empty and up.empty


def test_period_ignores_zero_payment_patient_row(app):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성정상', '카드', 50000, '100111'),
                   ('2026-05-11', '200', '합성무수납', '', 0, '')])
    table = app.compute_period_recon(h, p)
    assert table.iloc[0]['미설명거래건수'] == 0
    assert app.period_summary(table)['bad_days'] == []


@pytest.mark.parametrize('approval', ['100111', ''])
def test_period_retains_negative_payment_without_cancel_flag(app, approval):
    h, p = _files(app, [(50000, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성음수', '카드', -50000, approval)])
    assert not bool(p.iloc[0]['is_취소'])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == len(up) == 1
    assert up.iloc[0]['금액'] == -50000
    assert '금액 부호·취소 상태' in up.iloc[0]['검토사유']
    assert app.compute_period_recon(h, p).iloc[0]['카드차이'] == 100000


@pytest.mark.parametrize('h_amount', [20000, 120000])
def test_period_negative_payment_does_not_disappear_in_approval_group_net(app, h_amount):
    h, p = _files(app, [(h_amount, '100111', '정상승인', '카드')],
                  [('2026-05-11', '100', '합성양수', '카드', 70000, '100111'),
                   ('2026-05-11', '100', '합성음수', '카드', -50000, '100111')])
    uh, up = app.find_period_day_detail(h, p, '2026-05-11')
    assert len(uh) == 1 and len(up) == 2
    assert app.period_summary(app.compute_period_recon(h, p))['bad_days'] == ['2026-05-11']
