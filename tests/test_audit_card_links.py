"""합성 감사 회귀: 환불, 부분 승인 연결, 동명이인, 긴 승인번호."""
import pytest
import pandas as pd

import factories as F


def _parsed(app, h_rows, p_rows, d_rows):
    h = app.parse_hansol(F.hansol_raw(
        금액=[r[0] for r in h_rows], 승인번호=[r[1] for r in h_rows],
        거래상태=[r[2] for r in h_rows], 구분=['카드'] * len(h_rows),
        거래일=[r[3] for r in h_rows], 거래시간=['100000'] * len(h_rows)))
    p = app.parse_patient(F.table_raw(
        ['차트번호', '이름', '결제수단', '비급여(과세총금액)', '결제메모', '수납일'], *p_rows))
    d, dr = app.parse_daily(F.table_raw(
        ['내원순서', '차트번호', '성명', '구분', '카드', '현금', '이체'], *d_rows))
    return h, p, d, dr


def test_normal_refund_is_not_confirmed_error(app):
    h, p, d, dr = _parsed(app,
        [(50000, '100111', '정상승인', '260511'), (50000, '100111', '취소승인', '260511'),
         (10000, '200222', '정상승인', '260511')],
        [('100', '합성가', '카드', 50000, '100111', '2026-05-11'),
         ('100', '합성가', '환불-카드', 50000, '100111', '2026-05-11'),
         ('200', '합성나', '카드', 11000, '200222', '2026-05-11')],
        [(1, '100', '합성가', '', 50000, 0, 0), (2, '100', '합성가', '환불', 50000, 0, 0),
         (3, '200', '합성나', '', 10000, 0, 0)])
    totals = app.compute_totals(h, d, dr, p)
    suspects = app.find_channel_suspects('카드', h, d, p, totals=totals, daily_refund=dr)
    confirmed = [s for s in suspects if '★★' in s['출처']]
    assert len(confirmed) == 1
    assert confirmed[0]['환자'] == '합성나' and confirmed[0]['금액'] == -1000
    assert '즉시 수정' not in confirmed[0]['조치']


@pytest.mark.parametrize('missing_approval_row', [0, 1])
def test_unlinked_sale_or_refund_does_not_create_confirmed_approval_error(app, missing_approval_row):
    p_rows = [
        ['100', '합성정상', '카드', 100000, '100111', '2026-05-11'],
        ['100', '합성정상', '환불-카드', 100000, '100111', '2026-05-11'],
        ['100', '합성정상', '카드', 80000, '200222', '2026-05-11'],
        ['200', '합성오류', '카드', 11000, '300333', '2026-05-11'],
    ]
    p_rows[missing_approval_row][4] = ''
    h, p, d, dr = _parsed(app,
        [(100000, '100111', '정상승인', '260511'), (100000, '100111', '취소승인', '260511'),
         (80000, '200222', '정상승인', '260511'), (10000, '300333', '정상승인', '260511')],
        p_rows,
        [(1, '100', '합성정상', '', 100000, 0, 0), (2, '100', '합성정상', '환불', 20000, 0, 0),
         (3, '200', '합성오류', '', 10000, 0, 0)])
    totals = app.compute_totals(h, d, dr, p)
    assert totals['h_card'] == totals['d_card'] == 90000
    assert p.loc[p['차트번호'] == '100', '금액'].sum() == 80000
    assert app._hansol_card_links(h, p)['100']['net'] is None
    suspects = app.find_channel_suspects('카드', h, d, p, totals=totals, daily_refund=dr)
    confirmed = [s for s in suspects if '★★' in s['출처']]
    assert [(s['환자'], s['금액']) for s in confirmed] == [('합성오류', -1000)]


def test_partial_approval_link_stays_unknown_and_verdict_is_withheld(app):
    h, p, d, dr = _parsed(app,
        [(30000, '100111', '정상승인', '260511'), (20000, '200222', '정상승인', '260511')],
        [('100', '합성가', '카드', 30000, '100111', '2026-05-11'),
         ('100', '합성가', '카드', 20000, '', '2026-05-11')],
        [(1, '100', '합성가', '', 30000, 0, 0)])
    assert app._hansol_card_by_chart(h, p)['100'] == (None, 1)
    verif = app.build_verification(p, d, dr, hansol=h)
    verdict = verif['유형C2_금액불일치'].iloc[0]['한솔판정']
    assert '미연결 1건' in verdict and '연결 1건' in verdict
    assert '차트마감 쪽 오류' not in verdict and '확정' not in verdict
    guessed = app.compute_p1(app.run_matching(h, d, p)[0], p, d)[0]
    table = app.build_3way_table(h, d, p, guessed, daily_refund=dr)
    assert pd.isna(table.iloc[0]['한솔카드']) and pd.isna(table.iloc[0]['한-차카드차'])


@pytest.mark.parametrize('unlinked_approvals', [('', ''), ('200222', '300333')])
def test_offsetting_unlinked_rows_do_not_prove_complete_terminal_amount(app, unlinked_approvals):
    h, p, d, dr = _parsed(app,
        [(30000, '100111', '정상승인', '260511'), (20000, '999999', '정상승인', '260511')],
        [('100', '합성가', '카드', 30000, '100111', '2026-05-11'),
         ('100', '합성가', '카드', 20000, unlinked_approvals[0], '2026-05-11'),
         ('100', '합성가', '환불-카드', 20000, unlinked_approvals[1], '2026-05-11')],
        [(1, '100', '합성가', '', 50000, 0, 0)])
    assert app._hansol_card_by_chart(h, p)['100'] == (None, 1)
    verdict = app.build_verification(p, d, dr, hansol=h)['유형C2_금액불일치'].iloc[0]['한솔판정']
    assert '미연결 2건' in verdict and '판단 보류' in verdict
    assert '일일마감 쪽 오류' not in verdict


@pytest.mark.parametrize('output', ['table', 'ai', 'excel'])
def test_amount_only_match_cannot_fill_unknown_terminal_amount(app, output):
    h, p, d, dr = _parsed(app,
        [(50000, '100111', '정상승인', '260511')],
        [('100', '합성가', '현금', 50000, '', '2026-05-11')],
        [(1, '100', '합성가', '', 50000, 0, 0)])
    guessed = app.compute_p1(app.run_matching(h, d, p)[0], p, d)[0]
    assert guessed.iloc[0]['한솔카드'] == 50000
    totals = app.compute_totals(h, d, dr, p)
    if output == 'table':
        row = app.build_3way_table(h, d, p, guessed, daily_refund=dr).iloc[0]
        assert pd.isna(row['한솔카드']) and pd.isna(row['한-차카드차'])
    elif output == 'ai':
        text = app.build_ai_text(h, d, dr, p, app.compute_channel_recon(totals),
                                 guessed, None, None, {}, totals=totals)
        section = text.split('[차트번호별 3-way 통합')[1].split('[원시거래행')[0]
        patient_line = next(line for line in section.splitlines() if line.startswith('100|'))
        assert patient_line.split('|')[4] == '-'
    else:
        import io
        import openpyxl
        data = app.build_ai_excel(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), totals,
                                 hansol=h, daily=d, patient=p, p1_full=guessed, daily_refund=dr)
        sheet = openpyxl.load_workbook(io.BytesIO(data))['0_3way통합']
        values = dict(zip([c.value for c in sheet[1]], [c.value for c in sheet[2]]))
        assert values['한솔카드'] is None and values['한-차카드차'] is None


@pytest.mark.parametrize('reverse', [False, True])
def test_ambiguous_name_amount_does_not_merge_either_direction(app, reverse):
    p_rows = [('100', '합성동명', '카드', 50000, '', '2026-05-11'),
              ('900', '합성동명', '카드', 50000, '', '2026-05-11')]
    d_rows = [(1, '555', '합성동명', '', 50000, 0, 0)]
    if reverse:
        p_rows = [('555', '합성동명', '카드', 50000, '', '2026-05-11')]
        d_rows = [(1, '100', '합성동명', '', 50000, 0, 0),
                  (2, '900', '합성동명', '', 50000, 0, 0)]
    h, p, d, dr = _parsed(app, [(10000, '200222', '정상승인', '260511')], p_rows, d_rows)
    verif = app.build_verification(p, d, dr)
    assert verif['유형B_차트번호오타'].empty
    assert len(verif['유형C1_한쪽만존재']) == 3
    pp, dp = app._chart_method_pivots(p, d, dr)
    assert set(pp) == set(p['차트번호']) and set(dp) == set(d['차트번호'])


def test_distinct_long_approvals_do_not_collide_in_links_or_suspects(app):
    h, p, d, dr = _parsed(app,
        [(50000, '112345678', '정상승인', '260511'), (30000, '212345678', '정상승인', '260511')],
        [('100', '합성가', '카드', 50000, '112345678', '2026-05-11')],
        [(1, '100', '합성가', '', 80000, 0, 0)])
    assert app._hansol_card_by_chart(h, p)['100'] == (50000, 1)
    totals = app.compute_totals(h, d, dr, p)
    suspects = app.find_channel_suspects('카드', h, d, p, totals=totals, daily_refund=dr)
    assert not [s for s in suspects if '★★' in s['출처']]
    text = app.build_ai_text(h, d, dr, p, app.compute_channel_recon(totals),
                             None, None, None, {'카드': suspects}, totals=totals)
    raw_section = text.split('[원시거래행')[1].split('[한솔PG-only]')[0]
    assert '212345678' not in raw_section
    assert '112345678' in raw_section


def test_same_approval_on_different_date_does_not_link(app):
    h, p, d, dr = _parsed(app,
        [(50000, '100111', '정상승인', '260512')],
        [('100', '합성가', '카드', 50000, '100111', '2026-05-11')],
        [(1, '100', '합성가', '', 30000, 0, 0)])
    linked = app._hansol_card_by_chart(h, p)
    assert linked.get('100', (None, 0))[0] is None
    totals = app.compute_totals(h, d, dr, p)
    assert not [s for s in app.find_channel_suspects('카드', h, d, p, totals=totals) if '★★' in s['출처']]
    verdict = app.build_verification(p, d, dr, hansol=h)['유형C2_금액불일치'].iloc[0]['한솔판정']
    assert '지지' not in verdict and '판단 보류' in verdict


def test_shared_approval_is_not_claimed_as_single_patient(app):
    h, p, d, dr = _parsed(app,
        [(90000, '100111', '정상승인', '260511')],
        [('100', '합성가', '카드', 50000, '100111', '2026-05-11'),
         ('200', '합성나', '카드', 30000, '100111', '2026-05-11')],
        [(1, '100', '합성가', '', 50000, 0, 0), (2, '200', '합성나', '', 30000, 0, 0)])
    totals = app.compute_totals(h, d, dr, p)
    assert not [s for s in app.find_channel_suspects('카드', h, d, p, totals=totals) if '★★' in s['출처']]
    assert all(v[0] is None for v in app._hansol_card_by_chart(h, p).values())


def test_partial_link_excel_keeps_amount_and_difference_blank(app):
    import io
    import openpyxl

    h, p, d, dr = _parsed(app,
        [(30000, '100111', '정상승인', '260511'), (20000, '200222', '정상승인', '260511')],
        [('100', '합성가', '카드', 30000, '100111', '2026-05-11'),
         ('100', '합성가', '카드', 20000, '', '2026-05-11')],
        [(1, '100', '합성가', '', 30000, 0, 0)])
    totals = app.compute_totals(h, d, dr, p)
    data = app.build_ai_excel(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), totals,
                             hansol=h, daily=d, patient=p, daily_refund=dr)
    sheet = openpyxl.load_workbook(io.BytesIO(data))['0_3way통합']
    values = dict(zip([c.value for c in sheet[1]], [c.value for c in sheet[2]]))
    assert values['한솔카드'] is None and values['한-차카드차'] is None
    assert values['차트카드'] == 50000 and values['일-차카드차'] == -20000
