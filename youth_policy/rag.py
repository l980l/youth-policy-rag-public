import re
from typing import TypedDict
from . import prompts

CITE_PATTERN = re.compile(r"\[근거\s*([\d,\s]+)\]")


def citation_numbers(answer: str) -> set[int]:
    return {int(n.strip()) for group in CITE_PATTERN.findall(answer)
            for n in group.split(",") if n.strip().isdigit()}


def cited_sources(answer, sources):
    numbers = citation_numbers(answer)
    return [(i, s) for i, s in enumerate(sources, start=1) if i in numbers]


def validate_answer(answer, sources):
    numbers = citation_numbers(answer)
    if any(n < 1 or n > len(sources) for n in numbers):
        return "문서에서 확인되지 않습니다. 생성된 답변의 근거 번호가 유효하지 않아 답변을 보류했습니다."
    if answer.strip() and not numbers:
        # 출처 없는 일반 지식 답변을 그대로 서비스하지 않는다.
        return "문서에서 확인되지 않습니다. 질문을 뒷받침하는 유효한 근거 인용이 없습니다."
    return answer.strip() or "문서에서 확인되지 않습니다."


class RAGState(TypedDict, total=False):
    policy_id: str
    question: str
    history: list[dict]
    sources: list[dict]
    answer: str


def build_graph(index, model):
    from langgraph.graph import StateGraph, START, END

    def retrieve(state):
        # 후속 질문의 지시어가 검색에서 사라지지 않게 최근 사용자 질문을 함께 사용한다.
        recent = [m["content"] for m in state.get("history", [])[-6:] if m["role"] == "user"]
        query = "\n".join(recent + [state["question"]])
        return {"sources": index.retrieve(state["policy_id"], query)}

    def generate(state):
        sources = state.get("sources", [])
        if not sources:
            return {"answer": "문서에서 확인되지 않습니다."}
        requested_years = set(re.findall(r"(?<!\d)((?:19|20)\d{2})\s*년(?!생)", state["question"]))
        if requested_years and any(word in state["question"] for word in ["일정", "신청", "시작", "모집", "접수", "개시"]):
            available = set(re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", "\n".join(s["text"] for s in sources)))
            missing = requested_years - available
            if missing:
                years = ", ".join(sorted(missing))
                return {"answer": f"문서에서 {years}년의 신청·모집 일정은 확인되지 않습니다. 해당 연도의 최신 공식 공고를 확인해 주세요."}
        context = [{"number": i, "title": s["title"], "type": s["source_type"],
                    "text": s["text"], "collected_at": s["collected_at"]}
                   for i, s in enumerate(sources, start=1)]
        answer = model.text(prompts.ANSWER, {"question": state["question"],
                                            "history": state.get("history", [])[-6:], "evidence": context})
        return {"answer": validate_answer(answer, sources)}

    graph = StateGraph(RAGState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate", generate)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()
