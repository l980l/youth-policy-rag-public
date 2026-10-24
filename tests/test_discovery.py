import unittest
from unittest.mock import Mock
from youth_policy.discover import explore
from youth_policy.domain import normalize


class ExploreTests(unittest.TestCase):
    def test_query_batch_preserves_order_and_uses_one_embedding_request(self):
        from youth_policy.index import VectorIndex
        index = VectorIndex.__new__(VectorIndex)
        index.meta, index.embedder = Mock(), Mock(spec=["embed_documents"])
        index.meta.count.return_value = 2
        index.embedder.embed_documents.return_value = [[1., 0.], [0., 1.]]
        index.meta.query.return_value = {"ids": [["a"], ["b"]], "documents": [["first"], ["second"]],
                                        "metadatas": [[{"policy_id": "a"}], [{"policy_id": "b"}]]}
        result = index.candidates_many(["취업", "주거"], k=15)
        self.assertEqual([rows[0]["policy_id"] for rows in result], ["a", "b"])
        index.embedder.embed_documents.assert_called_once_with(["취업", "주거"])
        index.meta.query.assert_called_once_with(query_embeddings=[[1., 0.], [0., 1.]],
                                                 n_results=2, include=["documents", "metadatas"])

    def setUp(self):
        self.policy = normalize({"plcyNo": "test", "plcyNm": "가상 취업교육",
                                 "plcySprtCn": "백엔드 개발 취업 교육 지원",
                                 "sprtTrgtMinAge": "19", "sprtTrgtMaxAge": "35"})
        self.index, self.model = Mock(), Mock()
        self.index.candidates_many.return_value = [[{"policy_id": "test"}], [{"policy_id": "test"}]]
        self.profile = {"constraints": [{"id": "age", "description": "만 30세"},
                                        {"id": "region", "description": "대전 거주"}],
                        "preferences": ["백엔드 개발"], "queries": ["개발 취업교육"], "questions": []}
        self.candidate = {"id": "test", "relevance_evidence": "백엔드 개발 취업 교육 지원",
                          "checks": [{"condition_id": "age", "status": "met", "evidence": "최소 연령: 19\n최대 연령: 35"},
                                     {"condition_id": "region", "status": "unknown", "evidence": ""}],
                          "additional_checks": ["취업 상태 기준"]}

    def run_explore(self):
        self.model.json.side_effect = [self.profile, {"candidates": [self.candidate]}]
        return explore("테스트용 가상 프로필", [self.policy], self.index, self.model)

    def test_unknown_residence_is_not_claimed_as_eligible(self):
        result = self.run_explore()
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["needs_confirmation"]), 1)
        self.assertIn("대전 거주", result["needs_confirmation"][0]["unverified_conditions"])
        self.assertEqual(result["needs_confirmation"][0]["verified_conditions"][0]["condition"], "만 30세")

    def test_explicit_incompatibility_is_excluded_even_if_relevant(self):
        self.candidate["checks"][0]["status"] = "unmet"
        result = self.run_explore()
        self.assertFalse(result["matches"] or result["needs_confirmation"])

    def test_fabricated_eligibility_quote_is_downgraded_to_unknown(self):
        self.candidate["checks"][0]["evidence"] = "누구나 신청 가능"
        result = self.run_explore()
        row = result["needs_confirmation"][0]
        self.assertEqual(row["verified_conditions"], [])
        self.assertIn("만 30세", row["unverified_conditions"])

    def test_fabricated_relevance_quote_is_rejected(self):
        self.candidate["relevance_evidence"] = "정규직 취업 보장"
        result = self.run_explore()
        self.assertFalse(result["matches"] or result["needs_confirmation"])

    def test_missing_condition_check_does_not_become_confirmed(self):
        self.candidate["checks"] = self.candidate["checks"][:1]
        result = self.run_explore()
        self.assertFalse(result["matches"])
        self.assertIn("대전 거주", result["needs_confirmation"][0]["unverified_conditions"])

    def test_unsubstantiated_additional_check_does_not_block_relevance(self):
        self.profile["constraints"] = []
        self.candidate["checks"] = []
        self.candidate["additional_checks"] = ["취업 상태 기준", "최신 공고·접수상태"]
        result = self.run_explore()
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["needs_confirmation"], [])

    def test_evidenced_requirement_stays_pending(self):
        self.profile["constraints"] = []
        self.policy["raw"]["addAplyQlfcCndCn"] = "미취업 청년만 참여 가능"
        self.candidate["additional_checks"] = [
            {"condition": "취업 상태 기준", "evidence": "미취업 청년만 참여 가능"}]
        result = self.run_explore()
        self.assertEqual(result["matches"], [])
        row = result["needs_confirmation"][0]
        self.assertIn("취업 상태 기준", row["unverified_conditions"])
        self.assertEqual(row["additional_requirements"][0]["evidence"], "미취업 청년만 참여 가능")

    def test_fabricated_requirement_and_general_notice_do_not_block(self):
        self.profile["constraints"] = []
        self.candidate["additional_checks"] = [
            {"condition": "취업 상태 기준", "evidence": "미취업자만 참여 가능"},
            {"condition": "최신 공고·접수상태", "evidence": "백엔드 개발 취업 교육 지원"}]
        result = self.run_explore()
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["additional_requirements"], [])

    def test_unverified_user_condition_stays_pending_without_additional_checks(self):
        self.candidate["additional_checks"] = []
        result = self.run_explore()
        self.assertFalse(result["matches"])
        self.assertEqual(result["needs_confirmation"][0]["unverified_conditions"], ["대전 거주"])

    def test_all_explicit_conditions_verified_becomes_related(self):
        self.profile["constraints"] = self.profile["constraints"][:1]
        self.candidate["additional_checks"] = []
        result = self.run_explore()
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["status"], "confirmed")
        self.assertEqual(result["needs_confirmation"], [])

    def test_code_and_open_access_text_are_not_qualification_restrictions(self):
        self.profile["constraints"] = []
        self.policy["raw"].update({"jobCd": "0013010", "plcyExplnCn": "구직·재직 청년 등 전 국민 대상"})
        self.candidate["additional_checks"] = [
            {"condition": "취업 상태 기준", "evidence": "취업 상태 코드: 0013010"},
            {"condition": "취업 상태 기준", "evidence": "구직·재직 청년 등 전 국민 대상"},
            {"condition": "취업 상태 기준", "evidence": "구직·재직 청년"}]
        result = self.run_explore()
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["additional_requirements"], [])

    def test_anyone_with_explicit_income_limit_still_needs_income_check(self):
        self.profile["constraints"] = []
        self.policy["raw"]["earnEtcCn"] = "중위소득 60% 이하이면 누구나 신청 가능"
        self.candidate["additional_checks"] = [
            {"condition": "소득 기준", "evidence": "중위소득 60% 이하이면 누구나 신청 가능"}]
        result = self.run_explore()
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["needs_confirmation"][0]["unverified_conditions"], ["소득 기준"])


if __name__ == "__main__":
    unittest.main()
