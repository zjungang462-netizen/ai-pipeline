"""真实 Windows 维护需求：扫描带 BOM 的 UTF-16 脚本，不能误作二进制略过。"""
import codecs
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path.cwd()))
from scripts.precommit import credential_findings,MAX_SCAN_BYTES


class UTF16Credentials(unittest.TestCase):
    def test_utf16_le_and_be_credentials_are_found_without_echo(self):
        token='sk-'+('S'*30)
        text='# windows script\napi_key="'+token+'"\n'
        for bom,encoding in ((codecs.BOM_UTF16_LE,'utf-16-le'),(codecs.BOM_UTF16_BE,'utf-16-be')):
            with self.subTest(encoding=encoding):
                rows=credential_findings(bom+text.encode(encoding),'script.ps1','index')
                self.assertTrue(any(row['kind']=='token_pattern' and row['line']==2 for row in rows),rows)
                self.assertNotIn(token,json.dumps(rows))

    def test_invalid_declared_utf16_is_not_silently_ignored(self):
        rows=credential_findings(codecs.BOM_UTF16_LE+b'\x00','script.ps1','working_tree')
        self.assertTrue(any(row['kind']=='encoding_error' for row in rows),rows)

    def test_large_utf16_is_rejected_before_decoding(self):
        data=codecs.BOM_UTF16_LE+('x'*(MAX_SCAN_BYTES//2+1)).encode('utf-16-le')
        rows=credential_findings(data,'script.ps1','index')
        self.assertTrue(any(row['kind']=='scan_size_limit' for row in rows),rows)

    def test_binary_and_regular_utf8_behavior_is_preserved(self):
        self.assertEqual(credential_findings(b'\x89PNG\r\n\x1a\n\x00\x00','image.png','index'),[])
        self.assertEqual(credential_findings(b'\x89PNG\0'+b'x'*MAX_SCAN_BYTES,'large-image.png','index'),[])
        self.assertEqual(credential_findings(b'print("ok")\n','source.py','index'),[])


if __name__=='__main__':unittest.main()
