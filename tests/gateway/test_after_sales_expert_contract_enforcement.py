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
    def test_expert_contract_without_evidence_stops_numeric_rendering(self):
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

            self.assertTrue(result["direct_response"])
            self.assertIn("暂不输出未经核实的正式数值结论", result["direct_response"])
            self.assertEqual("clarification", result["answer_shape"])
            self.assertEqual("block", result["preflight_decision"])
            self.assertEqual("stop_before_answer_generation", result["preflight_action"])

    def test_pending_evidence_also_stops_unverified_rendering(self):
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

        self.assertTrue(result["direct_response"])
        self.assertIn("暂不输出未经核实的正式数值结论", result["direct_response"])
        self.assertEqual("clarification", result["answer_shape"])
        self.assertEqual("block", result["preflight_decision"])
        self.assertEqual("stop_before_answer_generation", result["preflight_action"])

    def test_file_lookup_guard_does_not_ask_numeric_stage_or_known_version(self):
        from tempfile import TemporaryDirectory
        plan={"runtime_preflight":{"eligible":True},
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


if __name__ == "__main__":
    unittest.main()
