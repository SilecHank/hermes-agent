from unittest.mock import Mock, patch
from hermes_cli.ivd_cron_service_contract import _terminate_process_group

def test_termination_without_posix_group_api_falls_back_to_child_kill():
    process=Mock();process.poll.return_value=0
    with patch('hermes_cli.ivd_cron_service_contract.os.killpg', None, create=True):
        _terminate_process_group(process)
    process.kill.assert_called_once_with()

def test_termination_without_sigkill_falls_back_to_child_kill():
    process=Mock();process.poll.return_value=0
    with patch('hermes_cli.ivd_cron_service_contract.signal.SIGKILL', None, create=True):
        _terminate_process_group(process)
    process.kill.assert_called_once_with()
