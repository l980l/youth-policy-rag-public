import os
import time
import requests
from .domain import normalize


class YouthAPIError(RuntimeError):
    pass


def parse_response(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        raise YouthAPIError("API 응답 형식이 예상과 다릅니다.")
    if data.get("errorCode") or data.get("resultCode") not in (None, 0, "0", "200", 200):
        raise YouthAPIError("API 인증 또는 요청 오류입니다. 키 승인 상태와 요청 조건을 확인하세요.")
    result = data.get("result")
    if not isinstance(result, dict) or "youthPolicyList" not in result:
        raise YouthAPIError("정책 목록이 없습니다. API 승인 상태와 응답 명세를 확인하세요.")
    rows = result["youthPolicyList"]
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise YouthAPIError("정책 목록 형식이 예상과 다릅니다.")
    return rows


class YouthClient:
    def __init__(self, settings, api_key=None, session=None):
        self.settings = settings
        self.key = api_key or os.getenv("YOUTH_API_KEY", "")
        if not self.key:
            raise YouthAPIError(".env에 승인된 YOUTH_API_KEY를 설정하세요.")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "YouthPolicyRAG-Education/0.1"})
        self.last_request = 0.0
        self.pagination = {}
        self.collection_summary = {}

    def request(self, **params) -> list[dict]:
        for attempt in range(4):
            time.sleep(max(0, self.settings.request_interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                r = self.session.get(self.settings.youth_api_url, params={
                    "apiKeyNm": self.key, "rtnType": "json", **params,
                }, timeout=30)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < 3:
                        delay = r.headers.get("Retry-After", "")
                        # 정상적인 숫자 Retry-After를 따르고, 그 외에는 지수 대기한다.
                        time.sleep(float(delay) if delay.replace(".", "", 1).isdigit() else 2 ** attempt)
                        continue
                if r.status_code != 200:
                    raise YouthAPIError(f"정책 API 요청 실패(HTTP {r.status_code}). 승인 상태를 확인하세요.")
                data = r.json()
                rows = parse_response(data)
                paging = data.get("result", {}).get("pagging", {})
                self.pagination = paging if isinstance(paging, dict) else {}
                return rows
            except requests.RequestException:
                if attempt == 3:
                    raise YouthAPIError("정책 API에 연결하지 못했습니다. 네트워크를 확인하세요.") from None
                time.sleep(2 ** attempt)
            except ValueError:
                raise YouthAPIError("정책 API가 JSON을 반환하지 않았습니다.") from None
        raise YouthAPIError("정책 API 재시도 횟수를 초과했습니다.")

    def collect(self, limit: int = 100) -> list[dict]:
        if limit < 1:
            raise ValueError("수집 건수는 1 이상이어야 합니다.")
        found = {}
        for page in range(1, limit + 2):
            rows = self.request(pageNum=page, pageSize=min(limit, 10), pageType="1")
            before = len(found)
            for raw in rows:
                p = normalize(raw)
                found[p["policy_id"]] = p
                if len(found) >= limit:
                    return list(found.values())
            if not rows or len(found) == before:
                break
        return list(found.values())

    def search(self, query: str) -> list[dict]:
        # 목록의 본문은 후보 선별에 쓰지 않는다. 선별된 ID만 detail()로 조회한다.
        return self.request(pageNum=1, pageSize=10, pageType="1", plcyNm=query)

    def collect_all(self, page_size: int = 100, on_page=None) -> list[dict]:
        """API 전체 건수 또는 빈 마지막 페이지까지 수집한다. 반복 페이지는 성공으로 처리하지 않는다."""
        if not 1 <= page_size <= 100:
            raise ValueError("페이지 크기는 1~100으로 지정하세요.")
        found, expected, page = {}, None, 1
        while True:
            rows = self.request(pageNum=page, pageSize=page_size, pageType="1")
            value = self.pagination.get("totCount")
            if value is not None:
                try:
                    total = int(value)
                except (ValueError, TypeError):
                    raise YouthAPIError("API 전체 건수 형식을 확인하세요.") from None
                if total < 0 or (expected is not None and total != expected):
                    raise YouthAPIError("수집 중 API 전체 건수가 변경되었습니다. 목록을 다시 수집하세요.")
                expected = total
            before = len(found)
            for raw in rows:
                p = normalize(raw)
                found[p["policy_id"]] = p
            self.collection_summary = {"api_total": expected, "unique_count": len(found),
                                       "pages": page, "page_size": page_size, "complete": False}
            if on_page:
                on_page(page, rows, dict(self.collection_summary))
            if expected is not None and len(found) == expected:
                self.collection_summary["complete"] = True
                return list(found.values())
            if not rows:
                if expected is not None and len(found) < expected:
                    raise YouthAPIError("API 전체 건수보다 적은 정책만 반환됐습니다. 전체 수집 완료로 처리하지 않습니다.")
                self.collection_summary["complete"] = True
                return list(found.values())
            if len(found) == before or (expected is not None and len(found) > expected):
                raise YouthAPIError("API 페이지가 반복되거나 전체 건수와 맞지 않습니다. 전체 수집 완료로 처리하지 않습니다.")
            page += 1

    def detail(self, policy_id: str) -> dict:
        rows = self.request(pageNum=1, pageSize=1, pageType="2", plcyNo=policy_id)
        for row in rows:
            if str(row.get("plcyNo")) == policy_id:
                return normalize(row)
        raise YouthAPIError("선택한 정책의 상세 문서가 반환되지 않았습니다.")
