"""Regression tests for serving-safe expert fallback boundaries."""

from __future__ import annotations

import unittest
from pathlib import Path

from gateway.after_sales_guard import _render_fast_response_context


def _load_fast_module(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def build_fast_response_plan(message, question_type='wet_lab'):\n"
        f"    return {payload!r}\n",
        encoding="utf-8",
    )
    return path


class AfterSalesExpertContractEnforcementTests(unittest.TestCase):
    def setUp(self):
        # These are renderer contract unit tests with synthetic planner results,
        # not trusted-root/production loading tests. Keep the loader seam explicit;
        # real cold-entry tests exercise binding in separate fresh processes.
        import importlib.util
        from unittest.mock import patch

        def synthetic_loader(path, mtime):
            spec = importlib.util.spec_from_file_location("synthetic_contract_plan", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        loader = patch("gateway.after_sales_guard._load_module", side_effect=synthetic_loader)
        loader.start()
        self.addCleanup(loader.stop)

    def test_navigation_rejects_wrong_explicit_identity_and_does_not_repeat_known_fields(self):
        from gateway.after_sales_guard import _render_source_navigation
        source = {"identity": {"sop_id": "SOP-JL-388", "revision": "A0"},
                  "input_sufficient": True, "candidates": [{"file_name": "SOP-JL-3880 A0 unrelated.pdf"}]}
        answer = _render_source_navigation(source)
        self.assertNotIn("3880", answer)
        self.assertNotIn("告诉我", answer)
        self.assertNotIn("受控文件", answer)

    def test_navigation_labels_unverified_candidates_and_does_not_claim_delivery(self):
        from gateway.after_sales_guard import _render_source_navigation
        source = {"identity": {"sop_id": "SOP-JL-388", "revision": "A0"},
                  "input_sufficient": True, "candidates": [{"file_name": "SOP-JL-388 A0 质量控制.pdf", "authority": "locator_only"}]}
        answer = _render_source_navigation(source)
        self.assertIn("SOP-JL-388 A0", answer)
        self.assertNotIn("受控文件", answer)
        self.assertNotIn("已发送", answer)
        self.assertNotIn("告诉我", answer)

    def test_expert_contract_without_evidence_escalates_instead_of_stopping(self):
        plan = {
        "runtime_preflight": {"eligible": True, "route_version": "test"},
        "answer_template": {"style": "short_first"},
        "preflight_gate": {"decision": "allow", "pipeline_action": "continue_final_answer"},
        "product_identity": {"product_scope": "NIFTY", "product_scope_confirmed": True},
        "initial_files": ["knowledge-base/reference/nipt-zscore-thresholds.md"],
        "fast_path": {"hit": False, "answer_shape": "standard"},
        "answer_shape": {"answer_shape": "scalar_lookup"},
        "answer_contract": {"task_kind": "lookup", "deliverable": "direct_answer"},
        "expert_route_contract": {
            "status": "clarification",
            "formal_answer_allowed": False,
            "bounded_expert_answer_allowed": False,
            "issues": ["evidence_required", "product_scope_required"],
        },
    }
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            tmp_path = Path(root)
            module_path = _load_fast_module(
                tmp_path / "knowledge-base" / "scripts" / "hermes_fast_response_pipeline.py",
                plan,
            )

            result = _render_fast_response_context(
                {"fast_response_module": str(module_path)},
                message="NIPT目标染色体单体Z值应该是多少以下",
                match=None,
            )

            # 2026-11-02：契约未达 serving 层级不再停发，改按 bounded 专家
            # 分析作答；数值仍不得作为正式结论输出。
            self.assertEqual("", result["direct_response"])
            self.assertNotEqual("block", result["preflight_decision"])
            self.assertIn("[专家模式]", result["context"])
            self.assertEqual("expert_mode_auto_fallback", result["route_id"])

    def test_pending_evidence_also_escalates_unverified_rendering(self):
        plan = {
            "runtime_preflight": {"eligible": True, "route_version": "test"},
            "answer_template": {"style": "short_first"},
            "preflight_gate": {"decision": "allow", "pipeline_action": "continue_final_answer"},
            "product_identity": {"product_scope": "NIFTY", "product_scope_confirmed": True},
            "initial_files": [],
            "fast_path": {"hit": False, "answer_shape": "standard"},
            "answer_shape": {"answer_shape": "scalar_lookup"},
            "expert_route_contract": {
                "status": "pending_evidence",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
            },
        }
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            module_path = _load_fast_module(
                Path(root) / "knowledge-base" / "scripts" / "hermes_fast_response_pipeline.py",
                plan,
            )
            result = _render_fast_response_context(
                {"fast_response_module": str(module_path)},
                message="NIFTY结果阈值是多少",
                match=None,
            )

        self.assertEqual("", result["direct_response"])
        self.assertNotEqual("block", result["preflight_decision"])
        self.assertIn("[专家模式]", result["context"])

    def _render_with_contract(self, contract: dict, message: str = "NIFTY结果阈值是多少"):
        plan = {
            "runtime_preflight": {"eligible": True, "route_version": "test"},
            "answer_template": {"style": "short_first"},
            "preflight_gate": {"decision": "allow", "pipeline_action": "continue_final_answer"},
            "product_identity": {"product_scope": "NIFTY", "product_scope_confirmed": True},
            "initial_files": [],
            "fast_path": {"hit": False, "answer_shape": "standard"},
            "answer_shape": {"answer_shape": "scalar_lookup"},
            "expert_route_contract": contract,
        }
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            module_path = _load_fast_module(
                Path(root) / "knowledge-base" / "scripts" / "hermes_fast_response_pipeline.py",
                plan,
            )
            return _render_fast_response_context(
                {"fast_response_module": str(module_path)},
                message=message,
                match=None,
            )

    def test_stop_wording_asks_for_product_when_scope_is_unknown(self):
        result = self._render_with_contract(
            {
                "status": "clarification",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
                "capability": "after_sales_diagnosis",
                "issues": ["product_scope_required", "evidence_required"],
            }
        )

        # 不向使用者索取产品/版本：没有答案就转 bounded 分析，停发通知一律不发。
        self.assertEqual("", result["direct_response"])
        self.assertNotEqual("block", result["preflight_decision"])
        self.assertIn("[专家模式]", result["context"])

    def test_stop_wording_asks_for_version_when_product_is_known(self):
        result = self._render_with_contract(
            {
                "status": "clarification",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
                "capability": "after_sales_diagnosis",
                "product_line": "NIFTY",
                "version": "",
                "issues": ["product_scope_required", "evidence_required"],
            }
        )

        # 版本与文件编号由系统自查：既不索取，也不再用停发通知代替答案。
        self.assertEqual("", result["direct_response"])
        self.assertNotIn("请补充产品版本", str(result))
        self.assertNotIn("SOP 编号", str(result.get("direct_response") or ""))
        self.assertNotEqual("block", result["preflight_decision"])

    def test_mechanism_gap_states_boundary_instead_of_asking_version(self):
        result = self._render_with_contract(
            {
                "status": "pending_evidence",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
                "capability": "mechanism_explanation",
                "product_line": "NIFTY",
                "version": "",
                "version_scope": "general_mechanism",
                "issues": ["evidence_not_serving", "evidence_required"],
            }
        )

        self.assertEqual("", result["direct_response"])
        self.assertNotEqual("block", result["preflight_decision"])
        self.assertEqual("expert_analysis", result["answer_shape"])
        self.assertIn("[专家模式]", result["context"])

    def test_evidence_gap_asks_for_the_input_that_changes_the_judgement(self):
        result = self._render_with_contract(
            {
                "status": "pending_evidence",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
                "capability": "parameter_resolution",
                "product_line": "NIFTY",
                "version": "V1",
                "issues": ["evidence_required"],
            }
        )

        self.assertEqual("", result["direct_response"])
        self.assertNotIn("请补充产品名称", str(result))
        self.assertNotEqual("block", result["preflight_decision"])
        self.assertIn("[专家模式]", result["context"])
    def test_file_lookup_guard_does_not_ask_numeric_stage_or_known_version(self):
        from tempfile import TemporaryDirectory
        plan={"task_contract":{"intent":"sop_lookup"}, "runtime_preflight":{"eligible":True},
              "answer_shape":{"answer_shape":"direct_fact"},
              "answer_contract":{"deliverable":"direct_answer"},
              "context_envelope":{"product_line":"PMseq","product_version":"V5","workflow_step":"建库"},
              "expert_route_contract":{"status":"clarification","formal_answer_allowed":False,
                                        "bounded_expert_answer_allowed":False}}
        with TemporaryDirectory() as root:
            path=_load_fast_module(Path(root)/"scripts/hermes_fast_response_pipeline.py",plan)
            result=_render_fast_response_context({"fast_response_module":str(path)},
                       message="PMseq RNA V5 建库SOP在哪里",match=None)
        self.assertEqual("block",result["preflight_decision"])
        self.assertEqual("stop_before_answer_generation",result["preflight_action"])
        self.assertNotIn("数值",result["direct_response"])
        self.assertNotIn("请补充",result["direct_response"])
        self.assertNotIn("首次建库",result["direct_response"])

    def test_stop_wording_never_asks_for_a_version_or_document_number(self):
        # 老板手上只有“哪个项目 + 哪一步”，版本号/文件编号由系统自查。
        # 任何兜底话术都不许把这些当成用户必须补的信息。
        import gateway.after_sales_guard as guard_module

        for wording in (
            guard_module.EXPERT_CONTRACT_MISSING_PRODUCT,
            guard_module.EXPERT_CONTRACT_MISSING_VERSION,
            guard_module.EXPERT_CONTRACT_MECHANISM_BOUNDARY,
            guard_module.EXPERT_CONTRACT_EVIDENCE_DEFAULT,
        ):
            self.assertNotIn("请补充产品版本", wording)
            self.assertNotIn("SOP 编号或版本", wording)
            self.assertNotIn("尚未核实到", wording)
        self.assertIn("项目名称", guard_module.EXPERT_CONTRACT_MISSING_PRODUCT)
        self.assertIn("这一步是提取", guard_module.EXPERT_CONTRACT_MISSING_VERSION)
        self.assertIn("我自己核对", guard_module.EXPERT_CONTRACT_EVIDENCE_DEFAULT)

    def test_stop_response_for_a_known_product_asks_only_for_the_step(self):
        from gateway.after_sales_guard import build_expert_contract_stop_response

        text = build_expert_contract_stop_response(
            {
                "status": "clarification",
                "product_line": "NIFTY",
                "version": "",
                "capability": "product_scope",
                "issues": ["product_scope_required", "evidence_required"],
            }
        )
        self.assertIn("产品已确认", text)
        self.assertIn("这一步是提取", text)
        self.assertNotIn("请补充产品版本", text)
        self.assertNotIn("SOP 编号", text)
        self.assertNotIn("尚未核实到", text)


if __name__ == "__main__":
    unittest.main()


class ExpertModeMissCoverageTests(unittest.TestCase):
    """A structured miss must fall back to expert mode, not to a version ask."""

    def setUp(self):
        import importlib.util
        from unittest.mock import patch

        def synthetic_loader(path, mtime):
            spec = importlib.util.spec_from_file_location("synthetic_miss_plan", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        loader = patch("gateway.after_sales_guard._load_module", side_effect=synthetic_loader)
        loader.start()
        self.addCleanup(loader.stop)

    def _render(self, plan, message):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            module_path = _load_fast_module(
                Path(root) / "knowledge-base" / "scripts" / "hermes_fast_response_pipeline.py",
                plan,
            )
            return _render_fast_response_context(
                {"fast_response_module": str(module_path)}, message=message, match=None
            )

    def _plan(self, *, capability, shape="standard", intent="product_fact"):
        return {
            "runtime_preflight": {"eligible": False, "route_version": "test"},
            "answer_template": {"style": "short_first"},
            "preflight_gate": {"decision": "allow", "pipeline_action": "continue_final_answer"},
            "initial_files": [],
            "task_contract": {
                "intent": intent,
                "context": {"provenance_detail": {"matched_terms": {"sample_type": ["组织"]}}},
            },
            "fast_path": {"hit": False, "disposition": "miss", "answer_shape": shape},
            "answer_shape": {"answer_shape": "direct_fact"},
            "expert_route_contract": {
                "status": "clarification",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": False,
                "capability": capability,
                "issues": ["evidence_not_serving", "evidence_required", "product_scope_required"],
            },
        }

    def test_clinical_analysis_question_misses_into_expert_mode(self):
        # Regression: this question was answered with "请补充产品名称、版本或 SOP
        # 编号" although the planner itself had no product requirement to meet.
        result = self._render(
            self._plan(capability="parameter_resolution"),
            "稽留流产会导致绒毛都没了吗，取出来只剩母体组织的碎膜了",
        )

        self.assertEqual("", result["direct_response"])
        self.assertNotEqual("block", result["preflight_decision"])
        self.assertIn("[专家模式]", result["context"])
        self.assertEqual("expert_mode_auto_fallback", result["route_id"])
        self.assertEqual("expert_analysis", result["answer_shape"])
        self.assertFalse(result["expert_route_contract"])

    def test_case_conclusion_question_escalates_to_bounded_analysis(self):
        # 2026-11-02 口径：个案判读缺口也要给出可核实部分与边界，不再以
        # 「本轮已核对…」停发通知收场。
        result = self._render(
            self._plan(capability="data_interpretation", intent="report_interpretation"),
            "这个样本的报告结论应该怎么判读",
        )

        self.assertNotEqual("block", result["preflight_decision"])
        self.assertEqual("", result["direct_response"])
        self.assertIn("[专家模式]", result["context"])

    def test_expert_output_contract_states_the_claim_scope_domain(self):
        # Regression (2026-11-01): the prompt named the scope field but never
        # its value domain, so drafts copied the neighbouring route field
        # (version_scope="general_mechanism") into scope and every claim was
        # rejected as claim_scope_invalid on all three platforms.
        plan = {
            "runtime_preflight": {"eligible": True, "route_version": "test"},
            "answer_template": {"style": "short_first"},
            "preflight_gate": {"decision": "allow", "pipeline_action": "continue_final_answer"},
            "product_identity": {"product_scope": "NIFTY", "product_scope_confirmed": True},
            "initial_files": [],
            "fast_path": {"hit": True, "answer_shape": "standard"},
            "answer_shape": {"answer_shape": "diagnostic"},
            "task_contract": {"intent": "diagnostic"},
            "expert_route_contract": {
                "status": "ready",
                "formal_answer_allowed": False,
                "bounded_expert_answer_allowed": True,
                "allowed_claim_kinds": [
                    "hypothesis",
                    "verification_action",
                    "boundary",
                    "unknown",
                ],
                "issues": [],
            },
        }

        result = self._render(plan, "无创样本两次XO分级不同是什么原因")
        context = result["context"]

        self.assertIn("scope只能取general或product", context)
        self.assertIn("version_scope", context)
        self.assertIn("start_line必须等于该摘录自带的start_line", context)
