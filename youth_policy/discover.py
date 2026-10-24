from . import prompts
from .domain import clean, policy_document
import re


def valid_quote(quote, text):
    return isinstance(quote, str) and bool(quote.strip()) and " ".join(quote.split()) in " ".join(text.split())


def discover(question, policies, index, model):
    by_id = {p["policy_id"]: p for p in policies}
    candidates = index.candidates(question)
    bodies = {c["policy_id"]: policy_document(by_id[c["policy_id"]])["text"]
              for c in candidates if c["policy_id"] in by_id}
    result = model.json(prompts.DISCOVER, {"conditions": question,
        "candidates": [{"id": key, "text": text} for key, text in bodies.items()]})
    matches = result.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError("조건 탐색 결과 형식이 올바르지 않습니다.")
    found, seen = [], set()
    for match in matches:
        if not isinstance(match, dict):
            continue
        key, quote = match.get("id"), match.get("evidence")
        if (isinstance(key, str) and key in bodies and key not in seen and isinstance(quote, str)
                and quote.strip() and " ".join(quote.split()) in " ".join(bodies[key].split())):
            found.append({"policy": by_id[key], "evidence": quote})
            seen.add(key)
    return found


def explore(question, policies, index, model):
    """확인된 조건과 미확인 조건을 구분한다. 명백히 불충족인 후보는 표시하지 않는다."""
    profile = model.json(prompts.PROFILE, {"input": question})
    constraints = profile.get("constraints", [])
    if not isinstance(constraints, list):
        raise ValueError("조건 분석 형식을 확인하세요.")
    required = {}
    for item in constraints[:12]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("description"), str):
            required[item["id"]] = item["description"][:200]
    if question.strip() and not required and not profile.get("preferences"):
        raise ValueError("조건과 선호를 분석하지 못했습니다.")
    queries = profile.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError("탐색 검색어 형식을 확인하세요.")
    queries = list(dict.fromkeys([question] + [q[:150] for q in queries if isinstance(q, str) and q.strip()][:3]))
    by_id = {p["policy_id"]: p for p in policies}
    ranked = {}
    for candidates in index.candidates_many(queries, k=15):
        for rank, candidate in enumerate(candidates):
            key = candidate.get("policy_id")
            if key in by_id:
                ranked[key] = ranked.get(key, 0) + 1 / (60 + rank)
    ids = sorted(ranked, key=ranked.get, reverse=True)[:30]
    bodies = {key: policy_document(by_id[key])["text"] for key in ids}
    if not bodies:
        return {"query": question, "matches": [], "needs_confirmation": [], "questions": [], "policy_count": len(policies)}
    selected = model.json(prompts.EXPLORE, {
        "input": question, "constraints": [{"id": k, "description": v} for k, v in required.items()],
        "preferences": profile.get("preferences", []),
        # 긴 지역 코드·URL은 자격 판단 근거로 해석하지 않게 하고 본문 검토에 집중한다.
        # 발췌 검증은 아래에서 생략 전의 실제 원문으로 수행한다.
        "candidates": [{"id": key, "text": re.sub(
            r"(?m)^(?:대상 지역 코드|신청 URL|참고 URL [12]):[^\n]*(?:\n|$)", "", text),
            "region_note": "지역 코드의 행정구역은 미확인. 기관 소재지만으로 거주 요건을 추정하지 말 것."}
            for key, text in bodies.items()],
    }).get("candidates", [])
    if not isinstance(selected, list):
        raise ValueError("조건 검토 결과 형식을 확인하세요.")
    confirmed, pending, seen = [], [], set()
    allowed_checks = {"연령 기준", "소득 기준", "취업 상태 기준", "학력·전공 기준", "주민등록 주소·거주기간", "중복참여 제한"}
    for row in selected:
        if not isinstance(row, dict):
            continue
        key, quote = row.get("id"), row.get("relevance_evidence")
        if not isinstance(key, str) or key not in bodies or key in seen or not valid_quote(quote, bodies[key]):
            continue
        values = row.get("checks", [])
        if not isinstance(values, list):
            continue
        checks = {c["condition_id"]: c for c in values if isinstance(c, dict)
                  and isinstance(c.get("condition_id"), str) and c["condition_id"] in required}
        # 근거를 제시한 명백한 불충족은 후보로 추천하지 않는다.
        if any(c.get("status") == "unmet" for c in checks.values()):
            continue
        verified, unknown = [], []
        for condition_id, description in required.items():
            check = checks.get(condition_id, {})
            evidence = check.get("evidence", "")
            if check.get("status") == "met" and valid_quote(evidence, bodies[key]):
                verified.append({"condition": description, "evidence": evidence})
            else:
                unknown.append(description)
        additional = row.get("additional_checks", [])
        if not isinstance(additional, list):
            additional = []
        # 모델이 만든 일반 경고만으로 자격 미확인으로 분류하지 않는다.
        # 추가 요건도 실제 본문의 발췌가 있어야 분류 근거로 사용할 수 있다.
        requirements = []
        raw = by_id[key].get("raw", {})
        requirement_texts = [clean(raw.get(field)) for field in (
            "plcyExplnCn", "plcySprtCn", "addAplyQlfcCndCn", "ptcpPrpTrgtCn", "earnEtcCn")]
        for check in additional:
            if not isinstance(check, dict):
                continue
            condition, evidence = check.get("condition"), check.get("evidence")
            if (isinstance(condition, str) and condition in allowed_checks
                    # 발췌에서 '전 국민'을 잘라냈더라도 원래 필드의 맥락을 확인한다.
                    and any(valid_quote(evidence, text) and not re.search(
                        r"전\s*국민|모든\s*국민|(?:연령|소득|취업|학력|거주)[^\n.]{0,12}제한\s*없", text)
                            for text in requirement_texts)):
                unknown.append(condition)
                requirements.append({"condition": condition, "evidence": evidence})
        match = {"policy": by_id[key], "evidence": quote, "verified_conditions": verified,
                 "additional_requirements": requirements,
                 "unverified_conditions": list(dict.fromkeys(unknown)),
                 "status": "needs_confirmation" if unknown else "confirmed"}
        (pending if unknown else confirmed).append(match)
        seen.add(key)
        if len(seen) >= 6:
            break
    questions = profile.get("questions", [])
    return {"query": question, "matches": confirmed, "needs_confirmation": pending,
            "questions": [q[:200] for q in questions if isinstance(q, str)][:3] if isinstance(questions, list) else [],
            "policy_count": len(policies)}
