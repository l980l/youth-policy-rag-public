"""검색어 캐시와 선택 정책의 필수 상세 문서 수집."""
import hashlib
from . import prompts
from .domain import policy_document, clean
from .storage import read_json, write_json
from .collector import YouthAPIError
import json


class SourceCollector:
    def __init__(self, settings, client, model, index):
        self.settings, self.client, self.model, self.index = settings, client, model, index

    def _document(self, policy_id):
        # 최초 수집 때 저장된 공식 상세 문서를 질의 시 재사용한다.
        path = self.settings.data_dir / "documents" / (hashlib.sha256(policy_id.encode()).hexdigest() + ".json")
        document = read_json(path)
        if document is None:
            document = policy_document(self.client.detail(policy_id))
            write_json(path, document)
        return document

    def ensure(self, policy, question, history=None):
        policy_id = policy["policy_id"]
        # 선택 정책 자체의 문서는 LLM 선별·본문 길이와 무관하게 항상 색인한다.
        self.index.index_document(policy_id, self._document(policy_id), policy["name"])
        payload = {"policy": policy["name"], "question": question, "history": (history or [])[-6:]}
        plan_key = hashlib.sha256(json.dumps({"id": policy_id, "payload": payload,
                                              "model": self.settings.llm_model, "prompt": prompts.SEARCH},
                                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        plan_path = self.settings.data_dir / "query_plans" / (plan_key + ".json")
        queries = read_json(plan_path)
        if queries is None:
            result = self.model.json(prompts.SEARCH, payload)
            extracted = result.get("queries", [])
            if not isinstance(extracted, list):
                raise ValueError("검색어 추출 형식이 올바르지 않습니다.")
            queries = list(dict.fromkeys([policy["name"]] + [q.strip()[:100] for q in extracted
                                         if isinstance(q, str) and q.strip()][:3]))
            write_json(plan_path, queries)
        trace = []
        for query in queries:
            # API 검색 결과는 검색어별로, 선별 및 색인은 항목+질문별로 캐시한다.
            search_key = hashlib.sha256(query.casefold().encode()).hexdigest()
            search_path = self.settings.data_dir / "search_cache" / (search_key + ".json")
            rows = read_json(search_path)
            hit = rows is not None
            if rows is None:
                try:
                    rows = self.client.search(query)
                except YouthAPIError:
                    # 선택 정책의 공식 상세는 이미 확보했다. 추가 검색 실패는 알리고 기존 근거를 쓴다.
                    trace.append({"query": query, "cache_hit": False, "selected": [], "rejected": [],
                                  "status": "external_search_unavailable"})
                    continue
                write_json(search_path, rows)
            candidate_map = {clean(r.get("plcyNo")): r for r in rows if clean(r.get("plcyNo"))}
            selection_key = hashlib.sha256((policy_id + "\n" + question + "\n" + query).encode()).hexdigest()
            selection_path = self.settings.data_dir / "selection_cache" / (selection_key + ".json")
            selected_ids = read_json(selection_path)
            if selected_ids is None:
                candidates = [{"id": key, "title": clean(row.get("plcyNm")),
                               "summary": clean(row.get("plcyExplnCn"))[:500]}
                              for key, row in candidate_map.items()]
                selected = self.model.json(prompts.SELECT, {"policy": policy["name"],
                                                            "question": question, "candidates": candidates}) if candidates else {"ids": []}
                values = selected.get("ids", [])
                if not isinstance(values, list):
                    raise ValueError("문서 선별 형식이 올바르지 않습니다.")
                selected_ids = list(dict.fromkeys(i for i in values if isinstance(i, str) and i in candidate_map))[:3]
            for selected_id in selected_ids:
                self.index.index_document(policy_id, self._document(selected_id), query)
            # 실패한 수집/색인은 완료 처리하지 않는다.
            write_json(selection_path, selected_ids)
            trace.append({"query": query, "cache_hit": hit,
                          "selected": selected_ids, "rejected": [i for i in candidate_map if i not in selected_ids]})
        return trace
