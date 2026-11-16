"""真实维护需求：doctor 不应把合法内联提示词误判为缺失 Markdown 文件。"""
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0,str(Path.cwd()))
from orchestrator.doctor import diagnose


class DoctorInline(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def report(self,prompt):
        import json
        config=self.root/'pipeline.toml'
        config.write_text('task_workdir="workspace"\nstate_dir="state"\n'
            '[adapters.local]\ntype="fake"\n[[stages]]\nname="report"\nkind="report"\n'
            'adapter="local"\nprompt='+json.dumps(prompt,ensure_ascii=False)+'\n',encoding='utf8')
        return diagnose(config)

    def test_inline_text_ending_in_markdown_name_is_healthy(self):
        for prompt in ('请阅读后概括 README.md','Read the implementation.\nThen summarize docs/README.md'):
            with self.subTest(prompt=prompt):
                report=self.report(prompt)
                self.assertTrue(report['ok'],report)

    def test_explicit_missing_paths_are_still_errors(self):
        for prompt in ('prompts/missing.md','./instructions/missing.md','instructions/missing.md'):
            with self.subTest(prompt=prompt):
                self.assertFalse(self.report(prompt)['ok'])

    def test_existing_prompt_path_with_spaces_is_healthy(self):
        path=self.root/'instructions/review notes.md';path.parent.mkdir()
        path.write_text('review code',encoding='utf8')
        self.assertTrue(self.report('instructions/review notes.md')['ok'])


if __name__=='__main__':unittest.main()
