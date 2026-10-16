"""보조 화면도 공통 환불 순금액을 사용하는지 합성 자료로 검증한다."""
import pandas as pd


def test_two_file_card_detail_deducts_partial_refund(app):
    daily = pd.DataFrame([{"차트번호": "101", "성명": "예시 A", "총액": 100000, "카드": 100000}])
    refund = pd.DataFrame([{"차트번호": "101", "성명": "예시 A", "총액": 20000, "카드": 20000}])
    patient = pd.DataFrame([{"차트번호": "101", "이름": "예시 A", "분류": "카드", "금액": 80000}])

    result = app._two_file_card_comparison(patient, daily, refund)

    assert result["구글 일일마감 카드"].tolist() == [80000]
    assert result["베가스 마감 카드"].tolist() == [80000]
    assert result["차이(일일마감-베가스)"].tolist() == [0]


def test_two_file_card_detail_keeps_refund_only_and_offsetting_differences(app):
    daily = pd.DataFrame([{"차트번호": "102", "성명": "예시 B", "총액": 50000, "카드": 50000}])
    refund = pd.DataFrame([{"차트번호": "101", "성명": "예시 A", "총액": 20000, "카드": 20000}])
    patient = pd.DataFrame([
        {"차트번호": "101", "이름": "예시 A", "분류": "카드", "금액": -10000},
        {"차트번호": "102", "이름": "예시 B", "분류": "카드", "금액": 40000},
    ])

    result = app._two_file_card_comparison(patient, daily, refund).set_index("차트번호")

    assert result.loc["101", "구글 일일마감 카드"] == -20000
    assert result["차이(일일마감-베가스)"].to_dict() == {"101": -10000, "102": 10000}
    assert result["차이(일일마감-베가스)"].sum() == 0


def test_two_file_card_detail_retains_full_refund_zero(app):
    daily = pd.DataFrame([{"차트번호": "101", "성명": "예시 A", "총액": 20000, "카드": 20000}])
    patient = pd.DataFrame([
        {"차트번호": "101", "이름": "예시 A", "분류": "카드", "금액": 20000},
        {"차트번호": "101", "이름": "예시 A", "분류": "카드", "금액": -20000},
    ])

    result = app._two_file_card_comparison(patient, daily, daily.copy())

    assert len(result) == 1
    assert result["구글 일일마감 카드"].tolist() == [0]
    assert result["차이(일일마감-베가스)"].tolist() == [0]
