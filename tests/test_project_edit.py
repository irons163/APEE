import json
import unittest

from apee.config import ApeeError, Config
from apee.controls import edit_project, project_revision, snapshot
from test_controls import ControlsFixture


class ProjectEditTests(ControlsFixture):
    def test_edit_preserves_other_projects_unknown_options_and_literal_templates(self):
        raw = self.data['projects'][0]
        raw.update(tests=[['python3', '{config_dir}/verification.py']], extension=dict(future=True))
        second = dict(raw, repo='example/other')
        self.data['projects'].append(second)
        self.settings()
        previous = snapshot(self.engine)['projects'][0]
        edit_project(self.config, 'example/demo', dict(priority=1, enabled=False, base_branch='codex/target'), previous['revision'])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['projects'][0]['tests'], [['python3', '{config_dir}/verification.py']])
        self.assertEqual(saved['projects'][0]['extension'], dict(future=True))
        self.assertEqual(saved['projects'][1], second)
        self.assertEqual(saved['projects'][0]['base_branch'], 'codex/target')

    def test_stale_or_invalid_edit_is_atomic_and_does_not_erase_configuration(self):
        raw = self.data['projects'][0]
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ApeeError, '已改變'):
            edit_project(self.config, 'example/demo', dict(priority=1), 'outdated-revision')
        with self.assertRaises(ApeeError):
            edit_project(self.config, 'example/demo', dict(tests=[]), project_revision(raw))
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_target_branches_are_rejected_before_dispatch(self):
        for branch in ('--bad', '../main', 'head..main', '/main', 'main/', 'main.lock', 'refs//heads/main'):
            with self.subTest(branch=branch):
                before = self.path.read_bytes()
                with self.assertRaises(ApeeError):
                    edit_project(self.config, 'example/demo', dict(base_branch=branch), project_revision(self.data['projects'][0]))
                self.assertEqual(self.path.read_bytes(), before)
