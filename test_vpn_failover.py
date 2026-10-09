import copy
import json
from pathlib import Path
import tempfile
import unittest
import configure_vpn_failover as failover


def fixture():
    return {"mode": "rule", "mixed-port": 7890, "allow-lan": True,
            "dns": {"enable": True, "nameserver": ["1.1.1.1"]},
            "proxies": [{"name": "node-one", "type": "vless", "server": "node1.example", "uuid": "private-one"},
                        {"name": "node-two", "type": "trojan", "server": "node2.example", "password": "private-two"}],
            "proxy-groups": [{"name": "VPN", "type": "fallback", "proxies": ["node-one", "node-two"]}],
            "rules": ["DOMAIN,local.example,DIRECT", "MATCH,VPN"]}


class FailoverTests(unittest.TestCase):
    def test_all_nodes_checked_and_fastest_working_soundcloud_route_selected(self):
        original = fixture()
        untouched = copy.deepcopy(original)
        configured, summary = failover.fastest_config(original)
        self.assertEqual(original, untouched)
        group = configured['proxy-groups'][0]
        self.assertEqual(group['type'], 'url-test')
        self.assertEqual(group['proxies'], ['node-one', 'node-two'])
        self.assertEqual(group['tolerance'], 0)
        self.assertEqual(group['interval'], 60)
        self.assertFalse(group['lazy'])
        self.assertEqual(group['url'], failover.CHECK_URL)
        self.assertEqual(group['expected-status'], 200)
        self.assertEqual(group['empty-fallback'], 'REJECT')
        self.assertEqual(configured['proxies'], original['proxies'])
        for key in ('rules', 'dns', 'mixed-port', 'allow-lan'):
            self.assertEqual(configured[key], original[key])
        self.assertEqual(summary['inline_nodes'], 2)
        self.assertNotIn('private', json.dumps(summary))

    def test_provider_nodes_get_their_own_checks_without_exposing_subscription(self):
        config = fixture()
        config['proxies'] = []
        config['proxy-providers'] = {'subscription': {'type': 'http', 'url': 'https://provider.example/private',
                                                     'header': {'Authorization': ['private-token']}}}
        configured, summary = failover.fastest_config(config)
        self.assertEqual(configured['proxy-groups'][0]['use'], ['subscription'])
        provider = configured['proxy-providers']['subscription']
        self.assertEqual(provider['url'], config['proxy-providers']['subscription']['url'])
        self.assertEqual(provider['header'], config['proxy-providers']['subscription']['header'])
        self.assertEqual(provider['health-check']['url'], failover.CHECK_URL)
        self.assertTrue(provider['health-check']['enable'])
        self.assertFalse(provider['health-check']['lazy'])
        self.assertNotIn('private', json.dumps(summary))

    def test_direct_nodes_and_old_filters_do_not_enter_automatic_selection(self):
        config = fixture()
        config['proxies'].append({'name': 'local-direct', 'type': 'direct'})
        config['proxy-groups'][0].update({'include-all': True, 'filter': 'node-one', 'default-selected': 'node-one'})
        configured, _ = failover.fastest_config(config)
        group = configured['proxy-groups'][0]
        self.assertEqual(group['proxies'], ['node-one', 'node-two'])
        self.assertNotIn('include-all', group)
        self.assertNotIn('filter', group)
        self.assertNotIn('default-selected', group)

    def test_configuration_is_idempotent(self):
        config, _ = failover.fastest_config(fixture())
        again, _ = failover.fastest_config(config)
        self.assertEqual(config, again)

    def test_ambiguous_routes_and_single_node_are_rejected(self):
        cases = []
        config = fixture(); config['rules'].append('MATCH,VPN'); cases.append(config)
        config = fixture(); config['rules'] = ['MATCH,DIRECT']; cases.append(config)
        config = fixture(); config['mode'] = 'global'; cases.append(config)
        config = fixture(); config['proxies'] = config['proxies'][:1]; cases.append(config)
        config = fixture(); config['proxies'][1]['name'] = 'node-one'; cases.append(config)
        for config in cases:
            with self.assertRaises(ValueError):
                failover.fastest_config(config)

    def test_failed_mihomo_validation_keeps_original_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes(b'original-private-config')
            with self.assertRaises(ValueError):
                failover.replace_config(path, path.read_bytes(), b'candidate', lambda _: False)
            self.assertEqual(path.read_bytes(), b'original-private-config')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_successful_validation_keeps_exact_backup_and_handles_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes(b'original-private-config')
            backup = failover.replace_config(path, path.read_bytes(), b'candidate', lambda _: True)
            self.assertEqual(backup.read_bytes(), b'original-private-config')
            self.assertEqual(path.read_bytes(), b'candidate')
            self.assertIsNone(failover.replace_config(path, b'candidate', b'candidate', lambda _: True))
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes(b'original')
            def validate(_):
                path.write_bytes(b'owner-edit')
                return True
            with self.assertRaises(ValueError):
                failover.replace_config(path, b'original', b'candidate', validate)
            self.assertEqual(path.read_bytes(), b'owner-edit')


if __name__ == '__main__':
    unittest.main()
