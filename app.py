"""교육용 step7_skeleton.py의 선택→채팅→상태→인용 UI 흐름을 청년정책에 적용."""
import os
import time
import streamlit as st
from youth_policy.config import load_settings
from youth_policy.storage import load_policies
from youth_policy.rag import cited_sources
from youth_policy.discover import explore

st.set_page_config(page_title="청년정책 AI 길잡이", page_icon="🌱", layout="wide")
st.title("🌱 청년정책 AI 길잡이")
st.caption("지원 조건부터 신청 준비까지, 공식 정책 문서의 근거를 함께 확인하세요.")
st.info("수집 시점의 안내입니다. 신청 가능 여부와 중복수혜는 최신 모집공고 및 담당 기관에서 확인하세요.")


def render_sources(answer, sources):
    used = cited_sources(answer, sources)
    if not used:
        st.caption("답변에 인용된 근거가 없습니다.")
        return
    with st.expander(f"답변에 사용한 근거 {len(used)}건"):
        for number, source in used:
            st.markdown(f"**[근거 {number}] {source['title']}**")
            st.caption(f"{source['source_type']} · 수집 {source['collected_at']}")
            if source["url"].startswith("https://"):
                st.link_button("공식 원문 확인", source["url"])
            st.write(source["text"])


@st.cache_resource
def resources(settings):
    from youth_policy.llm import Model, embeddings
    from youth_policy.index import VectorIndex
    from youth_policy.collector import YouthClient
    from youth_policy.sources import SourceCollector
    from youth_policy.rag import build_graph
    model = Model(settings)
    index = VectorIndex(settings, embeddings(settings))
    collector = SourceCollector(settings, YouthClient(settings), model, index)
    return model, index, collector, build_graph(index, model)


try:
    settings = load_settings()
    policies = load_policies(settings.data_dir)
except Exception:
    st.error("설정 또는 데이터 파일을 읽지 못했습니다. .env와 데이터 파일을 확인하세요.")
    st.stop()

if not policies:
    st.warning("정책 데이터가 없습니다. API 키 승인 후 터미널에서 다음 명령을 실행하세요.")
    st.code("python -m youth_policy.cli collect --limit 10\npython -m youth_policy.cli collect --limit 100\npython -m youth_policy.cli index", language="powershell")
    st.stop()

with st.sidebar:
    st.header("정책 선택")
    st.caption(f"수집된 정책 {len(policies)}건")
    if len(policies) < 100:
        st.warning("프로젝트 기준 100건 미충족")
    by_id = {p["policy_id"]: p for p in policies}
    selected = st.selectbox("질문할 정책", list(by_id), index=None,
                            format_func=lambda key: by_id[key]["name"] +
                            (" · " + by_id[key]["raw"]["sprvsnInstCdNm"] if by_id[key]["raw"].get("sprvsnInstCdNm") else ""),
                            key="selected_policy",
                            placeholder="정책을 선택하세요")

missing = []
if not os.getenv("YOUTH_API_KEY"):
    missing.append("YOUTH_API_KEY")
if not (os.getenv("GMS_KEY") or os.getenv("LLM_API_KEY")):
    missing.append("GMS_KEY 또는 LLM_API_KEY")
if missing:
    st.warning(".env에 " + ", ".join(missing) + "를 설정한 뒤 앱을 재시작하세요.")
    st.stop()
try:
    model, index, collector, graph = resources(settings)
except Exception:
    st.error("모델 또는 색인을 준비하지 못했습니다. 설치 버전과 설정을 확인하세요. 설정을 변경했다면 새 DATA_DIR로 재색인하세요.")
    st.stop()
with st.sidebar:
    st.caption(f"메타데이터 색인 {index.meta.count()}건 · 문서 청크 {index.docs.count()}건")
if not index.meta.count():
    st.warning("색인이 없습니다. python -m youth_policy.cli index 를 실행하세요.")
    st.stop()
if index.meta.count() < len(policies):
    st.info("수집된 정책의 검색 색인을 갱신하고 있습니다. 완료 전에는 일부 정책이 검색에서 빠질 수 있습니다.")

st.session_state.setdefault("chats", {})


def select_policy(key):
    st.session_state.selected_policy = key


with st.expander("내 조건으로 정책 찾기", expanded=not bool(selected)):
    st.caption("정책을 먼저 선택하지 않아도 됩니다. 현재 수집된 정책에서 관심 분야와 조건을 함께 살펴봅니다.")
    with st.form("condition_search", enter_to_submit=True):
        conditions = st.text_input("자연어 조건", placeholder="만 25세, 서울 거주, 미취업자가 신청할 수 있는 취업 지원 정책")
        submitted = st.form_submit_button("조건으로 탐색")
    cache_key = (conditions, str(settings.data_dir.resolve()),
                 (settings.data_dir / "policies.json").stat().st_mtime_ns,
                 settings.llm_model, settings.llm_base_url)
    if submitted and not conditions.strip():
        st.info("탐색할 조건을 입력해 주세요.")
    if submitted and conditions.strip() and st.session_state.get("discovery_cache_key") != cache_key:
        started = time.monotonic()
        with st.spinner("정책 조건을 근거와 대조하고 있습니다..."):
            try:
                st.session_state.discovery_report = explore(conditions, policies, index, model)
                st.session_state.discovery_seconds = time.monotonic() - started
                st.session_state.discovery_cache_key = cache_key
            except Exception:
                st.error("조건 탐색에 실패했습니다. 모델 연결과 설정을 확인하세요.")
    report = st.session_state.get("discovery_report")
    if report and st.session_state.get("discovery_cache_key") == cache_key:
        if "discovery_seconds" in st.session_state:
            st.caption(f"최초 탐색 시간 {st.session_state.discovery_seconds:.1f}초 · 같은 조건의 마지막 결과는 재사용합니다.")
        st.caption(f"수집된 정책 {report['policy_count']}건 범위의 결과입니다. 전국 모든 정책을 확인한 결과는 아닙니다.")
        confirmed, pending = report["matches"], report["needs_confirmation"]
        if not confirmed and not pending:
            st.info("현재 수집된 정책에서는 관련 근거를 찾지 못했습니다. 관심 분야를 나눠 검색하거나 공식 정책 검색에서 범위를 넓혀 보세요.")
        elif not confirmed:
            st.info("입력한 조건을 모두 확인한 정책은 아직 없습니다. 아래는 관심 분야와 관련이 있지만 자격 확인이 더 필요한 후보입니다.")
        for label, matches in [("입력 조건이 문서에서 확인된 후보", confirmed),
                                ("관련 정책 · 자격 확인 필요", pending)]:
            if matches:
                st.markdown(f"**{label}**")
            for match in matches:
                p = match["policy"]
                st.markdown(f"**{p['name']}**")
                institution = p["raw"].get("sprvsnInstCdNm")
                if institution:
                    st.caption(f"주관 기관: {institution}")
                st.write(match["evidence"])
                if match["verified_conditions"]:
                    with st.expander("문서로 확인한 입력 조건"):
                        for condition in match["verified_conditions"]:
                            st.write(condition["condition"])
                            st.caption(condition["evidence"])
                if match["unverified_conditions"]:
                    st.write("추가 확인: " + " / ".join(match["unverified_conditions"]))
                st.caption("현재 접수 여부와 최종 신청 자격은 최신 모집공고에서 확인하세요.")
                st.link_button("조건 확인 원문", p["source_url"])
                st.button("이 정책에 질문하기", key="pick_" + p["policy_id"],
                          on_click=select_policy, args=(p["policy_id"],))
        if report.get("questions"):
            with st.expander("조건을 더 정확히 확인하려면"):
                for follow_up in report["questions"]:
                    st.write(follow_up)
    elif report:
        st.caption("조건 또는 정책 데이터가 바뀌었습니다. ‘조건으로 탐색’을 다시 눌러 주세요.")

if not selected:
    st.info("왼쪽에서 정책을 선택하거나 조건으로 찾아보세요.")
    st.stop()

policy = by_id[selected]
st.subheader(policy["name"])
st.caption(f"{policy['category']} · 수집 {policy['collected_at']}")
if policy.get("evidence_quality") == "limited":
    st.warning("이 정책은 수집된 상세 설명이 적습니다. 자격·신청방법·제외조건은 공식 원문에서 추가 확인이 필요할 수 있습니다.")
messages = st.session_state.chats.setdefault(selected, [])
with st.sidebar:
    if st.button("이 정책의 대화 지우기"):
        messages.clear()
        st.rerun()

for message in messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            render_sources(message["content"], message.get("sources", []))

checklist = st.button("신청 준비 체크리스트 만들기")
question = st.chat_input("지원 조건, 제출 서류, 제외 조건을 물어보세요")
if checklist:
    question = "이 정책의 신청 준비 체크리스트를 조건, 서류, 신청경로, 제외조건, 추가 확인사항으로 정리해 줘."
if question:
    history = [{"role": m["role"], "content": m["content"]} for m in messages[-6:]]
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        started = time.monotonic()
        try:
            with st.status("검색어 추출 · 공식 문서 선별 · 수집 · 색인 중...", expanded=False) as status:
                trace = collector.ensure(policy, question, history)
                partial = isinstance(trace, list) and any(t.get("status") == "external_search_unavailable" for t in trace)
                status.update(label="근거 문서 준비 완료", state="complete")
                if partial:
                    st.warning("일부 추가 문서를 검색하지 못했습니다. 확보된 공식 문서를 바탕으로 답변합니다.")
            with st.spinner("근거 검색과 답변 생성 중..."):
                result = graph.invoke({"policy_id": selected, "question": question, "history": history})
            st.markdown(result["answer"])
            render_sources(result["answer"], result["sources"])
            st.caption(f"처리 시간 {time.monotonic() - started:.1f}초")
            messages.append({"role": "assistant", "content": result["answer"], "sources": result["sources"]})
        except Exception:
            st.error("요청을 처리하지 못했습니다. API 승인 상태, 모델 사용 권한, 네트워크와 색인을 확인하세요.")
