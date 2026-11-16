"""生成文档是提交门禁的一部分，漂移应失败，示例值保持不变。"""
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.generate_config_docs import (ROOT, annotated_example, check_generated,
                                         generated_files, START, END)


class SchemaDocuments(unittest.TestCase):
    def test_repository_generated_files_are_current(self):
        self.assertEqual(check_generated(),[])

    def test_example_regeneration_is_idempotent_and_preserves_values(self):
        for path in ROOT.glob('*.example.toml'):
            with self.subTest(name=path.name):
                before=path.read_text(encoding='utf8')
                after=annotated_example(before)
                self.assertEqual(tomllib.loads(before),tomllib.loads(after))
                self.assertEqual(annotated_example(after),after)

    def test_drift_check_rejects_stale_table_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);readme=root/'README.md'
            text='prefix\n'+START+'\nstale table\n'+END+'\nsuffix\n'
            readme.write_text(text,encoding='utf8')
            self.assertEqual(check_generated(root),[str(readme)])
            self.assertEqual(readme.read_text(encoding='utf8'),text)
            fresh=generated_files(root)[readme]
            self.assertIn('watch_enforce',fresh)
            readme.write_text(fresh,encoding='utf8')
            self.assertFalse(check_generated(root))
