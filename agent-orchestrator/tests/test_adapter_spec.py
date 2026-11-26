"""适配器参数声明：校验规则、注册表与工厂/配置/doctor 一致性。"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlunsplit

from orchestrator.adapters import registry
from orchestrator.adapters.factory import build_adapter
from orchestrator.adapters.spec import (MISSING, AdapterSpec, Capabilities, OptionSpec,
                                        check_http_url, validate_options)
from orchestrator.config import KNOWN_ADAPTER_TYPES, AdapterConfig, ConfigError, parse_config

API = {'base_url': 'https://api.example.com/v1', 'model': 'm', 'api_key_env': 'TEST_KEY'}


def _cfg(adapter: dict, base: Path):
    return parse_config({'task_workdir': 'ws', 'state_dir': 'state',
                         'adapters': {'a': adapter},
                         'stages': [{'name': 'draft', 'kind': 'report', 'adapter': 'a', 'prompt': 'p'}]},
                        base_dir=base)


class SpecContractTests(unittest.TestCase):
    def test_options_match_constructor_signature_and_defaults(self):
        for type_, spec in registry.specs().items():
            with self.subTest(type_):
                params = inspect.signature(spec.factory).parameters
                for o in spec.options:
                    self.assertIn(o.key, params, f'{type_}.{o.key} 不是构造器参数')
                    p = params[o.key]
                    if o.required:
                        self.assertIs(p.default, inspect.Parameter.empty, f'{type_}.{o.key} 必填性不一致')
                    else:
                        self.assertEqual(o.default, p.default, f'{type_}.{o.key} 默认值漂移')
                required = {n for n, p in params.items()
                            if p.default is inspect.Parameter.empty and n not in ('self', 'name')
                            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
                self.assertLessEqual(required, set(spec.keys), f'{type_} 有未声明的必填参数')

    def test_overridable_effort_matches_capability(self):
        spec = registry.get('openai_compat')
        self.assertEqual(spec.option('reasoning_effort').choices, spec.capabilities.reasoning_effort)
        self.assertEqual(spec.option('thinking').choices, spec.capabilities.thinking)
        self.assertEqual(set(spec.overridable_keys),
                         {'model', 'reasoning_effort', 'thinking', 'review_json_mode'})

    def test_registry_covers_known_adapter_types(self):
        self.assertEqual(set(registry.specs()), KNOWN_ADAPTER_TYPES)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            AdapterSpec('x', dict, (OptionSpec('a', str), OptionSpec('a', int)))

    def test_capabilities_enum_checked(self):
        with self.assertRaises(ValueError):
            Capabilities(session_resume='maybe')


class ValidateOptionsTests(unittest.TestCase):
    spec = property(lambda self: registry.get('openai_compat'))

    def errors(self, raw):
        _, issues = validate_options(self.spec, 'a', raw)
        return {i.path: i.message for i in issues if i.level == 'error'}

    def test_defaults_filled_without_mutating_input(self):
        raw = dict(API)
        out, issues = validate_options(self.spec, 'a', raw)
        self.assertEqual(issues, [])
        self.assertEqual(out['timeout_s'], 120)
        self.assertEqual(out['temperature'], 0.2)
        self.assertIsNone(out['max_tokens'])
        self.assertEqual(raw, API)

    def test_missing_required(self):
        self.assertEqual(set(self.errors({'model': 'm'})),
                         {'adapters.a.base_url', 'adapters.a.api_key_env'})

    def test_empty_string_required_rejected(self):
        self.assertIn('adapters.a.model', self.errors({**API, 'model': '  '}))

    def test_type_errors_are_strict(self):
        for key, value in (('timeout_s', '60'), ('timeout_s', True), ('timeout_s', 1.5),
                           ('temperature', float('nan')), ('temperature', -0.1),
                           ('max_tokens', 0), ('review_json_mode', 1)):
            with self.subTest(key=key, value=value):
                self.assertIn(f'adapters.a.{key}', self.errors({**API, key: value}))

    def test_int_accepted_as_float(self):
        self.assertEqual(self.errors({**API, 'temperature': 1}), {})

    def test_choices(self):
        self.assertIn('adapters.a.reasoning_effort', self.errors({**API, 'reasoning_effort': 'ultra'}))
        self.assertIn('adapters.a.thinking', self.errors({**API, 'thinking': 'auto'}))
        self.assertEqual(self.errors({**API, 'reasoning_effort': 'xhigh', 'thinking': 'enabled'}), {})

    def test_url_rejects_file_and_credentials(self):
        url_with_userinfo = urlunsplit(('https', 'test-user:test-password@api.example.com', '', '', ''))
        for url in ('file:///etc/passwd', url_with_userinfo, 'api.example.com'):
            with self.subTest(url=url):
                self.assertIsNotNone(check_http_url(url))
                self.assertIn('adapters.a.base_url', self.errors({**API, 'base_url': url}))

    def test_unknown_key_warns_with_suggestion(self):
        _, issues = validate_options(self.spec, 'a', {**API, 'max_token': 100})
        self.assertEqual([(i.level, i.path) for i in issues], [('warning', 'adapters.a.max_token')])
        self.assertIn('max_tokens', issues[0].hint)

    def test_workbench_override_rules_follow_spec(self):
        from types import SimpleNamespace
        from orchestrator.collaboration import model_settings
        cfg = SimpleNamespace(adapters={
            'q': AdapterConfig('q', 'cli_qoder', {}), 'w': AdapterConfig('w', 'cli_qwen', {}),
            'a': AdapterConfig('a', 'openai_compat', dict(API))})
        ok = model_settings.validate_options(cfg, {'models': {
            'q': {'reasoning_effort': 'max', 'thinking': 'adaptive'}, 'a': {'review_json_mode': True}}})
        self.assertEqual(ok['models']['q'], {'reasoning_effort': 'max', 'thinking': 'adaptive'})
        for bad in ({'w': {'reasoning_effort': 'high'}}, {'q': {'thinking': 'enabled'}},
                    {'q': {'review_json_mode': True}}, {'a': {'reasoning_effort': 'max'}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model_settings.validate_options(cfg, {'models': bad})

    def test_readme_adapter_block_is_generated(self):
        from scripts.generate_config_docs import ADAPTER_START, ADAPTER_END, ROOT
        from orchestrator.adapters.spec import render_adapter_tables
        text = (ROOT / 'README.md').read_text(encoding='utf8')
        self.assertEqual(text.split(ADAPTER_START)[1].split(ADAPTER_END)[0],
                         '\n' + render_adapter_tables(registry.specs()))

    def test_unknown_key_strict(self):
        _, issues = validate_options(self.spec, 'a', {**API, 'max_token': 100}, strict_unknown=True)
        self.assertEqual(issues[0].level, 'error')

    def test_list_items(self):
        spec = AdapterSpec('x', dict, (OptionSpec('extra_env', list, (), item_type=str),))
        out, issues = validate_options(spec, 'a', {})
        self.assertEqual(out['extra_env'], [])
        _, issues = validate_options(spec, 'a', {'extra_env': ['A', '']})
        self.assertEqual(issues[0].level, 'error')
        self.assertIs(MISSING, OptionSpec('k', str).default)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_config_rejects_type_error_with_field_path(self):
        with self.assertRaises(ConfigError) as ctx:
            _cfg({'type': 'openai_compat', **API, 'timeout_s': '60'}, self.base)
        self.assertIn('adapters.a.timeout_s', str(ctx.exception))

    def test_parse_config_keeps_raw_options_and_collects_warning(self):
        cfg = _cfg({'type': 'openai_compat', **API, 'max_token': 5}, self.base)
        self.assertEqual(cfg.adapters['a'].options, {**API, 'max_token': 5})  # 指纹输入不变
        self.assertEqual([i.path for i in cfg.adapter_warnings], ['adapters.a.max_token'])

    def test_fake_unknown_key_only_warns(self):
        cfg = _cfg({'type': 'fake', 'model': 'x'}, self.base)
        self.assertEqual(len(cfg.adapter_warnings), 1)

    def test_factory_builds_with_defaults_and_kind(self):
        adapter = build_adapter(AdapterConfig('a', 'openai_compat', dict(API)), kind='review')
        self.assertEqual((adapter.timeout_s, adapter.temperature, adapter.kind), (120, 0.2, 'review'))
        self.assertEqual(build_adapter(AdapterConfig('f', 'fake', {'kind': 'code'})).kind, 'code')

    def test_factory_rejects_invalid_direct_config(self):
        with self.assertRaises(ValueError):
            build_adapter(AdapterConfig('a', 'openai_compat', {**API, 'timeout_s': 0}))

    def test_factory_builds_cli_via_spec(self):
        from orchestrator.adapters.cli_registry import build
        codex = build_adapter(AdapterConfig('c', 'cli_codex', {'model': 'm', 'isolate_config': True}))
        self.assertEqual((codex.model, codex.isolate_config, codex.timeout_s), ('m', True, 300))
        self.assertEqual(type(build(AdapterConfig('q', 'cli_qwen', {}))).__name__, 'QwenAdapter')
        self.assertEqual(build_adapter(AdapterConfig('d', 'cli_dsh', {})).permission_mode, 'read-only')

    def test_doctor_reports_unknown_key_warning(self):
        from orchestrator.doctor import diagnose
        path = self.base / 'c.toml'
        path.write_text('task_workdir="ws"\nstate_dir="state"\n[adapters.a]\ntype="fake"\nmodle="x"\n'
                        '[[stages]]\nname="draft"\nkind="report"\nadapter="a"\nprompt="p"\n', encoding='utf8')
        report = diagnose(path)
        checks = report['checks'] if isinstance(report, dict) else report
        hits = [c for c in checks if 'adapters.a.modle' in str(c)]
        self.assertTrue(hits, checks)
        self.assertIn('warning', str(hits[0]))


if __name__ == '__main__':
    unittest.main()
