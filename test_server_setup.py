import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('server_setup', Path(__file__).parent/'tools/configure_server.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

class ServerSetupTest(unittest.TestCase):
    def test_configuration_preserves_literal_credentials_without_echoing_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = io.StringIO()
            with patch.object(setup,'ROOT',root), patch('sys.argv',['setup','--ip','80.47.238.200']), patch('builtins.input',side_effect=['fixture-client','https://soundcloud.com/owner?tracking=1']), patch.object(setup.getpass,'getpass',return_value="fixture'$secret#"), contextlib.redirect_stdout(output):
                setup.main()
            data = (root/'.env').read_text(encoding='utf-8')
            self.assertIn("SOUNDCLOUD_CLIENT_SECRET='fixture\\'$secret#'",data)
            self.assertIn('21474836480',data)
            self.assertIn('5368709120',data)
            self.assertNotIn('tracking',data)
            self.assertNotIn("fixture'$secret#",output.getvalue())
            with patch.object(setup,'ROOT',root), patch('sys.argv',['setup','--ip','80.47.238.200']):
                with self.assertRaises(ValueError):setup.main()
            self.assertEqual((root/'.env').read_text(encoding='utf-8'),data)

    def test_configuration_rejects_control_characters_and_private_ips(self):
        for value in ['', 'fixture\nINJECTED=1', 'fixture\0secret']:
            with self.assertRaises(ValueError):setup.dotenv_value(value)
        with patch('sys.argv',['setup','--ip','127.0.0.1']):
            with self.assertRaises(ValueError):setup.main()
