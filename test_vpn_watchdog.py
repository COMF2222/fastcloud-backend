import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
import vpn_watchdog as watchdog


class WatchdogTests(unittest.TestCase):
    def test_systemd_directory_is_absolute_without_literal_quotes(self):
        self.assertEqual(watchdog.systemd_dropin('/root/fastcloud-backend'),
                         '[Service]\nWorkingDirectory=/root/fastcloud-backend\n')
        self.assertEqual(watchdog.systemd_dropin('/srv/fastcloud space%name'),
                         '[Service]\nWorkingDirectory=/srv/fastcloud space%%name\n')
        for bad in ('relative/path', '/root/project\nExecStart=other', '/root/project\0'):
            with self.assertRaises(ValueError):
                watchdog.systemd_dropin(bad)

    def test_docker_compose_failure_does_not_report_a_successful_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'private config error'))
            self.assertEqual(watchdog.run_once(Path(directory), path, runner), 1)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(watchdog.load_state(path)['restarts'], [])

    def test_short_failure_recovers_without_restart(self):
        state = watchdog.initial_state()
        state, action = watchdog.evaluate(state, False, 1000)
        self.assertEqual(action, "failed")
        state, action = watchdog.evaluate(state, True, 1060)
        self.assertEqual(action, "recovered")
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["restarts"], [])

    def test_three_consecutive_failures_restart(self):
        state = watchdog.initial_state()
        actions = []
        for now in (1000, 1060, 1120):
            state, action = watchdog.evaluate(state, False, now)
            actions.append(action)
        self.assertEqual(actions, ["failed", "failed", "restart"])
        self.assertEqual(state["last_restart"], 1120)
        self.assertEqual(state["restarts"], [1120])

    def test_cooldown_and_hourly_budget_survive_successful_probes(self):
        state = {"failures": 2, "last_restart": 1000, "restarts": [1000]}
        state, action = watchdog.evaluate(state, False, 1180)
        self.assertEqual(action, "cooldown")
        state, action = watchdog.evaluate(state, False, 1300)
        self.assertEqual(action, "restart")
        state, _ = watchdog.evaluate(state, True, 1360)
        self.assertEqual(state["restarts"], [1000, 1300])
        state.update(failures=2, last_restart=1600, restarts=[1000, 1300, 1600])
        state, action = watchdog.evaluate(state, False, 1900)
        self.assertEqual(action, "limit")
        state, action = watchdog.evaluate(state, False, 4600)
        self.assertEqual(action, "restart")
        self.assertEqual(state["restarts"], [1300, 1600, 4600])

    def test_one_available_target_keeps_vpn_running(self):
        for results in ([True, False], [False, True], [True, True]):
            self.assertTrue(watchdog.probe_result(subprocess.CompletedProcess([], 0, json.dumps(results))))
        self.assertFalse(watchdog.probe_result(subprocess.CompletedProcess([], 0, '[false,false]')))

    def test_unavailable_or_malformed_probe_is_not_a_vpn_failure(self):
        for code, output in ((1, '[false,false]'), (0, ''), (0, '{}'), (0, '[0,0]'), (0, '[false]')):
            self.assertIsNone(watchdog.probe_result(subprocess.CompletedProcess([], code, output)))

    def test_state_roundtrip_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            self.assertEqual(watchdog.load_state(path), watchdog.initial_state())
            watchdog.save_state(path, {"failures": 2, "last_restart": 1000, "restarts": [1000]})
            self.assertEqual(watchdog.load_state(path)["failures"], 2)
            for bad in ('{', '{"failures":-1,"last_restart":0,"restarts":[]}',
                        '{"failures":0,"last_restart":NaN,"restarts":[]}'):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    watchdog.load_state(path)

    def test_restart_targets_only_mihomo_and_persists_attempt_before_command(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / 'state.json'
            watchdog.save_state(path, {"failures": 2, "last_restart": 0, "restarts": []})
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                if 'ps' in command:
                    return subprocess.CompletedProcess(command, 0, 'container-id\n')
                if 'exec' in command:
                    self.assertEqual(kwargs['input'], watchdog.PROBE)
                    return subprocess.CompletedProcess(command, 0, '[false,false]')
                self.assertEqual(command[-4:], ['restart', '--timeout', '10', 'mihomo'])
                self.assertEqual(watchdog.load_state(path)['last_restart'], 1000)
                return subprocess.CompletedProcess(command, 1, '', 'private docker error')
            self.assertEqual(watchdog.run_once(project, path, runner, now=1000), 1)
            self.assertEqual(len(calls), 4)
            self.assertEqual(watchdog.load_state(path)['restarts'], [1000])

    def test_stopped_container_is_not_started_by_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, ''))
            self.assertEqual(watchdog.run_once(Path(directory), path, runner), 0)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(watchdog.load_state(path)['failures'], 0)

    def test_exec_failure_never_restarts_vpn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, 'id'),
                                       subprocess.CompletedProcess([], 0, 'id'),
                                       subprocess.CompletedProcess([], 1, '')])
            self.assertEqual(watchdog.run_once(Path(directory), path, runner), 1)
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(watchdog.load_state(path)['restarts'], [])


if __name__ == '__main__':
    unittest.main()
