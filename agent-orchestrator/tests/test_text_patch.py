import json,tempfile,unittest
from pathlib import Path
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
class TextPatchProtocol(unittest.TestCase):
    def test_exact_patch_preserves_unrelated_content_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); target=root/'module.py'; target.write_bytes(b'one\r\ntwo\r\nthree\r\n')
            payload=json.dumps({'edits':[{'old':'two\n','new':'changed\n'}]})
            text='```json\n# patch: module.py\n'+payload+'\n```'
            files=OpenAICompatAdapter._write_code_files(text,root,'coding')
            self.assertEqual(files,[str(target.resolve())]); self.assertEqual(target.read_bytes(),b'one\r\nchanged\r\nthree\r\n')
    def test_ambiguous_old_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); target=root/'x.py'; target.write_text('same same', encoding="utf-8")
            with self.assertRaises(ValueError):
                OpenAICompatAdapter._apply_text_patch(root,'x.py',json.dumps({'edits':[{'old':'same','new':'x'}]}))
            self.assertEqual(target.read_text(encoding="utf-8"),'same same')
    def test_late_mismatch_does_not_partially_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); target=root/'x.py'; target.write_text('one two', encoding="utf-8")
            with self.assertRaises(ValueError):
                OpenAICompatAdapter._apply_text_patch(root,'x.py',json.dumps({'edits':[{'old':'one','new':'first'},{'old':'absent','new':'x'}]}))
            self.assertEqual(target.read_text(encoding="utf-8"),'one two')
    def test_patch_outside_root_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'ws';root.mkdir(); outside=root.parent/'outside.py';outside.write_text('safe', encoding="utf-8")
            with self.assertRaises((ValueError,RuntimeError)):
                OpenAICompatAdapter._apply_text_patch(root,'../outside.py',json.dumps({'edits':[{'old':'safe','new':'bad'}]}))
            self.assertEqual(outside.read_text(encoding="utf-8"),'safe')
    def test_malformed_shape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'x.py';target.write_text('original', encoding="utf-8")
            for value in ([],{'edits':[]},{'edits':[{'old':'','new':'x'}]},{'edits':[{'old':'original','new':3}]}):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    OpenAICompatAdapter._apply_text_patch(root,'x.py',json.dumps(value))
            self.assertEqual(target.read_text(encoding="utf-8"),'original')

    def test_explicit_bare_patch_stream_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); a=root/'a.py'; b=root/'b.py'; a.write_text('one', encoding="utf-8"); b.write_text('two', encoding="utf-8")
            text='# patch: a.py\n'+json.dumps({'edits':[{'old':'one','new':'first'}]})+'\n# patch: b.py\n'+json.dumps({'edits':[{'old':'two','new':'second'}]})
            self.assertEqual(len(OpenAICompatAdapter._write_code_files(text,root,'coding')),2)
            self.assertEqual(a.read_text(encoding="utf-8"),'first');self.assertEqual(b.read_text(encoding="utf-8"),'second')
    def test_bare_patch_with_trailing_prose_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'a.py';target.write_text('original', encoding="utf-8")
            text='# patch: a.py\n'+json.dumps({'edits':[{'old':'original','new':'changed'}]})+'\nThis is not a patch header'
            with self.assertRaises(ValueError):OpenAICompatAdapter._write_code_files(text,root,'coding')
            self.assertEqual(target.read_text(encoding="utf-8"),'original')

    def test_bare_patch_backticks_in_json_do_not_change_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);a=root/'a.py';b=root/'b.py';a.write_text('```one```', encoding="utf-8");b.write_text('```two```', encoding="utf-8")
            text='# patch: a.py\n'+json.dumps({'edits':[{'old':'```one```','new':'```first```'}]})+'\n# patch: b.py\n'+json.dumps({'edits':[{'old':'```two```','new':'```second```'}]})
            self.assertEqual(len(OpenAICompatAdapter._write_code_files(text,root,'coding')),2)
            self.assertEqual(a.read_text(encoding="utf-8"),'```first```');self.assertEqual(b.read_text(encoding="utf-8"),'```second```')
    def test_invalid_patch_result_preserves_paid_usage(self):
        import io
        from unittest import mock
        from orchestrator.contracts import RunContext
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'a.py';target.write_text('original', encoding="utf-8")
            response='''# patch: a.py
{"edits":[{"old":"absent","new":"changed"}]}'''
            body=io.BytesIO(json.dumps({'choices':[{'message':{'content':response}}],'usage':{'total_tokens':123}}).encode())
            adapter=OpenAICompatAdapter('api','https://example.invalid','model','TEXT_PATCH_TEST_KEY')
            context=RunContext('t','r','coding',root,root/'runs',kind='code')
            with mock.patch.dict('os.environ',{'TEXT_PATCH_TEST_KEY':'dummy'}),mock.patch('urllib.request.urlopen',return_value=body):
                result=adapter.run('p',root,context)
            self.assertFalse(result.ok);self.assertEqual(result.usage['tokens'],123)
            self.assertEqual(target.read_text(encoding="utf-8"),'original');self.assertTrue(result.stdout)
    def test_patch_edit_count_bound_rejects_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'a.py';target.write_text('original', encoding="utf-8")
            with self.assertRaises(ValueError):
                OpenAICompatAdapter._apply_text_patch(root,'a.py',json.dumps({'edits':[{'old':'original','new':'changed'}]*33}))
            self.assertEqual(target.read_text(encoding="utf-8"),'original')
