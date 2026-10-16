"""Uploaded identifiers and notes must remain literal text in downloaded workbooks."""
import io

import openpyxl
import pandas as pd
import pytest


@pytest.mark.parametrize("value", ["=1+1", '=HYPERLINK("https://example.com","sample")',
                                  "+SUM(1,2)", "-1+2", "@SUM(1,2)", "#N/A"])
def test_verification_export_keeps_untrusted_text_literal(app, value):
    data = app.build_verification_excel({
        "유형C2_금액불일치": pd.DataFrame({"성명": [value], "차이": [-20000]})
    })
    worksheet = openpyxl.load_workbook(io.BytesIO(data))["유형C2_금액불일치"]
    assert worksheet["A2"].value == value
    assert worksheet["A2"].data_type == "s"
    assert worksheet["B2"].value == -20000
    assert worksheet["B2"].data_type == "n"


def test_reconciliation_export_has_no_executable_formula_cells(app):
    payload = "=1+1"
    totals = dict(_has_hansol=True, h_card=0, d_card=0, p_card=0,
                  h_cash=0, d_cashxfer=0, p_cashxfer=0, d_plat=0, p_plat=0)
    data = app.build_ai_excel(
        pd.DataFrame({"차트번호": [payload], "이름": [payload], "차이": [-5000]}),
        pd.DataFrame({"승인번호": [payload], "금액": [-1000]}),
        pd.DataFrame({"성명": [payload], "카드": [-2000]}), totals,
        suspects_by_channel={"카드": [{"근거": payload}]},
        verif={"유형C1_한쪽만존재": pd.DataFrame({"성명": [payload]})},
    )
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    found = []
    for worksheet in workbook:
        for row in worksheet:
            for cell in row:
                assert cell.data_type != "f", (worksheet.title, cell.coordinate)
                if cell.value == payload:
                    found.append(cell)
                    assert cell.data_type == "s"
    assert len(found) == 6
    assert workbook["1_P1_한솔vs차트"]["C2"].value == -5000
