"""Real Gateway lifecycle/run entry boundaries; no real model or transport."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from gateway.run import GatewayRunner
from tests.gateway.test_ivd_gateway_lifecycle_integration import _runner, _source


def test_trusted_business_body_is_shared_by_package_and_guard():
    runner = _runner();seen=[]
    def package(*args, **kwargs):
        seen.append(('package',kwargs['question'],kwargs.get('business_question')))
        return SimpleNamespace(answer_shape='clarification')
    def guard(*args, **kwargs):
        seen.append(('guard',kwargs['message'],None));return None
    raw='[Gateway sender=时间测试] CNVseq的产品经理是谁'
    with patch('gateway.ivd_runtime.ivd_engine_mode',return_value='package'), patch('gateway.ivd_runtime.execute_exclusive_ivd_turn',side_effect=package), patch('gateway.after_sales_guard.prepare_after_sales_turn',side_effect=guard):
        runner._prepare_ivd_lifecycle({},platform='qqbot',message=raw,history=[],business_message='CNVseq的产品经理是谁')
    assert seen==[('package',raw,'CNVseq的产品经理是谁'),('guard','CNVseq的产品经理是谁',None)]


