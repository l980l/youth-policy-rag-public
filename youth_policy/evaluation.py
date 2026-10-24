"""실제 API·모델을 사용하는 재현 가능한 평가. 정답은 수집한 필드 원문에서 구성한다."""
import json
import time
from .config import ROOT
from .domain import clean, now_iso
from .storage import load_policies, read_json, write_json
from .rag import build_graph, citation_numbers


def make_cases(policies):
    specs = [
        ("support", "plcySprtCn", "이 정책은 무엇을 얼마나 지원하나요? 지원 내용을 근거와 함께 설명해 줘."),
        ("application", "plcyAplyMthdCn", "이 정책의 신청 방법과 제출 서류를 공식 안내에 근거해 정리해 줘."),
        ("exclusion", "ptcpPrpTrgtCn", "이 정책에서 참여가 제한되는 대상과 제외 조건은 무엇인가요?"),
        ("eligibility", "addAplyQlfcCndCn", "이 정책의 추가 신청 자격 조건을 구체적으로 알려 줘."),
    ]
    cases, used = [], set()
    for case_id, field, question in specs:
        candidates = sorted((p for p in policies if len(clean(p["raw"].get(field))) > 20),
                            key=lambda p: (p["policy_id"] not in used, len(clean(p["raw"].get(field)))), reverse=True)
        if not candidates:
            continue
        policy = candidates[0]
        used.add(policy["policy_id"])
        cases.append({"id": case_id, "policy_id": policy["policy_id"], "question": question,
                      "expected_field": field, "expected_text": clean(policy["raw"][field]),
                      "expect_unknown": False})
    if policies:
        cases.append({"id": "unknown", "policy_id": policies[0]["policy_id"],
                      "question": "2035년에 이 정책의 신청이 시작되는 확정 날짜를 알려 줘. 현재 연도의 일정으로 대신 답하지 마.",
                      "expected_field": "", "expected_text": "현재 수집 문서에 2035년 확정 일정 없음",
                      "expect_unknown": True})
    return cases


def normalized(text):
    return " ".join(text.split())


def run(settings):
    from .collector import YouthClient
    from .llm import Model, embeddings
    from .index import VectorIndex
    from .sources import SourceCollector
    from .discover import explore
    policies = load_policies(settings.data_dir)
    if not policies:
        raise ValueError("수집된 정책이 없습니다.")
    by_id = {p["policy_id"]: p for p in policies}
    model = Model(settings)
    index = VectorIndex(settings, embeddings(settings))
    client = YouthClient(settings)
    collector = SourceCollector(settings, client, model, index)
    graph = build_graph(index, model)
    report = {"started_at": now_iso(), "model": settings.llm_model,
              "embedding_model": settings.embedding_model, "cases": [], "discovery": []}
    try:
        for case in make_cases(policies):
            started = time.monotonic()
            policy = by_id[case["policy_id"]]
            trace = collector.ensure(policy, case["question"])
            result = graph.invoke({"policy_id": policy["policy_id"], "question": case["question"], "history": []})
            sources, answer = result["sources"], result["answer"]
            excerpt = normalized(case["expected_text"])[:60]
            retrieval_hit = (any(excerpt in normalized(s["text"]) for s in sources)
                             if not case["expect_unknown"] else None)
            numbers = citation_numbers(answer)
            record = {**case, "policy_name": policy["name"], "source_url": policy["source_url"],
                      "answer": answer, "sources": sources, "trace": trace,
                      "seconds": round(time.monotonic() - started, 2), "expected_excerpt_retrieved": retrieval_hit,
                      "citation_numbers_valid": all(1 <= n <= len(sources) for n in numbers),
                      "has_citations": bool(numbers),
                      "uncertainty_stated": any(t in answer for t in ["확인되지", "확인할 수 없", "명시되어 있지", "포함되어 있지", "알 수 없", "확정할 수 없"]),
                      "semantic_review": "수동 검토 필요"}
            report["cases"].append(record)
            write_json(settings.data_dir / "evaluation.json", report)
            print(f"평가 {case['id']}: 근거 {len(sources)}건, {record['seconds']}초", flush=True)

        # 같은 질문을 다시 수집하여 검색어 캐시 동작과 API 재호출 건수를 기록한다.
        first = report["cases"][0]
        original_get = client.session.get
        request_calls = []

        def tracked_get(*args, **kwargs):
            request_calls.append(1)
            return original_get(*args, **kwargs)

        client.session.get = tracked_get
        start = time.monotonic()
        repeated_trace = collector.ensure(by_id[first["policy_id"]], first["question"])
        repeated = graph.invoke({"policy_id": first["policy_id"], "question": first["question"], "history": []})
        report["repeat"] = {"seconds": round(time.monotonic() - start, 2),
                            "external_api_calls": len(request_calls), "trace": repeated_trace,
                            "answer": repeated["answer"]}

        # 넓은 탐색과 충족할 수 없는 조건을 함께 점검한다.
        for query in ["자격증 응시료를 지원하는 정책을 찾아줘", "만 150세이며 화성에 거주하는 사람에게만 지원하는 정책"]:
            explored = explore(query, policies, index, model)
            found = explored["matches"]
            report["discovery"].append({"query": query,
                "matches": [{"policy_id": m["policy"]["policy_id"], "name": m["policy"]["name"],
                             "evidence": m["evidence"]} for m in found],
                "needs_confirmation": [{"policy_id": m["policy"]["policy_id"], "name": m["policy"]["name"],
                                        "evidence": m["evidence"], "unverified_conditions": m["unverified_conditions"]}
                                       for m in explored["needs_confirmation"]]})
        report["completed_at"] = now_iso()
        write_json(settings.data_dir / "evaluation.json", report)
        lines = ["# 실제 RAG 실행 결과", "", f"실행일: {report['completed_at']}", "",
                 "정답 기준은 API의 실제 필드 원문입니다. 자동 지표는 검색·인용 형식 검사이며 사실 정확성 전체를 보증하지 않습니다.", ""]
        for case in report["cases"]:
            lines += [f"## {case['id']}: {case['policy_name']}", "", f"질문: {case['question']}", "",
                      f"기대 원문 필드: `{case['expected_field'] or '해당 없음'}`", "",
                      case["expected_text"], "", "### 실제 답변", "", case["answer"], "",
                      f"검색 원문 포함: {case['expected_excerpt_retrieved']} / 유효 인용: {case['citation_numbers_valid']} / {case['seconds']}초", "",
                      f"[정책 원문]({case['source_url']})", ""]
        lines += ["## 반복 질의 및 조건 탐색", "", f"반복 질의 외부 정책 API 호출: {report['repeat']['external_api_calls']}회", "",
                  f"반복 질의 소요: {report['repeat']['seconds']}초", ""]
        for item in report["discovery"]:
            lines += [f"- {item['query']}: 조건 확인 {len(item['matches'])}건 / 추가 확인 {len(item.get('needs_confirmation', []))}건"]
        (ROOT / "docs" / "live-evaluation.md").write_text("\n".join(lines), encoding="utf-8")
        return report
    finally:
        index.client.close()
