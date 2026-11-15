"""真实自验证发现的元数据形状问题，验证失败关闭与可恢复性。"""
import json
import tempfile
import unittest
from pathlib import Path
from orchestrator.evidence import approved, digest, seal, validate
from orchestrator.state import RunState

class MetadataShapeContract(unittest.TestCase):
    def test_manifest_non_object_is_rejected(self):
        for value in ([], None, True, 7, 'bad'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)
                (path/'manifest.json').write_text(json.dumps(value),encoding='utf8')
                self.assertFalse(validate(path,{'outputs_hash':'invalid'}))

    def test_code_reference_non_object_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'report.md').write_text('ok',encoding='utf8')
            output=seal(path)
            data=json.loads((path/'manifest.json').read_text(encoding='utf8'))
            data['code']='bad'
            (path/'manifest.json').write_text(json.dumps(data),encoding='utf8')
            self.assertFalse(validate(path,{'outputs_hash':output}))

    def test_approval_non_object_is_rejected(self):
        for value in ([], None, 'bad', 7):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)
                (path/'approval.json').write_text(json.dumps(value),encoding='utf8')
                self.assertFalse(approved(path,{'n':1,'inputs_hash':'i','outputs_hash':'o'}))

    def make_state(self,directory):
        state=RunState(Path(directory),'r')
        number,path=state.new_attempt_dir('report')
        (path/'report.md').write_text('ok',encoding='utf8')
        state.commit_success('report',number,path,'input','ignored',None,None)
        return state,path

    def test_success_non_object_is_not_adopted(self):
        for value in ([],None,'bad',7):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                state,path=self.make_state(directory)
                (path/'success.json').write_text(json.dumps(value),encoding='utf8')
                self.assertFalse(state.is_complete('report','input'))
                self.assertIsNone(state.adopt_successful('report','input'))

    def test_decision_non_object_is_not_adopted(self):
        for value in ([],None,'bad',7):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                state,path=self.make_state(directory)
                (path/'decision.json').write_text(json.dumps(value),encoding='utf8')
                self.assertFalse(state.is_complete('report','input'))
                self.assertIsNone(state.adopt_successful('report','input'))

    def test_seal_does_not_inherit_malformed_code(self):
        for value in ([],None,{'code':'bad'},{'code':[]}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)
                (path/'report.md').write_text('ok',encoding='utf8')
                (path/'manifest.json').write_text(json.dumps(value),encoding='utf8')
                output=seal(path)
                self.assertTrue(validate(path,{'outputs_hash':output}))

    def test_digest_invalid_code_has_controlled_error(self):
        with self.assertRaises((ValueError,TypeError)):
            digest({'schema_version':2,'files':{'x':'y'},'code':'bad'})

    def test_valid_record_still_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            state,path=self.make_state(directory)
            self.assertTrue(state.is_complete('report','input'))
            decision=json.loads((path/'decision.json').read_text(encoding='utf8'))
            self.assertEqual(decision['verdict'],'pass')

    def test_files_list_is_rejected_even_when_hash_matches_bad_shape(self):
        from orchestrator.state import sha256_text
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'report.md').write_text('ok',encoding='utf8')
            bad={'schema_version':2,'files':['report.md']}
            (path/'manifest.json').write_text(json.dumps(bad),encoding='utf8')
            expected=sha256_text(json.dumps({'schema_version':2,'files':['report.md'],'tree':None},ensure_ascii=False,sort_keys=True))
            self.assertFalse(validate(path,{'outputs_hash':expected}))
