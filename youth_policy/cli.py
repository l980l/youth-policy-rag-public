import argparse
import hashlib
import json
from .config import load_settings
from .storage import load_policies, write_json
from .domain import audit, policy_document, evidence_length, select_substantive, now_iso, FIELDS


def main():
    parser = argparse.ArgumentParser(description="청년정책 수집·품질 점검·색인")
    parser.add_argument("command", choices=["collect", "audit", "index", "graph", "evaluate", "check-ui"])
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, help="샘플 수집 건수. 생략하면 전체 수집")
    parser.add_argument("--scan-limit", type=int, help="품질 선별 전에 살펴볼 목록 건수")
    parser.add_argument("--min-body-chars", type=int, default=0, help="실질 본문 최소 길이; 100 권장")
    scope.add_argument("--all", action="store_true", help="건수 제한 없이 API 전체 정책 수집(기본값). 본문 분량으로 제외하지 않음")
    args = parser.parse_args()
    settings = load_settings()
    try:
        if args.command == "collect":
            from .collector import YouthClient
            client = YouthClient(settings)
            if args.all or args.limit is None:
                if args.min_body_chars or args.scan_limit:
                    raise ValueError("전체 수집에는 본문 선별 및 후보 건수 제한을 함께 지정하지 마세요.")

                def page_saved(page, rows, summary):
                    write_json(settings.data_dir / "raw" / "full_pages" / f"page-{page:05d}.json", rows)
                    write_json(settings.data_dir / "full_collection_progress.json", summary)
                    print(f"목록 수집: {summary['unique_count']}/{summary['api_total'] or '전체'}건 · {page}페이지", flush=True)

                policies = client.collect_all(on_page=page_saved)
                if not policies:
                    raise ValueError("전체 목록이 비어 있습니다. 기존 데이터는 유지합니다.")
                # 목록에 상세 본문이 모두 포함됨을 실제 표본으로 확인했다. 필드가 누락된 항목만 상세 조회.
                for position, policy in enumerate(policies):
                    if any(key not in policy["raw"] for key in FIELDS):
                        policies[position] = client.detail(policy["policy_id"])
                for policy in policies:
                    document = policy_document(policy)
                    filename = hashlib.sha256(policy["policy_id"].encode()).hexdigest() + ".json"
                    write_json(settings.data_dir / "documents" / filename, document)
                existing = load_policies(settings.data_dir)
                if existing:
                    write_json(settings.data_dir / "snapshots" / "before_full_collection.json", existing)
                write_json(settings.data_dir / "policies.json", policies)
                write_json(settings.data_dir / "audit.json", audit(policies))
                report = {**client.collection_summary, "mode": "all", "collected_at": now_iso(),
                          "selected": len(policies), "minimum_body_chars": 0,
                          "body_source": "공식 API 목록에 포함된 상세 본문; 필드 누락 시 상세 API 보완"}
                write_json(settings.data_dir / "collection_report.json", report)
                write_json(settings.data_dir / "full_collection_progress.json", report)
                print(json.dumps({**report, "audit": audit(policies)}, ensure_ascii=False, indent=2))
                return
            if args.limit < 1 or args.min_body_chars < 0 or (args.scan_limit is not None and args.scan_limit < args.limit):
                raise ValueError("수집 건수와 품질 선별 설정을 확인하세요.")
            previous = {p["policy_id"]: p for p in load_policies(settings.data_dir)}
            items = client.collect(args.scan_limit or args.limit)
            write_json(settings.data_dir / "raw" / "candidates.json", items)
            scanned = len(items)
            if args.min_body_chars:
                items = select_substantive(items, args.min_body_chars)
            policies = []
            for item in items:
                # 목록과 상세 API가 다른 필드를 제공할 수 있어 실제 상세를 별도 확인한다.
                old = previous.get(item["policy_id"])
                policy = old if old and old["raw"] == item["raw"] else client.detail(item["policy_id"])
                if evidence_length(policy) < args.min_body_chars:
                    continue
                policies.append(policy)
                document = policy_document(policy)
                filename = hashlib.sha256(policy["policy_id"].encode()).hexdigest() + ".json"
                write_json(settings.data_dir / "documents" / filename, document)
                print(f"상세 확보 {len(policies)}/{args.limit}", flush=True)
                if len(policies) >= args.limit:
                    break
            if not policies:
                raise ValueError("수집된 정책이 없습니다. 기존 데이터는 변경하지 않았습니다.")
            write_json(settings.data_dir / "policies.json", policies)
            write_json(settings.data_dir / "audit.json", audit(policies))
            write_json(settings.data_dir / "collection_report.json", {
                "scanned": scanned, "eligible_candidates": len(items), "selected": len(policies),
                "minimum_body_chars": args.min_body_chars,
                "selection": "제외조건·신청방법 존재 및 본문 분량 우선" if args.min_body_chars else "API 반환 순서",
            })
            print(json.dumps(audit(policies), ensure_ascii=False, indent=2))
            if len(policies) < args.limit:
                parser.exit(2, "목표 건수를 확보하지 못했습니다. 데이터 점검이 필요합니다.\n")
        elif args.command == "audit":
            result = audit(load_policies(settings.data_dir))
            write_json(settings.data_dir / "audit.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["gate_100_passed"]:
                parser.exit(2, "100건 및 본문 품질 기준 미충족입니다.\n")
        elif args.command == "check-ui":
            from .ui_validation import run
            run(settings)
        elif args.command == "evaluate":
            from .evaluation import run
            run(settings)
            print("실제 평가 결과: data/evaluation.json, docs/live-evaluation.md")
        elif args.command == "graph":
            from .rag import build_graph
            print(build_graph(None, None).get_graph().draw_mermaid())
        else:
            from .index import VectorIndex
            from .llm import embeddings
            policies = load_policies(settings.data_dir)
            if not policies:
                raise ValueError("정책 데이터가 없습니다. collect를 먼저 실행하세요.")
            index = VectorIndex(settings, embeddings(settings))
            try:
                index.index_policies(policies, progress=lambda done, total: print(f"메타데이터 색인 {done}/{total}", flush=True))
                index.index_documents(((p["policy_id"], policy_document(p)) for p in policies),
                                      progress=lambda done: print(f"상세 문서 색인 {done}/{len(policies)}", flush=True))
                report = {"metadata_count": index.meta.count(), "document_chunk_count": index.docs.count(),
                          "policy_count": len(policies), "completed_at": now_iso()}
                write_json(settings.data_dir / "index_report.json", report)
                print(json.dumps(report, ensure_ascii=False))
            finally:
                index.client.close()
    except Exception as exc:
        # HTTP 오류 문자열에 인증키가 포함될 수 있어 traceback/원시 오류는 출력하지 않는다.
        parser.exit(1, f"작업 실패({type(exc).__name__}): .env의 키 승인 상태, 네트워크, 데이터 및 설치 버전을 확인하세요. "
                        "CHUNK/임베딩 설정 변경 시 DATA_DIR을 새 폴더로 지정하세요.\n")


if __name__ == "__main__":
    main()
