"""从已登记配置说明实际执行范围，不读取凭据或推断用户任务是否完成。"""


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
        mode,label='fixed_tests','固定测试流程'
        summary='只执行配置中的固定测试命令；不调用模型、不执行开发阶段。测试通过只证明这些命令通过。'
    elif simulated:
        mode,label='simulation','含模拟执行器的流程'
        summary='包含模拟执行器；模拟阶段用于演示，不能作为真实业务开发交付。'
    elif code:
        mode,label='development','代码开发流程'
        summary='按配置调用开发执行器；完成范围以代码差异、真实测试和审查证据为准。'
    else:
        mode,label='analysis','分析与验证流程'
        summary='此配置没有代码开发阶段；报告或测试通过不能代表已修改业务实现。'
    return {'mode':mode,'label':label,'summary':summary,'workdir':str(config.task_workdir),
            'uses_model':bool(model_stages),'has_development_stage':code,'stages':stages,
            'declared_files':list(config.autonomy.allowed_files),
            'task_text_changes_execution_scope':False}
