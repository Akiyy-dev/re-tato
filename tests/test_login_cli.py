import importlib.util
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec('telethon') and importlib.util.find_spec('Crypto'), 'optional login dependencies not installed')
class LoginCliTests(unittest.TestCase):
    def test_help_does_not_request_account_input_or_network(self):
        result = subprocess.run([sys.executable, '-m', 'tatocollector.login', '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--session', result.stdout)

    def test_noninteractive_run_exits_without_requesting_a_login_code(self):
        result = subprocess.run([sys.executable, '-m', 'tatocollector.login'], input='',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('interactive terminal', result.stderr)


if __name__ == '__main__':
    unittest.main()
