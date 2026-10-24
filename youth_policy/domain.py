"""온통청년 필드를 변환한다. 코드값과 누락값을 임의의 자격 조건으로 해석하지 않는다."""
import html
import re
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

API_DOC_URL = "https://www.youthcenter.go.kr/cmnFooter/openapiIntro/oaiDoc"
TERMS_URL = "https://www.youthcenter.go.kr/cmnFooter/termsInfo"
USAGE_CONDITIONS = "온통청년 이용약관과 API 승인 조건 적용. 원문 재배포·상업적 이용은 개별 자료의 이용조건을 별도 확인."
FIELDS = {
    "plcyExplnCn": "정책 설명", "plcySprtCn": "지원 내용",
    "sprtTrgtAgeLmtYn": "연령 제한 여부", "sprtTrgtMinAge": "최소 연령",
    "sprtTrgtMaxAge": "최대 연령", "zipCd": "대상 지역 코드",
    "addAplyQlfcCndCn": "추가 신청 자격", "ptcpPrpTrgtCn": "참여 제한 대상",
    "earnCndSeCd": "소득 조건 코드", "earnMinAmt": "최소 소득 금액(원시값)",
    "earnMaxAmt": "최대 소득 금액(원시값)", "earnEtcCn": "소득 기타 조건",
    "jobCd": "취업 상태 코드", "schoolCd": "학력 코드",
    "plcyAplyMthdCn": "신청 방법", "srngMthdCn": "심사 및 발표",
    "sbmsnDcmntCn": "제출 서류", "aplyYmd": "신청 기간",
    "aplyPrdSeCd": "신청 기간 구분 코드", "bizPrdBgngYmd": "사업 시작일",
    "bizPrdEndYmd": "사업 종료일", "bizPrdEtcCn": "사업 기간 기타",
    "etcMttrCn": "기타 사항", "sprvsnInstCdNm": "주관 기관",
    "operInstCdNm": "운영 기관", "aplyUrlAddr": "신청 URL",
    "refUrlAddr1": "참고 URL 1", "refUrlAddr2": "참고 URL 2",
}
EVIDENCE_FIELDS = ["plcySprtCn", "addAplyQlfcCndCn", "ptcpPrpTrgtCn",
                   "earnEtcCn", "plcyAplyMthdCn", "sbmsnDcmntCn", "etcMttrCn"]


def clean(value) -> str:
    if value is None:
        return ""
    return html.unescape(re.sub(r"<[^>]*>", " ", str(value))).strip()


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def normalize(raw: dict) -> dict:
    policy_id, title = clean(raw.get("plcyNo")), clean(raw.get("plcyNm"))
    if not policy_id or not title:
        raise ValueError("정책번호(plcyNo) 또는 정책명(plcyNm)이 없습니다. API 명세를 확인하세요.")
    return {
        "policy_id": policy_id, "name": title,
        "category": clean(raw.get("lclsfNm")),
        "subcategory": clean(raw.get("mclsfNm")),
        "source_url": "https://www.youthcenter.go.kr/youthPolicy/ythPlcyTotalSearch/ythPlcyDetail/" + policy_id,
        "collected_at": now_iso(), "source_type": "온통청년 Open API",
        "terms_reference": TERMS_URL, "api_reference": API_DOC_URL,
        "usage_conditions": USAGE_CONDITIONS,
        "evidence_quality": "substantive" if sum(len(clean(raw.get(k))) for k in EVIDENCE_FIELDS) >= 100 else "limited",
        "raw": raw,
    }


def metadata_text(policy: dict) -> str:
    raw = policy["raw"]
    names = ["plcyExplnCn", "plcyKywdNm", "sprvsnInstCdNm", "zipCd",
             "sprtTrgtAgeLmtYn", "sprtTrgtMinAge", "sprtTrgtMaxAge", "jobCd", "earnEtcCn"]
    lines = [f"정책명: {policy['name']}", f"분야: {policy['category']} / {policy['subcategory']}"]
    lines += [f"{FIELDS.get(k, k)}: {clean(raw[k])}" for k in names if clean(raw.get(k))]
    return "\n".join(lines)


def policy_document(policy: dict) -> dict:
    raw = policy["raw"]
    body = "\n".join(f"{label}: {clean(raw[k])}" for k, label in FIELDS.items() if clean(raw.get(k)))
    text = f"정책명: {policy['name']}\n{body}"
    groups = {
        "지원 내용": ["plcySprtCn"],
        "신청 자격 및 대상": ["sprtTrgtAgeLmtYn", "sprtTrgtMinAge", "sprtTrgtMaxAge", "addAplyQlfcCndCn",
                          "earnCndSeCd", "earnMinAmt", "earnMaxAmt", "earnEtcCn", "jobCd", "schoolCd"],
        "참여 제한 및 제외조건": ["ptcpPrpTrgtCn"],
        "신청 방법 및 제출 서류": ["plcyAplyMthdCn", "srngMthdCn", "sbmsnDcmntCn"],
        "신청 기간 및 사업 기간": ["aplyYmd", "aplyPrdSeCd", "bizPrdBgngYmd", "bizPrdEndYmd", "bizPrdEtcCn"],
        "정책 개요 및 기타": ["plcyExplnCn", "etcMttrCn", "sprvsnInstCdNm", "operInstCdNm"],
    }
    sections = []
    for label, keys in groups.items():
        value = "\n".join(f"{FIELDS[k]}: {clean(raw[k])}" for k in keys if clean(raw.get(k)))
        if value:
            sections.append({"label": label, "text": value})
    return {
        "document_id": policy["policy_id"], "title": policy["name"] + " · 정책 상세",
        "url": policy["source_url"], "text": text, "policy_name": policy["name"],
        "sections": sections, "sections_text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "source_type": "온통청년 API 정책 상세", "collected_at": policy["collected_at"],
        "terms_reference": policy["terms_reference"], "usage_conditions": policy["usage_conditions"],
    }


def evidence_length(policy: dict) -> int:
    return sum(len(clean(policy["raw"].get(k))) for k in EVIDENCE_FIELDS)


def select_substantive(policies: list[dict], minimum: int = 100) -> list[dict]:
    """본문이 있는 후보를 우선한다. 분량은 사실 정확성의 판정 기준이 아니다."""
    eligible = [p for p in policies if evidence_length(p) >= minimum]
    return sorted(eligible, key=lambda p: (
        bool(clean(p["raw"].get("ptcpPrpTrgtCn"))),
        bool(clean(p["raw"].get("plcyAplyMthdCn"))),
        evidence_length(p)), reverse=True)


def audit(policies: list[dict]) -> dict:
    ids = {p["policy_id"] for p in policies}
    rich = [p for p in policies if evidence_length(p) >= 100]
    return {
        "collected_at": now_iso(), "policy_count": len(policies), "unique_count": len(ids),
        "has_source_count": sum(bool(p.get("source_url")) for p in policies),
        "substantive_body_count": len(rich),
        "missing_exclusion_count": sum(not clean(p["raw"].get("ptcpPrpTrgtCn")) for p in policies),
        "missing_application_count": sum(not clean(p["raw"].get("plcyAplyMthdCn")) for p in policies),
        "gate_100_passed": (len(ids) >= 100 and len(ids) == len(policies) and len(rich) >= 100
                            and all(p.get("source_url") for p in policies)),
        "note": "본문 100자 기준은 초기 품질 점검이며, 제외조건·중복수혜 근거의 충분성을 보증하지 않습니다.",
    }
