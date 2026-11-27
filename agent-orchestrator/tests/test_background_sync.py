"""后台同步须覆盖全部历史、限制每批开销、保留恢复与控制收据。"""
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.collaboration.service import Service
from orchestrator.collaboration.background_sync import BackgroundSync
from orchestrator.collaboration import background_sync
from orchestrator.dashboard.server import Application
from orchestrator.state import atomic_write_json


class BackgroundSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = self.root/'c.toml'
        self.cfg.write_text('task_workdir="ws"\nstate_dir="state"\n'
                            '[[stages]]\nname="verify"\nkind="test"\n'
                            'test_cmd=["python","-c","print(1)"]\n', encoding='utf8')
        self.s = Service(self.root/'db.sqlite3', [])
        self.addCleanup(self.s._background_sync.close)
        self.project = self.s.store.register_project(self.cfg)

    def seed(self, number, managed=False, config=None, project=None):
        name = f'r-{number:04}'
        cfg = config or self.cfg
        path = self.root/('state' if cfg == self.cfg else 'other-state')/'runs'/name
        path.mkdir(parents=True, exist_ok=True)
        state = {'task':name, 'revision':1, 'stages':{'verify':{'status':'done'}},
                 'events':[{'kind':'test_pass', 'event_id':'event-1', 'stage':'verify', 'ts':1}]}
        (path/'state.json').write_text(json.dumps(state), encoding='utf8')
        (path/'job.json').write_text('{"status":"done"}', encoding='utf8')
        key = self.s.store.register_run(project or self.project, name, cfg, path.parents[1], managed=managed)
        # 相同时间、数百条记录，不能依靠首页或时间戳翻页。
        with self.s.store.connect() as db:db.execute('UPDATE runs SET created=1000 WHERE id=?', (key,))
        return key, path

    def count_offsets(self):
        with self.s.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM projection_offsets').fetchone()[0]

    def seed_many(self, numbers, *, managed_last=False):
        """仅批量准备夹具时复用连接；观察阶段仍使用产品默认持久化设置。"""
        numbers=list(numbers)
        with self.s.store.connect() as db:
            original_sync=db.execute('PRAGMA synchronous').fetchone()[0]
        with self.s.store.session():
            with self.s.store.connect() as db:
                db.execute('PRAGMA synchronous=NORMAL')
            result=[self.seed(i,managed=(managed_last and i==numbers[-1])) for i in numbers]
        with self.s.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],original_sync)
        return result

    def test_full_sync_projects_more_than_one_hundred_with_equal_timestamps(self):
        self.seed_many(range(137))
        stats = self.s.sync()
        self.assertEqual(stats['runs_checked'],137)
        self.assertEqual(self.count_offsets(),137)
        with self.s.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_log WHERE kind='engine_event'").fetchone()[0],137)

    def test_incremental_tick_is_bounded_and_sweeps_all_records(self):
        self.seed_many(range(137))
        with mock.patch.object(self.s,'launch',side_effect=AssertionError('历史不应执行模型或任务')):
            for _ in range(3):
                stats = self.s.sync(recover=True, incremental=True)
                self.assertLessEqual(stats['directories_checked'],50)
                self.assertLessEqual(stats['runs_checked'],50)
        self.assertEqual(self.count_offsets(),137)

    def test_warm_sync_avoids_job_reads_and_repeated_registration_writes(self):
        self.seed_many(range(8))
        self.s.sync()
        statements = []
        with self.s.store.session():
            with self.s.store.connect() as db:db.set_trace_callback(statements.append)
            with mock.patch('pathlib.Path.read_text',side_effect=AssertionError('未变化状态不应重新读取')):
                stats = self.s.sync(recover=True, incremental=True)
            with self.s.store.connect() as db:db.set_trace_callback(None)
        self.assertEqual(stats['imports_updated'],0)
        self.assertEqual(stats['imports_unchanged'],8)
        self.assertFalse([s for s in statements if s.lstrip().split()[0] in ('INSERT','UPDATE','DELETE','REPLACE')])

    def test_old_state_change_and_new_directory_are_eventually_discovered(self):
        entries=self.seed_many(range(137))
        self.s.sync()
        key,path=entries[0]
        state=json.loads((path/'state.json').read_text(encoding='utf8'))
        state['revision']=2
        state['events'].append({'kind':'changed','event_id':'event-2','ts':2})
        atomic_write_json(path/'state.json',state)
        # 独立目录中的新历史，没有预登记数据库。
        new=self.root/'state/runs/new-history';new.mkdir()
        atomic_write_json(new/'state.json',{'task':'new','stages':{},'events':[]})
        for _ in range(7):self.s.sync(incremental=True)
        self.assertEqual(sum(e['payload'].get('kind')=='changed' for e in self.s.store.events(key)),1)
        self.assertEqual(self.s.store.run(self.project+':new-history')['run_id'],'new-history')

    def test_old_managed_run_commands_and_topics_recover_beyond_first_page(self):
        # 受管运行必须取首页之外的最后一条，才能证明旧实现（只投影最近 100 条）
        # 真的漏掉它；用 r-0000 会在旧实现首页内，用例失去鉴别力。
        entries=self.seed_many(range(137),managed_last=True)
        key,path=entries[136]
        atomic_write_json(path/'job.json',{'status':'paused'})
        command=self.s.store.enqueue(key,'resume',{},'recover-command-001')
        topic=self.s.store.create_thread(key,'question',['developer'],1000,{},'recover-topic-001')
        with mock.patch.object(self.s,'launch') as launch, mock.patch.object(self.s,'launch_discussion') as discuss:
            for _ in range(3):self.s.sync(recover=True,incremental=True)
        launch.assert_any_call(key)
        discuss.assert_any_call(topic['id'])
        self.assertEqual(self.s.store.command(command['id'])['status'],'queued')

    def test_managed_workers_receive_only_declared_adapter_environment(self):
        self.cfg.write_text(
            'task_workdir="ws"\nstate_dir="state"\n'
            '[[stages]]\nname="verify"\nkind="test"\n'
            'test_cmd=["python","-c","print(1)"]\n'
            '[adapters.qwen]\ntype="cli_qwen"\n'
            'extra_env=["QWEN_API_KEY","OPENAI_BASE_URL"]\n'
            '[adapters.api]\ntype="openai_compat"\n'
            'base_url="https://example.invalid/v1"\nmodel="m"\n'
            'api_key_env="API_KEY_ENV"\n', encoding='utf8')
        service = Service(self.root/'workers.sqlite3', [self.cfg], recover=False)
        self.addCleanup(service._background_sync.close)
        project = service.store.register_project(self.cfg)
        run_path = self.root/'state/runs/env-run'
        run_path.mkdir(parents=True)
        atomic_write_json(run_path/'state.json', {'task':'env', 'stages':{}, 'events':[]})
        atomic_write_json(run_path/'job.json', {'status':'pending'})
        key = service.store.register_run(project, 'env-run', self.cfg, self.root/'state', managed=True)
        topic = service.store.create_thread(key, 'question', ['developer'], 1000, {}, 'env-topic-001')
        env = {'QWEN_API_KEY':'qwen-private-key', 'OPENAI_BASE_URL':'https://example.invalid/v1',
               'API_KEY_ENV':'api-private-key', 'UNDECLARED_API_KEY':'must-not-leak'}
        with mock.patch.dict(os.environ, env), \
                mock.patch('orchestrator.collaboration.service.subprocess.Popen') as popen:
            popen.return_value.poll.return_value = None
            service.launch(key)
            service.launch_discussion(topic['id'])
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            child_env = call.kwargs['env']
            self.assertEqual(child_env['QWEN_API_KEY'], env['QWEN_API_KEY'])
            self.assertEqual(child_env['OPENAI_BASE_URL'], env['OPENAI_BASE_URL'])
            self.assertEqual(child_env['API_KEY_ENV'], env['API_KEY_ENV'])
            self.assertNotIn('UNDECLARED_API_KEY', child_env)

    def test_receipt_written_after_last_visit_reconciles_on_next_sweep(self):
        key,path=self.seed(1)
        self.s.sync(recover=True)
        command=self.s.store.enqueue(key,'pause',{},'receipt-command-001')
        state=json.loads((path/'state.json').read_text(encoding='utf8'))
        state['control']={'receipts':{command['id']:{'status':'applied','result':{'checkpoint':True}}}}
        atomic_write_json(path/'state.json',state)
        self.s.sync(recover=True,incremental=True)
        self.assertEqual(self.s.store.command(command['id'])['status'],'applied')
        cursor=self.s.store.last_cursor(key)
        self.s.sync(recover=True,incremental=True)
        self.assertEqual(self.s.store.last_cursor(key),cursor)

    def test_incremental_bad_record_does_not_starve_later_records(self):
        entries=self.seed_many(range(3))
        original=background_sync.project
        def fail(store,run,cache):
            if run['id']==entries[0][0]:raise OSError('transient state update')
            return original(store,run,cache)
        with mock.patch.object(background_sync,'project',fail):
            stats=self.s.sync(incremental=True)
        self.assertEqual(stats['errors'],1)
        self.assertEqual(self.count_offsets(),2)
        self.s.sync(incremental=True)
        self.assertEqual(self.count_offsets(),3)

    def test_directory_handles_close_and_closed_sync_stops_scanning(self):
        self.seed_many(range(51))
        self.s.sync(incremental=True)
        self.assertTrue(self.s._background_sync.scans)
        self.s.closed.set()
        stats=self.s.sync(incremental=True)
        self.assertEqual(stats['directories_checked'],0)
        self.assertEqual(self.s._background_sync.scans,{})

    def test_project_config_change_discards_old_directory_iterator(self):
        self.seed(1)
        self.s.sync(incremental=True)
        text=self.cfg.read_text(encoding='utf8').replace('state_dir="state"','state_dir="other-state"')
        self.cfg.write_text(text,encoding='utf8')
        path=self.root/'other-state/runs/new-root';path.mkdir(parents=True)
        atomic_write_json(path/'state.json',{'task':'changed root','events':[],'stages':{}})
        for _ in range(3):self.s.sync(incremental=True)
        run=self.s.store.run(self.project+':new-root')
        self.assertEqual(Path(run['state_dir']),self.root/'other-state')

    def test_import_and_projection_caches_are_bounded(self):
        self.s._background_sync.IMPORT_CACHE_LIMIT=3
        self.s._background_sync.PROJECTION_CACHE_LIMIT=2
        self.seed_many(range(7))
        self.s.sync()
        self.assertEqual(len(self.s._background_sync.imports),3)
        self.assertEqual(len(self.s._projection_cache),2)
        self.assertLessEqual(len(self.s._background_sync.projected),3)
        self.assertEqual(self.count_offsets(),7)

    def test_small_full_state_cache_does_not_force_unchanged_history_rereads(self):
        self.s._background_sync.PROJECTION_CACHE_LIMIT=2
        self.seed_many(range(7))
        self.s.sync()
        self.assertEqual(len(self.s._projection_cache),2)
        with mock.patch('pathlib.Path.read_text',side_effect=AssertionError('只读签名足以跳过未变化投影')):
            stats=self.s.sync(incremental=True)
        self.assertEqual(stats['projections_unchanged'],7)

    def test_projects_share_directory_budget_without_starving_small_project(self):
        self.seed_many(range(61))
        other=self.root/'other.toml'
        other.write_text(self.cfg.read_text(encoding='utf8').replace('state_dir="state"',
                         'state_dir="other-state"'),encoding='utf8')
        project=self.s.store.register_project(other)
        path=self.root/'other-state/runs/discovered';path.mkdir(parents=True)
        atomic_write_json(path/'state.json',{'task':'other project','events':[],'stages':{}})
        stats=self.s.sync(incremental=True)
        self.assertLessEqual(stats['directories_checked'],50)
        self.assertEqual(self.s.store.run(project+':discovered')['project_id'],project)

    def test_inserted_id_before_cursor_is_seen_after_wraparound(self):
        self.seed_many(range(1,62))
        self.s.sync(incremental=True)
        key,_=self.seed(0)
        for _ in range(3):self.s.sync(incremental=True)
        self.assertTrue(any(e['kind']=='engine_event' for e in self.s.store.events(key)))

    def test_bad_project_config_does_not_block_other_projects(self):
        other=self.root/'other.toml'
        other.write_text(self.cfg.read_text(encoding='utf8').replace('state_dir="state"',
                         'state_dir="other-state"'),encoding='utf8')
        other_project=self.s.store.register_project(other)
        key,_=self.seed(1,config=other,project=other_project)
        self.cfg.unlink()
        stats=self.s.sync(incremental=True)
        self.assertGreaterEqual(stats['errors'],1)
        self.assertEqual(self.s.store.run(key)['project_id'],other_project)
        projects={p['name']:p for p in self.s.list_projects()}
        self.assertEqual(projects['c']['config'],str(self.cfg.resolve()))
        self.assertIsNone(projects['c']['execution_scope'])
        self.assertIsNone(projects['c']['config_hash'])
        self.assertTrue(projects['c']['unavailable'])
        self.assertIsNotNone(projects['other']['execution_scope'])
        self.assertEqual(projects['other']['config_hash'],hashlib.sha256(other.read_bytes()).hexdigest())
        # 改坏（而不是删除）同样只影响该条目，下一轮仍会重试而不是缓存失败。
        self.cfg.write_text('task_workdir = \n[[[ broken',encoding='utf8')
        stats=self.s.sync(incremental=True)
        self.assertGreaterEqual(stats['errors'],1)
        self.assertTrue({p['name']:p for p in self.s.list_projects()}['c']['unavailable'])

    def test_malformed_state_does_not_abort_incremental_sync(self):
        entries=self.seed_many(range(3))
        (entries[0][1]/'state.json').write_text(json.dumps({'stages':[]}),encoding='utf8')
        stats=self.s.sync(incremental=True)
        self.assertGreaterEqual(stats['errors'],1)
        self.assertEqual(self.count_offsets(),2)
        summaries={r['run_id']:r for r in self.s.list_runs()}
        self.assertEqual(summaries['r-0000']['task'],'')
        self.assertEqual(summaries['r-0000']['lifecycle'],'unknown')
        # 原样再跑一轮：畸形记录继续被跳过，不会把后续健康记录卡住。
        stats=self.s.sync(incremental=True)
        self.assertGreaterEqual(stats['errors'],1)
        self.assertEqual(self.count_offsets(),2)

    def test_service_starts_when_a_run_directory_state_is_malformed(self):
        config=self.root/'startup.toml'
        config.write_text('task_workdir="ws"\nstate_dir="state-start"\n'
                          '[[stages]]\nname="verify"\nkind="test"\n'
                          'test_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        good=self.root/'state-start/runs/good';bad=self.root/'state-start/runs/bad'
        good.mkdir(parents=True);bad.mkdir(parents=True)
        atomic_write_json(bad/'state.json',{'stages':[]})
        atomic_write_json(bad/'job.json',{})
        atomic_write_json(good/'state.json',{'task':'健康记录','stages':{},'events':[]})
        atomic_write_json(good/'job.json',{'status':'done'})
        service=Service(self.root/'startup.sqlite3',[config])
        self.addCleanup(service._background_sync.close)
        self.addCleanup(service.closed.set)
        self.assertIn('运行状态形状非法',service.last_sync_error)
        summaries={r['run_id']:r for r in service.list_runs()}
        self.assertEqual(summaries['bad']['lifecycle'],'unknown')
        self.assertEqual(summaries['good']['lifecycle'],'done')
        stats=service.sync(incremental=True)
        self.assertGreaterEqual(stats['errors'],1)
        project=service.store.projects()[0]['id']
        self.assertEqual(service.snapshot(project+':good')['task'],'健康记录')

    def test_projection_thread_survives_sync_exception(self):
        app=Application(self.root/'dash.sqlite3',[])
        self.addCleanup(app.close)
        with mock.patch.object(app.service,'sync',side_effect=RuntimeError('投影线程单次失败')):
            app.start_projection()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and not app.service.last_sync_error:
                time.sleep(.05)
            self.assertIn('投影线程单次失败',app.service.last_sync_error)
            self.assertTrue(app.projection_thread.is_alive())


if __name__=='__main__':unittest.main()
