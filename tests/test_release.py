import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from apee.config import ApeeError, Config
from apee.cli import doctor
from apee.portable import initialize
from apee.release import extract_runtime, package


class PortableReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='apee-release-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries):
        path = self.root/'python.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, target in entries:
                info = tarfile.TarInfo(name)
                if target is not None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    archive.addfile(info)
                else:
                    payload = b'fixture'
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_checksum_mismatch_stops_before_extracting(self):
        archive, _ = self.archive([('python/bin/python3', None)])
        output = self.root/'runtime'
        with self.assertRaisesRegex(ApeeError, 'SHA-256'):
            extract_runtime(archive, '0'*64, output)
        self.assertFalse(output.exists())

    def test_archive_traversal_and_external_link_are_rejected(self):
        for entries in ([('../escape', None)], [('python/../../escape', None)],
                        [('python/bin/python3', '/usr/bin/python3')],
                        [('python/bin/link', '../../escape')],
                        [('python/bin', '../lib'), ('python/bin/escape', None)]):
            with self.subTest(entries=entries):
                archive, sha = self.archive(entries)
                with self.assertRaises(ApeeError):
                    extract_runtime(archive, sha, self.root/'output')
        self.assertFalse((self.root/'output').exists())

    def test_internal_runtime_link_is_preserved(self):
        archive, sha = self.archive([('python/bin/python3.12', None), ('python/bin/python3', 'python3.12')])
        runtime = extract_runtime(archive, sha, self.root/'output')
        self.assertTrue((runtime/'bin/python3').is_symlink())
        self.assertEqual((runtime/'bin/python3').read_bytes(), b'fixture')

    def test_first_launch_creates_private_config_and_preserves_existing_settings(self):
        defaults = self.root/'defaults.json'
        defaults.write_text(json.dumps(dict(version=1, state_dir='.apee', projects=[])))
        instance = self.root/'New user with spaces'
        config = initialize(instance, defaults)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        data = json.loads(config.read_text())
        data['budget'] = dict(daily_tokens=12345)
        data['unknown_future_setting'] = ['preserve']
        config.write_text(json.dumps(data))
        saved = config.read_bytes()
        self.assertEqual(initialize(instance, defaults), config)
        self.assertEqual(config.read_bytes(), saved)
        self.assertFalse((instance/'.apee').exists())

    def test_invalid_existing_config_is_reported_without_replacement(self):
        defaults = self.root/'defaults.json'
        defaults.write_text('{"version":1,"projects":[]}')
        instance = self.root/'instance'
        instance.mkdir()
        path = instance/'apee.local.json'
        path.write_text('damaged settings')
        with self.assertRaises(ApeeError):
            initialize(instance, defaults)
        self.assertEqual(path.read_text(), 'damaged settings')
        with self.assertRaises(ApeeError):
            initialize('relative-instance', defaults)

    def test_concurrent_first_launch_never_exposes_partial_settings(self):
        defaults = self.root/'defaults.json'
        defaults.write_text(json.dumps(dict(version=1, state_dir='.apee', projects=[],
                                            future_data='preserve'*10000)))
        instance = self.root/'instance'
        def launch(_):
            path = initialize(instance, defaults)
            return json.loads(path.read_text())
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(launch, range(24)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(list(instance.iterdir()), [instance/'apee.local.json'])

    def test_source_defaults_have_no_projects_or_automatic_external_actions(self):
        root = Path(__file__).resolve().parents[1]
        config = Config(root/'apee.json')
        self.assertEqual(config.projects, {})
        self.assertFalse(config.planning['enabled'])
        self.assertFalse(config.notifications['enabled'])
        self.assertFalse(config.auto_recovery['enabled'])
        example = Config(root/'apee.example.json')
        self.assertTrue(all(not p['enabled'] and not p['publish_draft'] for p in example.projects.values()))

    def test_notarization_cannot_silently_fall_back_to_adhoc(self):
        with patch('apee.release.sys.platform', 'darwin'):
            with self.assertRaisesRegex(ApeeError, '憑證'):
                package('missing.tar.gz', '0'*64, self.root/'output', notary_profile='profile')
            with self.assertRaisesRegex(ApeeError, 'Developer ID'):
                package('missing.tar.gz', '0'*64, self.root/'output', signing_identity='Apple Development: fixture')

    def test_doctor_rejects_incomplete_exec_and_managed_permissions_interfaces(self):
        defaults = self.root/'defaults.json'
        defaults.write_text('{"version":1,"projects":[]}')
        config = Config(defaults)
        for missing in ('--ignore-user-config', '--include-managed-config', None):
            class Tools:
                def run(self, args, **kwargs):
                    flags = '--permission-profile --include-managed-config --ignore-user-config --output-schema --output-last-message'
                    return flags.replace(missing, '') if missing else flags
            with self.subTest(missing=missing), patch('apee.cli.Runner', Tools), redirect_stdout(io.StringIO()):
                self.assertEqual(doctor(config), int(missing is not None))


@unittest.skipUnless(os.environ.get('APEE_NATIVE_TESTS') == '1' and
                     os.environ.get('APEE_RELEASE_RUNTIME') and sys.platform == 'darwin',
                     '原生可攜式 app 驗證需要 APEE_RELEASE_RUNTIME')
class NativePortableReleaseTests(unittest.TestCase):
    def test_real_archive_relocates_without_build_settings_and_retains_new_user_data(self):
        cache = Path.home()/'.cache'
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='apee-portable-native-', dir=cache) as temporary:
            root = Path(temporary)
            pin = json.loads((Path(__file__).resolve().parents[1]/'release/python-runtimes.json').read_text())['arm64']
            built = package(os.environ['APEE_RELEASE_RUNTIME'], pin['sha256'], root/'Build with spaces', architecture='arm64')
            self.assertFalse(built['notarized'])
            self.assertFalse(built['public_app_ready'])
            self.assertTrue(built['source_payload_matches_checkout'])
            installed = root/'Installed elsewhere/APEE.app'
            installed.parent.mkdir()
            subprocess.run(['/usr/bin/ditto', '-x', '-k', str(Path(built['output'])/built['archive']), str(installed.parent)], check=True)
            shutil.rmtree(built['output'])
            instance = root/'New user data'
            env = dict(os.environ, APEE_INSTANCE_ROOT=str(instance), GH_TOKEN='fixture-secret',
                       OPENAI_API_KEY='fixture-secret', PYTHONPATH='/nonexistent/fixture')
            binary = installed/'Contents/MacOS/APEE'
            def run(*args):
                result = subprocess.run([str(binary), *args], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                return json.loads(result.stdout)
            backend = run('--backend-info')
            self.assertEqual(backend['config'], str(instance/'apee.local.json'))
            self.assertTrue(backend['python'].startswith(str(installed)))
            self.assertNotIn('fixture-secret', json.dumps(backend))
            self.assertNotIn('PYTHONPATH', backend['environment'])
            invalid = subprocess.run([str(binary), '--backend-info'],
                                     env=dict(env, APEE_INSTANCE_ROOT=''), capture_output=True, text=True, timeout=30)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn('絕對路徑', invalid.stderr)
            state = run('--diagnose')
            self.assertEqual(state['tasks'], [])
            self.assertFalse(state['service']['installed'])
            self.assertEqual(state['attempts']['used'], 0)
            config = Path(backend['config'])
            settings = json.loads(config.read_text())
            settings['budget'] = dict(daily_tokens=12345)
            config.write_text(json.dumps(settings))
            saved = config.read_bytes()
            moved = root/'Moved again/APEE.app'
            moved.parent.mkdir()
            installed.rename(moved)
            binary = moved/'Contents/MacOS/APEE'
            backend = run('--backend-info')
            self.assertTrue(backend['python'].startswith(str(moved)))
            self.assertEqual(config.read_bytes(), saved)
            self.assertEqual(run('--diagnose')['budget']['tokens'], 0)
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(moved)], check=True, capture_output=True)
