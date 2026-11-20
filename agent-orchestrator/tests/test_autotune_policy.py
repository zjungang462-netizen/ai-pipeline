"""预授权策略副本的自动选择、退化回退和配置不变性。"""
import json
import tempfile
import unittest
from pathlib import Path
from orchestrator.config import load_config
from orchestrator.state import atomic_write_json,sha256_file
from orchestrator.tuning import policy_tune,select_policy_config

class AutomaticTuning(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.file=self.root/'config.toml';self.state=self.root/'.s'
        self.file.write_text('''task_workdir="ws"
state_dir=".s"
[run_budget]
max_tokens=10000
max_duration_s=120
[autonomy]
enabled=true
allowed_files=["a.py","b.py"]
repair_batch_size=1
[autotune]
enabled=true
auto_apply=true
min_runs=5
max_batch_size=2
[[stages]]
name="coding"
adapter="fake"
kind="code"
prompt="p"
[[stages]]
name="testing"
kind="test"
test_cmd=["python","-c","pass"]
feedback_to="coding"
max_rounds=3
[adapters.fake]
type="fake"
''',encoding='utf8')
    def samples(self,source,count=5,tokens=100,cycles=1,unknown=0,prefix='r'):
        for i in range(count):
            atomic_write_json(self.state/'runs'/f'{prefix}{i}'/'metrics.json',{
                'run_id':f'{prefix}{i}','config_hash':sha256_file(source),'created_at':i,
                'tokens':tokens,'unknown_calls':unknown,'status':'done','verify_passed':True,
                'repair_cycles':cycles,'stages':{'testing':{'status':'done','rounds':1}},'fail_reasons':[]})
    def test_five_runs_apply_only_preallowed_copy(self):
        original=self.file.read_bytes();self.samples(self.file)
        result=policy_tune(self.file)
        self.assertEqual(result['status'],'applied_to_copy')
        selected=select_policy_config(self.file)
        self.assertNotEqual(selected,self.file);cfg=load_config(selected)
        self.assertEqual(cfg.autonomy.repair_batch_size,2)
        self.assertEqual(cfg.stages[1].max_rounds,3)
        self.assertEqual(self.file.read_bytes(),original)
        self.assertEqual(cfg.run_budget.max_tokens,load_config(self.file).run_budget.max_tokens)
    def test_prompt_suggestions_are_never_auto_applied(self):
        prompt_dir=self.root/'prompts';prompt_dir.mkdir()
        (prompt_dir/'coding.md').write_text('实现任务。\n',encoding='utf8')
        text=self.file.read_text(encoding='utf8').replace('prompt="p"','prompt="prompts/coding.md"')
        text += '''\n[[stages]]
name="review"
adapter="fake"
kind="review"
prompt="review"
feedback_to="coding"
max_rounds=3
'''
        self.file.write_text(text,encoding='utf8')
        original=self.file.read_bytes();self.samples(self.file)
        issue={'file':'a.py','problem':'遗漏边界处理','expected':'校验空输入'}
        for i in range(5):
            attempt=self.state/'runs'/f'r{i}'/'stages'/'review'/'attempts'/'1'
            attempt.mkdir(parents=True)
            atomic_write_json(attempt/'review_verdict.json',{'verdict':'fail','issues':[issue]})

        result=policy_tune(self.file)
        self.assertEqual(result['status'],'applied_to_copy')
        selected=load_config(result['config_path'])
        self.assertEqual(selected.stage('coding').prompt,'prompts/coding.md')
        self.assertEqual(self.file.read_bytes(),original)
        self.assertFalse(list(self.root.glob('config.tune-*.coding.prompt.md')))
    def test_test_command_suggestions_are_never_auto_applied(self):
        text=self.file.read_text(encoding='utf8').replace(
            'max_batch_size=2','max_batch_size=2\ntest_command_candidates={testing=[["python","-m","pytest","tests"]]}')
        self.file.write_text(text,encoding='utf8')
        original=self.file.read_bytes();self.samples(self.file,cycles=0)
        for i in range(5):
            path=self.state/'runs'/f'r{i}'/'metrics.json'
            data=json.loads(path.read_text(encoding='utf8'))
            data['status']='failed'
            data['stages']['testing']['status']='failed'
            atomic_write_json(path,data)
            attempt=self.state/'runs'/f'r{i}'/'stages'/'testing'/'attempts'/'1'
            atomic_write_json(attempt/'test_record.json',{'output':'Ran 0 tests','error':'测试未收集到任何用例'})

        result=policy_tune(self.file)
        self.assertEqual(result['status'],'proposed')
        self.assertEqual(self.file.read_bytes(),original)
        self.assertEqual(select_policy_config(self.file),self.file)
    def test_insufficient_or_unknown_usage_samples_do_not_apply(self):
        self.samples(self.file,4);self.assertEqual(policy_tune(self.file)['status'],'proposed')
        self.assertEqual(select_policy_config(self.file),self.file)
        self.samples(self.file,5,unknown=1,prefix='unknown')
        self.assertEqual(policy_tune(self.file)['status'],'proposed')
    def test_cost_regression_rolls_back(self):
        self.samples(self.file);first=policy_tune(self.file)
        candidate=Path(first['config_path']);self.samples(candidate,tokens=150,prefix='trial')
        self.assertEqual(select_policy_config(self.file),self.file)
    def test_tampered_candidate_is_never_selected(self):
        self.samples(self.file);first=policy_tune(self.file);candidate=Path(first['config_path'])
        candidate.write_text(candidate.read_text(encoding="utf-8")+'\n# tampered\n', encoding="utf-8")
        self.assertEqual(select_policy_config(self.file),self.file)
    def test_policy_chain_keeps_parent_lookup(self):
        self.samples(self.file);first=policy_tune(self.file);candidate=Path(first['config_path'])
        self.samples(candidate,cycles=0,prefix='trial');second=policy_tune(candidate)
        self.assertEqual(second['status'],'applied_to_copy')
        self.assertEqual(select_policy_config(self.file),Path(second['config_path']))
    def test_changed_original_configuration_does_not_auto_apply(self):
        self.samples(self.file);expected=sha256_file(self.file)
        self.file.write_text(self.file.read_text(encoding="utf-8")+'\n# changed\n', encoding="utf-8")
        with self.assertRaises(ValueError):policy_tune(self.file,expected)
    def test_rolled_back_strategy_is_not_automatically_reapplied(self):
        self.samples(self.file);first=policy_tune(self.file)
        candidate=Path(first['config_path']);self.samples(candidate,tokens=150,prefix='trial')
        self.assertEqual(select_policy_config(self.file),self.file)
        result=policy_tune(self.file)
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(select_policy_config(self.file),self.file)
    def test_malformed_policy_pointer_is_rejected_as_configuration_error(self):
        self.samples(self.file);policy_tune(self.file)
        for pointer in (self.state/'tuning').glob('next-*.json'):
            pointer.write_text('[]', encoding="utf-8")
        with self.assertRaises(ValueError):select_policy_config(self.file)

if __name__=='__main__':unittest.main()
