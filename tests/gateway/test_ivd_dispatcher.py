"""Tests for immutable package-only IVD dispatch decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gateway.ivd_dispatcher import DecisionEnvelope, IVDDispatcher


def _policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_version": "hermes-dispatch-v1",
        "resolution_order": [
            "operation_intent",
            "product_identity",
            "semantic_intent",
            "answer_shape",
        ],
        "operation_platform_scopes": ["HALOS"],
        "product_required_intents": [
            "parameter",
            "process",
            "product_fact",
            "report_interpretation",
            "troubleshooting",
        ],
        "clarifications": {"product_line": "请确认具体产品或项目名称。"},
        "operation_intents": [
            {
                "intent": "platform_operation",
                "priority": 10,
                "knowledge_type": "operation",
                "risk_class": "medium",
                "answer_shape": "direct_process",
                "aliases": [
                    {"alias": "导入数据", "match_mode": "contains"},
                    {"alias": "导入.{0,16}数据", "match_mode": "regex"},
                    {"alias": "创建.{0,12}任务", "match_mode": "regex"},
                ],
            }
        ],
        "semantic_intents": [
            {
                "intent": "troubleshooting",
                "priority": 20,
                "knowledge_type": "diagnostic_pattern",
                "risk_class": "high",
                "answer_shape": "diagnostic",
                "aliases": [{"alias": "异常", "match_mode": "contains"}],
            },
            {
                "intent": "parameter",
                "priority": 30,
                "knowledge_type": "parameter",
                "risk_class": "medium",
                "answer_shape": "scalar",
                "aliases": [{"alias": "多少", "match_mode": "contains"}],
            },
            {
                "intent": "product_fact",
                "priority": 100,
                "knowledge_type": "product",
                "risk_class": "low",
                "answer_shape": "direct_fact",
                "aliases": [],
            },
        ],
        "product_aliases": [
            {
                "product_line": "NIFTY",
                "product_variant": "标准版",
                "alias": "无创",
                "match_mode": "contains",
                "confidence": 1.0,
                "sort_key": [-1.0, -2, "nifty", "标准版", "无创"],
            },
            {
                "product_line": "NIFTY",
                "product_variant": "PRO",
                "alias": "NIFTY PRO",
                "match_mode": "token",
                "confidence": 1.0,
                "sort_key": [-1.0, -9, "nifty", "pro", "nifty pro"],
            },
            {
                "product_line": "CNV-seq",
                "product_variant": None,
                "alias": "CNV",
                "match_mode": "token",
                "confidence": 1.0,
                "sort_key": [-1.0, -3, "cnv-seq", "", "cnv"],
            },
            {
                "product_line": "HALOS",
                "product_variant": None,
                "alias": "HALOS",
                "match_mode": "token",
                "confidence": 1.0,
                "sort_key": [-1.0, -5, "halos", "", "halos"],
            },
        ],
        "answer_shapes": {},
        "workflow_stages": [
            {"stage": "extraction", "aliases": ["提取"]},
            {"stage": "report", "aliases": ["报告"]},
        ],
        "knowledge_types": [
            "diagnostic_pattern",
            "operation",
            "parameter",
            "product",
        ],
        "risk_classes": ["high", "low", "medium"],
        "sources": {},
    }


class IVDDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "serving-package"
        vocabulary = self.root / "indexes" / "dispatch-vocabulary-v1.json"
        vocabulary.parent.mkdir(parents=True)
        self._write_vocabulary(_policy())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_only_package_vocabulary_and_operation_intent_wins(self):
        dispatcher = IVDDispatcher(self.root)

        envelope = dispatcher.dispatch("HALOS 怎么导入无创数据？")

        self.assertEqual("platform_operation", envelope.intent)
        self.assertEqual("NIFTY", envelope.product_line)
        self.assertEqual("标准版", envelope.product_variant)
        self.assertEqual("operation", envelope.knowledge_type)
        self.assertEqual("direct_process", envelope.answer_shape)
        self.assertEqual(1, envelope.envelope_count)

    def test_parameter_decision_sets_all_axes_once(self):
        envelope = IVDDispatcher(self.root).dispatch("无创提取需要多少血浆？")

        self.assertEqual("parameter", envelope.intent)
        self.assertEqual("NIFTY", envelope.product_line)
        self.assertEqual("标准版", envelope.product_variant)
        self.assertEqual("extraction", envelope.workflow_stage)
        self.assertEqual("parameter", envelope.knowledge_type)
        self.assertEqual("medium", envelope.risk_class)
        self.assertEqual("scalar", envelope.answer_shape)
        self.assertEqual((), envelope.ambiguities)
        self.assertEqual((), envelope.clarifying_questions)

    def test_missing_product_asks_one_chinese_question_without_budget(self):
        envelope = IVDDispatcher(self.root).dispatch("提取需要多少血浆？")

        self.assertEqual(("product_line",), envelope.ambiguities)
        self.assertEqual(("请确认具体产品或项目名称。",), envelope.clarifying_questions)
        self.assertEqual("clarification", envelope.answer_shape)
        self.assertEqual(0, envelope.indexed_retrieval_budget)
        self.assertEqual(0, envelope.model_call_budget)
        self.assertEqual(1, envelope.envelope_count)

    def test_multiple_products_ask_one_question_without_selecting_one(self):
        envelope = IVDDispatcher(self.root).dispatch("无创和 CNV 提取分别需要多少？")

        self.assertIsNone(envelope.product_line)
        self.assertIsNone(envelope.product_variant)
        self.assertEqual(("product_line",), envelope.ambiguities)
        self.assertEqual(1, len(envelope.clarifying_questions))
        self.assertEqual(0, envelope.indexed_retrieval_budget)
        self.assertEqual(0, envelope.model_call_budget)

    def test_conflicting_variants_ask_once_without_selecting_a_variant(self):
        envelope = IVDDispatcher(self.root).dispatch("无创和 NIFTY PRO 各需要多少？")

        self.assertIsNone(envelope.product_line)
        self.assertIsNone(envelope.product_variant)
        self.assertEqual(("product_line",), envelope.ambiguities)
        self.assertEqual(("请确认具体产品或项目名称。",), envelope.clarifying_questions)
        self.assertEqual(0, envelope.indexed_retrieval_budget)
        self.assertEqual(0, envelope.model_call_budget)

    def test_operation_without_product_does_not_force_clarification(self):
        envelope = IVDDispatcher(self.root).dispatch("怎么创建分析任务？")

        self.assertEqual("platform_operation", envelope.intent)
        self.assertIsNone(envelope.product_line)
        self.assertEqual((), envelope.ambiguities)
        self.assertEqual((), envelope.clarifying_questions)

    def test_envelope_is_deeply_immutable(self):
        envelope = IVDDispatcher(self.root).dispatch("无创提取需要多少血浆？")

        with self.assertRaises(FrozenInstanceError):
            envelope.answer_shape = "report"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            envelope.metadata["rewrite"] = True  # type: ignore[index]

    def test_repeated_dispatch_is_deterministic_but_returns_one_envelope_each_time(self):
        dispatcher = IVDDispatcher(self.root)

        first = dispatcher.dispatch("无创提取需要多少血浆？")
        second = dispatcher.dispatch("无创提取需要多少血浆？")

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(1, first.envelope_count)
        self.assertEqual(1, second.envelope_count)

    def test_composition_skips_engine_for_clarification(self):
        engine = _RecordingEngine()

        outcome = IVDDispatcher(self.root).execute(
            engine, question="提取需要多少血浆？"
        )

        self.assertIsNone(outcome.result)
        self.assertEqual(0, engine.calls)
        self.assertEqual("clarification", outcome.envelope.answer_shape)

    def test_composition_passes_frozen_scope_to_knowledge_engine(self):
        engine = _RecordingEngine()

        outcome = IVDDispatcher(self.root).execute(
            engine,
            question="无创提取需要多少血浆？",
            evidence={"same_batch": True},
        )

        self.assertEqual("answer", outcome.result.text)
        self.assertEqual(1, engine.calls)
        self.assertEqual("NIFTY", engine.arguments["product_line"])
        self.assertEqual("标准版", engine.arguments["product_variant"])
        self.assertEqual("extraction", engine.arguments["workflow_stage"])
        self.assertEqual("parameter", engine.arguments["knowledge_type"])
        self.assertEqual("scalar", engine.arguments["answer_shape"])
        self.assertEqual({"same_batch": True}, engine.arguments["evidence"])
        self.assertFalse(engine.arguments["allow_index_transaction"])

    def test_invalid_or_non_package_vocabulary_fails_closed(self):
        vocabulary = self.root / "indexes" / "dispatch-vocabulary-v1.json"
        vocabulary.write_text('{"schema_version": 99}', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "dispatch vocabulary"):
            IVDDispatcher(self.root)

    def test_missing_package_manifest_fails_closed(self):
        (self.root / "package-manifest.json").unlink()

        with self.assertRaisesRegex(ValueError, "package manifest"):
            IVDDispatcher(self.root)

    def test_tampered_vocabulary_digest_fails_closed(self):
        vocabulary = self.root / "indexes" / "dispatch-vocabulary-v1.json"
        vocabulary.write_text(vocabulary.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "digest"):
            IVDDispatcher(self.root)

    def test_manifest_symlink_fails_closed(self):
        manifest = self.root / "package-manifest.json"
        target = self.root / "outside-manifest.json"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        os.symlink(target, manifest)

        with self.assertRaisesRegex(ValueError, "package manifest"):
            IVDDispatcher(self.root)

    def test_vocabulary_must_be_declared_in_manifest_members(self):
        manifest = self.root / "package-manifest.json"
        manifest.write_text(
            json.dumps({"schema_version": 1, "members": {}}), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "member"):
            IVDDispatcher(self.root)

    def test_manifest_package_digest_must_bind_canonical_members(self):
        manifest_path = self.root / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["package_digest"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "package digest"):
            IVDDispatcher(self.root)

    def test_indexes_replacement_after_path_check_fails_closed(self):
        indexes = self.root / "indexes"
        retained = self.root / "retained-indexes"
        outside = self.root.parent / "outside-indexes"
        outside.mkdir()
        vocabulary = indexes / "dispatch-vocabulary-v1.json"
        (outside / vocabulary.name).write_bytes(vocabulary.read_bytes())
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            value = os.fspath(path)
            if not swapped and (
                value == "indexes" or value.endswith("dispatch-vocabulary-v1.json")
            ):
                indexes.rename(retained)
                indexes.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("gateway.ivd_dispatcher.os.open", side_effect=racing_open):
            with self.assertRaisesRegex(ValueError, "dispatch vocabulary"):
                IVDDispatcher(self.root)
        self.assertTrue(swapped)

    def test_indexes_replacement_after_directory_open_cannot_redirect_member(self):
        indexes = self.root / "indexes"
        retained = self.root / "retained-indexes"
        outside = self.root.parent / "outside-indexes"
        outside.mkdir()
        hostile = _policy()
        hostile["clarifications"] = {"product_line": "Confirm product name."}
        (outside / "dispatch-vocabulary-v1.json").write_text(
            json.dumps(hostile), encoding="utf-8"
        )
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            value = os.fspath(path)
            if not swapped and value == "dispatch-vocabulary-v1.json":
                indexes.rename(retained)
                indexes.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("gateway.ivd_dispatcher.os.open", side_effect=racing_open):
            envelope = IVDDispatcher(self.root).dispatch("无创提取需要多少血浆？")

        self.assertTrue(swapped)
        self.assertEqual("NIFTY", envelope.product_line)
        self.assertEqual("scalar", envelope.answer_shape)

    def test_non_chinese_product_clarification_fails_closed(self):
        policy = _policy()
        policy["clarifications"] = {"product_line": "Confirm product name."}
        self._write_vocabulary(policy)

        with self.assertRaisesRegex(ValueError, "Chinese"):
            IVDDispatcher(self.root)

    def _write_vocabulary(self, policy: dict[str, object]) -> None:
        vocabulary = self.root / "indexes" / "dispatch-vocabulary-v1.json"
        vocabulary.write_text(
            json.dumps(policy, ensure_ascii=False), encoding="utf-8"
        )
        digest = hashlib.sha256(vocabulary.read_bytes()).hexdigest()
        members = {"indexes/dispatch-vocabulary-v1.json": digest}
        package_digest = hashlib.sha256(
            json.dumps(
                {
                    "algorithm": "sha256-canonical-members-v1",
                    "members": members,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (self.root / "package-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package_digest": package_digest,
                    "member_digest_algorithm": "sha256-canonical-members-v1",
                    "members": members,
                }
            ),
            encoding="utf-8",
        )


class _RecordingEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.arguments: dict[str, object] = {}

    def execute(self, **arguments: object) -> SimpleNamespace:
        self.calls += 1
        self.arguments = arguments
        return SimpleNamespace(text="answer")


if __name__ == "__main__":
    unittest.main()
