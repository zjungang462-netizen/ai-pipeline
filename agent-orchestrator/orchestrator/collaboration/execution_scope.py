"""从已登记配置说明实际执行范围，不读取凭据或推断用户任务是否完成。"""


# 已验收模板的原始文件指纹，分别对应 LF 和 CRLF 检出。修改演练内容时与验收一起更新。
_SHOWCASE_HASHES = frozenset({
    'b52af36f4bf49f93340bfa0f26a56b713842181746e7928fd42baedc2acbb648',
    '569247cd7b18d31e821ec580211fe83708100a3c44d48b936c9662022618570d',
})


def is_verified_showcase(config):
    return (config.source_path is not None
            and config.source_path.name == 'kiroween-demo.example.toml'
            and config.source_hash in _SHOWCASE_HASHES)


def execution_scope(config):
    stages=[]
    for stage in config.stages:
        adapter=config.adapters.get(stage.adapter) if stage.kind!='test' else None
        stages.append({'name':stage.name,'kind':stage.kind,
                       'adapter_type':adapter.type if adapter else 'command'})
    model_stages=[stage for stage in stages if stage['adapter_type'] not in ('command','fake')]
    simulated=any(stage['adapter_type']=='fake' for stage in stages)
    code=any(stage['kind']=='code' and stage['adapter_type'] not in ('fake','command') for stage in stages)
    test_only=bool(stages) and all(stage['kind']=='test' for stage in stages)
    if test_only:
        mode,label,label_en='fixed_tests','固定测试流程','Fixed-test workflow'
        summary='只执行配置中的固定测试命令；不调用模型、不执行开发阶段。测试通过只证明这些命令通过。'
        summary_en='Runs only the configured test commands; no model or development stage is invoked. Passing tests proves only those commands passed.'
    elif simulated:
        mode,label,label_en='simulation','含模拟执行器的流程','Workflow with simulated executors'
        summary='包含模拟执行器；模拟阶段用于演示，不能作为真实业务开发交付。'
        summary_en='Includes simulated executors for demonstration; simulated stages are not evidence of real business development delivery.'
    elif code:
        mode,label,label_en='development','代码开发流程','Code development workflow'
        summary='按配置调用开发执行器；完成范围以代码差异、真实测试和审查证据为准。'
        summary_en='Invokes the configured development executor; completion is defined by code diffs, real tests, and review evidence.'
    else:
        mode,label,label_en='analysis','分析与验证流程','Analysis and verification workflow'
        summary='此配置没有代码开发阶段；报告或测试通过不能代表已修改业务实现。'
        summary_en='This configuration has no code development stage; passing reports or tests does not mean business implementation changed.'
    return {'mode':mode,'label':label,'label_en':label_en,'summary':summary,'summary_en':summary_en,
            'workdir':str(config.task_workdir),
            'uses_model':bool(model_stages),'has_development_stage':code,'stages':stages,
            'declared_files':list(config.autonomy.allowed_files),
            'task_text_changes_execution_scope':False}
