'use strict';
(function(){
 const messages={
  'doc.title':'Pipeline · Collaborative Workbench',
  'login.title':'Connect to the local workbench',
  'login.intro':'Open the complete link printed in the terminal for this session. The session is local-only and does not require a model API key.',
  'login.token':'Local session token','login.connect':'Establish session',
  'sidebar.workspace':'Local workspace','sidebar.newTask':'＋ New task','sidebar.addProject':'Add project','sidebar.insights':'Insights & optimization','language.toggle':'Switch interface language',
  'sidebar.taskTree':'Task worktree','sidebar.refresh':'Refresh task list','sidebar.toggleAria':'Toggle task list','sidebar.search':'Search tasks…','sidebar.searchAria':'Search tasks',
  'sidebar.allProjects':'All projects','sidebar.statusFilter':'Filter by status','sidebar.allStatuses':'All statuses','sidebar.running':'Running',
  'sidebar.paused':'Paused','sidebar.waitingExternal':'Waiting for external conditions','sidebar.waitingOwner':'Waiting for owner action',
  'sidebar.waitingApproval':'Waiting for approval','sidebar.done':'Completed','sidebar.failed':'Failed / cancelled','sidebar.archived':'Archived',
  'sidebar.created':'Created','sidebar.newest':'Newest first ↓','sidebar.oldest':'Oldest first ↑','sidebar.clearFilters':'Clear filters',
  'sidebar.dateGroups':'Date groups','sidebar.collapseDays':'Collapse all date groups','sidebar.expandDays':'Expand all date groups',
  'sidebar.runList':'Task list','sidebar.loadOlder':'Load older tasks','sidebar.connecting':'Connecting',
  'breadcrumb.overview':'Workspace / Run overview','cmd.open':'⌘K Command palette','cmd.openTitle':'Open global command palette (Ctrl+K)',
  'theme.toggleTitle':'Cycle light / dark / Kiroween theme','help.aria':'Workbench help','help.title':'Safety boundaries and controls',
  'hero.line':'Make every multi-agent step','hero.accent':'backed by verifiable evidence.',
  'hero.intro':'Orchestrate OpenAI-compatible APIs and local coding CLIs such as Claude / Codex / Qwen / Qoder / Antigravity, advancing the delivery loop with real test exit codes, artifact hashes, and human approval gates.',
  'hero.newPipeline':'＋ New pipeline task','hero.demo':'⚡ Load the five-stage demo template','hero.project':'⚙ Build a dynamic multi-node project',
  'hero.insights':'📊 Usage insights & dual-model arena','hero.statsAria':'Workspace overview','hero.projects':'Registered projects','hero.runs':'Loaded tasks',
  'hero.doneWaiting':'Completed / awaiting approval','hero.adapters':'{count} adapters','hero.adaptersLabel':'Adapters','hero.flowAria':'Standard evidence pipeline topology',
  'hero.flow.requirements':'Requirements & architecture','hero.flow.requirementsSub':'Architect · Report artifact hash',
  'hero.flow.coding':'Multi-model collaborative coding','hero.flow.codingSub':'Claude / Codex / Qwen · Net diff validation',
  'hero.flow.testing':'Model-independent verification','hero.flow.testingSub':'Deterministic argv · Exit-code evidence',
  'hero.flow.review':'Independent review & approval gate','hero.flow.reviewSub':'JSON verdict + hash-locked release',
  'card.evidence.title':'Run activity & immutable evidence','card.evidence.body':'Track stage transitions, test exit codes, and rework loops; preserve SHA-256 input/output fingerprints and red/green diffs for every attempt.',
  'card.sandbox.title':'Container isolation & controlled deployment','card.sandbox.body':'Support atomic write-back from Grok / Doubao Docker sandboxes within declared allowlists, with acceptance checks and human approval required before release.',
  'card.roundtable.title':'Real-time multi-model roundtable','card.roundtable.body':'Mix compatible APIs and read-only CLI seats for cross-examination across rounds, with independent analysis budgets and verified recommendations before task injection.',
  'card.telemetry.title':'Multi-dimensional budgets & dual-model arena','card.telemetry.body':'Track token, duration, and call limits in real time; compare models on the same task in isolated Git worktrees and adopt the winning candidate with one click.',
  'room.pause':'Ⅱ Pause','room.resume':'▷ Resume','room.cancel':'Stop execution','room.export':'Export report','room.archive':'Archive','room.derive':'Create follow-up task ↗',
  'room.stepperAria':'Stage progress','room.scope':'Workflow notes & execution scope','room.description':'Full task description',
  'room.tabAll':'Run activity','room.tabChat':'Messages & roundtables','room.discuss':'Start roundtable','room.evidence':'Execution evidence',
  'room.feedAria':'Run activity & messages','room.older':'View earlier activity','room.latest':'View latest activity ↓','room.unarchive':'Restore from archive',
 'feed.newCount':'{count} new events · View latest ↓','feed.backLatest':'Back to latest ↓','feed.loadingEarlier':'Loading earlier activity…','feed.earliest':'Earliest activity reached','feed.latestFailed':'Could not load latest activity: {error}',
  'composer.label':'Comment','composer.hint':'Saving a comment does not call a model or change the run','composer.placeholder':'Record thoughts or add context about the current artifact…',
  'composer.aria':'Enter a comment','composer.actionHint':'Need to execute an action? Use an explicit button above.','composer.save':'Save comment ↑',
  'evidence.title':'Execution evidence','evidence.close':'Collapse execution evidence','usage.title':'Budget & usage','usage.progressAria':'Token budget usage',
  'usage.reported':'Reported','usage.reserved':'Reserved','usage.modelCalls':'Model calls','usage.unknownCalls':'Unknown usage','usage.duration':'Elapsed time',
  'artifacts.title':'Artifact archive','threads.title':'Roundtable topics',
  'insights.title':'Usage insights & self-optimization','insights.usage':'Usage comparison & failures','insights.lessons':'Failure lessons',
  'insights.routing':'Model routing recommendations','insights.arena':'Dual-model arena',
  'insights.usageNote':'Reported usage and reserved budget are not invoice amounts. Deltas do not prove implementation quality; failure statistics accumulate across scanned pages.',
  'insights.baseline':'Baseline run','insights.comparison':'Comparison run','insights.compare':'Compare loaded runs','insights.nextFailures':'Scan next failure page',
  'insights.lessonsNote':'Read-only scanning of project review and test failures extracts recurring defect checklists; they are injected into future task prompts only after human approval.',
  'insights.project':'Select project','insights.minOccurrences':'Minimum occurrences','insights.scanLessons':'Scan recurring failure lessons',
  'insights.approveLessons':'Approve current proposal','insights.routingNote':'Aggregates real model performance by stage type from immutable terminal metrics.json. Small samples are marked insufficient_data; unknown usage is never treated as free.',
  'insights.minSamples':'Minimum samples','insights.scanRoute':'Generate model routing recommendations',
  'insights.arenaNote':'Run different models on the same coding task in two isolated Git worktrees, compare diffs and deterministic test results, then manually adopt an eligible candidate.',
  'insights.gitProject':'Select Git project','insights.testPreset':'Acceptance test (required to adopt)','insights.noTests':'No tests; compare diffs only','insights.candidateA':'Candidate A tool & model','insights.candidateB':'Candidate B tool & model',
  'insights.antigravity':'Antigravity CLI','insights.modelId':'Model ID','insights.allowedFiles':'Allowed files (one relative path per line; blank uses the project allowlist)',
  'insights.arenaTask':'Arena coding task','insights.arenaTaskPlaceholder':'For example: fix the retry backoff boundary in src/utils/retry.py and add unit tests…',
  'insights.arenaGuard':'Offline demos, fixed-test workflows, and analysis workflows do not launch real local CLIs; select a code-development project.',
  'insights.arenaRun':'⚔ Start dual-model comparison','insights.arenaHistory':'Arena history','insights.noArenaHistory':'No arena records yet','insights.cancel':'Cancel run',
  'project.eyebrow':'DYNAMIC WORKFLOW BUILDER','project.title':'Dynamic multi-node project configuration',
  'project.note':'Add, remove, and reorder 1–12 model or deterministic test nodes. Each model node independently selects an available local tool and model.',
  'project.registered':'Registered projects (select to edit or safely remove registration)','project.name':'Project ID (letters/numbers)','project.namePlaceholder':'For example: my-multi-agent',
  'project.preset':'Initial template','project.preset.reviewPlanFix':'Review → plan approval → code fix → final review (4 nodes)',
  'project.preset.multiAgent':'Lead → design → code → cross-review → final review (5 nodes)',
  'project.preset.standardDev':'Design (approval) → implementation → independent review (3 nodes)','project.preset.reviewOnly':'Read-only code review (1 node)',
  'project.workdir':'Local project directory','project.workdirPlaceholder':'For example: D:\\work\\my-project','project.nodes':'Dynamic pipeline nodes (execution order)',
  'project.addNode':'＋ Add node','project.refreshModels':'↻ Refresh local models','project.proxy':'Local proxy URL (optional for CLI; no credentials)',
  'project.proxyPlaceholder':'Blank for direct connection, e.g. http://127.0.0.1:7897','project.budget':'Total duration (seconds) / model call limit',
  'project.maxDuration':'Maximum duration in seconds','project.maxCalls':'Maximum model calls','project.patchProbe':'Allow Antigravity CLI 1.5.x patch versions through protocol probe adaptation',
  'project.cancel':'Cancel','project.save':'Save & hot-load project',
  'task.project':'Project configuration','task.template':'Task template','task.noTemplate':'No template','task.templateName':'Template name for reuse','task.selectProject':'Select a project configuration','task.configUnavailableSuffix':' · Configuration unavailable','task.projectsRefreshFailed':'Could not refresh project configurations: {error}',
  'task.saveTemplate':'Save as template','task.description':'Task & acceptance criteria','task.descriptionPlaceholder':'Describe what to complete, which files to change, and how completion will be verified…',
  'task.modelSettings':'Models & reasoning for this run','task.modelNote':'Select an available model detected locally, apply a historical route recommendation, or add and remove nodes.',
  'task.route':'✨ Use historical recommendation','task.editNodes':'＋ Add / arrange nodes','task.refreshModels':'↻ Refresh local models',
  'task.unlimited':'Do not limit tokens for this run (duration and round limits remain)','task.providerNote':'No local output token limit is sent. Providers may still impose defaults. Usage continues to be recorded and billed by the provider.',
  'task.note':'The task uses the registered working directory, model budget, and file scope; it will actually execute after launch.','task.cancel':'Cancel','task.submit':'Create & start',
  'discussion.title':'Direct multi-model roundtable','discussion.note':'Mix compatible APIs with read-only Antigravity CLI (strict plan+sandbox) seats for cross-examination. Add guidance in the room composer during a discussion; it is injected in the next round.',
  'discussion.question':'Topic (use @relative/path to attach workspace source, e.g. @package.json)','discussion.questionPlaceholder':'For example: discuss whether MQTT reconnection has a race condition in @App.vue and propose a minimal fix.',
  'discussion.seats':'Seats, backend tools & models (select 1–4 roles)','discussion.architect':'Architect (architect)','discussion.developer':'Developer (developer)',
  'discussion.skeptic':'Red-team skeptic (skeptic)','discussion.reviewer':'Reviewer (reviewer)','discussion.autoBackend':'Automatic backend','discussion.compat':'Compatible API',
  'discussion.readonly':'Antigravity CLI (read-only)','discussion.architectBackend':'Architect backend','discussion.developerBackend':'Developer backend',
  'discussion.skepticBackend':'Red-team backend','discussion.reviewerBackend':'Reviewer backend','discussion.defaultModel':'Use configured model',
  'discussion.skepticModel':'Prefer a different model for challenge','discussion.rounds':'Cross-examination rounds','discussion.round1':'1 round (quick individual responses)',
  'discussion.round2':'2 rounds (cross-examination · recommended)','discussion.round3':'3 rounds (deep convergence)','discussion.budget':'Roundtable token budget',
  'discussion.files':'Additional read-only workspace files (optional, comma-separated, up to 4)','discussion.filesPlaceholder':'For example: package.json, vite.config.js',
  'discussion.consensus':'Automatically generate a structured roundtable consensus summary','discussion.note2':'Active tasks must be paused first. Questions about finished tasks use a separate analysis budget and do not rewrite original metrics.',
  'discussion.cancel':'Cancel','discussion.submit':'Start roundtable','discussion.selectRole':'Select at least one discussion role','discussion.started':'Started a {rounds}-round roundtable with {seats} seats.',
  'artifact.defaultTitle':'Artifact','artifact.copy':'Copy all','artifact.close':'Close',
  'confirm.noteLabel':'Approval note / revision instructions (optional; downstream stages prioritize them)','confirm.notePlaceholder':'For example: ignore the first dependency downgrade and apply only fixes 2 and 3…',
  'confirm.back':'Back','confirm.submit':'Confirm action',
  'cmd.placeholder':'Type a command or search task names / IDs… (↑↓ select, Enter run)','cmd.aria':'Global command search','cmd.listAria':'Available commands & tasks',
  'cmd.move':'Move highlight','cmd.execute':'Execute now','cmd.close':'Close palette',
  'common.close':'Close','common.cancel':'Cancel','common.customDefault':'Custom / tool default','common.configDefault':'Use configuration / default','error.request':'Request failed ({status})',
  'error.network':'Unable to reach the local workbench. Check that the service is running, then retry.','error.response':'The local workbench returned an unreadable response. Please retry.','action.retry':'Retry',
  'status.connected':'Local service connected','status.live':'Live activity connected','status.reconnecting':'Reconnecting live activity…','status.listFailed':'Task list refresh failed','status.offline':'Local workbench unreachable','status.connecting':'Connecting',
  'list.refreshFailed':'Could not refresh the task list: {error}',
  'list.loading':'Loading tasks…','list.loadLater':'Load later tasks','list.loadOlder':'Load older tasks','list.loadOlderFailed':'Could not load older tasks; retry: {error}','list.expandDay':'Expand tasks for this date','list.collapseDay':'Collapse tasks for this date',
  'list.queryChanged':'Query changed','list.invalidPage':'Invalid task pagination response; retry','list.stalled':'Task pagination did not advance; retry','list.unregisteredProject':'Unregistered project',
  'list.localProject':'Local project','list.timeUnknown':'Time unknown','list.today':'Today · {date}','list.yesterday':'Yesterday · {date}','list.current':'current',
  'list.loadedCount':'Number of matching tasks currently loaded','list.empty':'No matching tasks','list.keyword':'Keyword: {value}','list.projectFilter':'Project filter',
  'scope.fixed_tests':'Fixed-test workflow','scope.simulation':'Workflow with simulated executors','scope.development':'Code-development workflow','scope.analysis':'Analysis & verification workflow',
  'scope.fixed_tests.summary':'Runs only the fixed test commands in the configuration; no models or development stages are invoked. Passing proves only that those commands passed.',
  'scope.simulation.summary':'Contains simulated executors. Simulated stages are for demos and cannot serve as real business-development delivery.',
  'scope.development.summary':'Invokes development executors from the configuration; delivery scope is established by code diffs, real tests, and review evidence.',
  'scope.analysis.summary':'This configuration has no code-development stage; passing reports or tests does not mean business implementation was changed.',
  'scope.workdir':'Actual working directory: {path}.','scope.stages':'Stages: {stages}.','scope.taskPaths':'Paths in the task description do not automatically change the working directory or add stages.',
  'scope.kind.code':'development','scope.kind.report':'report','scope.kind.review':'review','scope.kind.test':'fixed test',
  'scope.mismatch':'Cannot verify that the current configuration matches the original run. The registered scope below does not establish what the old run delivered: {scope}',
  'scope.unavailable':'Configuration unavailable: {error}. Repair or re-register it and refresh the workbench. Unavailable configurations cannot create tasks.',
  'scope.missing':'Execution scope for this configuration is not available. Refresh the workbench before creating a task.','scope.select':'Select a project configuration and verify its directory and stages before starting.',
  'scope.derived':'The follow-up task uses an independent worktree based on the current fixed commit, not the parent working directory, and does not rewrite parent artifacts. It uses a new budget. Stages: {stages}.',
  'wait.recovery':'Recovery condition: {value}','wait.resume':'After recovery: {value}','wait.nextCheck':'Next allowed check: {value}',
  'snapshot.breadcrumb':'{project} / Task room','snapshot.fixedPassed':'Fixed tests passed','snapshot.stagesDone':'{done} / {total} stages completed','snapshot.synced':'Synced {time}',
  'notice.configChanged':'The configuration bound to this run has changed. Do not approve or resume the old task with the new configuration.',
  'notice.paused':'Paused at a checkpoint. The original task validity period and budget remain unchanged.','notice.approval':'Review the specific artifact before approving. Chat comments do not replace approval.',
  'notice.history':'Historical run imported read-only; this page will not take over its process or rewrite historical conclusions.','notice.cancelled':'Execution stopped. Existing commits and incurred usage remain; cancellation does not undo changes.',
  'scope.criticalChanged':'Run configuration changed','scope.criticalApproval':'Artifact awaiting approval','scope.historyBinding':'Historical configuration binding unverified','scope.execution':'Execution scope','scope.attention':'Attention · ','scope.workflow':'Workflow notes · ',
  'stage.attempt':'Attempt {attempt} · Feedback rounds {rounds}','stage.approve':'Review & approve this artifact','stage.diff':'View code diff ↗','stage.rerun':'Rerun from this stage',
  'stage.approveTitle':'Approve current artifact','stage.approveBody':'Approve attempt {attempt} of {stage}. The backend will revalidate input and artifact hashes.',
  'stage.rerunTitle':'Rerun from stage','stage.rerunBody':'This invalidates the stage and downstream artifacts. Costs, total rounds, and validity period are not reset.',
  'stage.error.auth':'Authentication & credentials','stage.error.authHint':'Check the adapter environment variables (for example DEEPSEEK_API_KEY / DASHSCOPE_API_KEY) or local CLI login status.',
  'stage.error.version':'Adapter version / compatibility gate','stage.error.versionHint':'Run python -m orchestrator.cli doctor to check tool versions, or verify the adapter type and version allowlist in the configuration.',
  'stage.error.timeout':'Execution or response timeout','stage.error.timeoutHint':'Increase the stage timeout_s, shorten the input context, or reduce reasoning_effort.',
  'stage.error.budget':'Quota / rate limit / budget cap','stage.error.budgetHint':'Check provider rate limits or increase max_tokens / max_duration_s in [run_budget].',
  'stage.error.protocol':'Structured output validation failed','stage.error.protocolHint':'Enable structured review JSON or adjust reviewer parameters so the output is a valid JSON object.',
  'stage.error.zeroDiff':'Zero net change blocked','stage.error.zeroDiffHint':'The coding stage made no actual changes to allowlisted files; add explicit file-change goals and retry.',
  'stage.error.generic':'Stage execution error','stage.error.genericHint':'Expand execution evidence or inspect attempt logs, fix the configuration, then select Rerun from this stage.',
  'stepper.approve':'Approve & continue','stepper.locate':'Locate this stage in execution evidence','stepper.reviewMark':'R',
  'usage.deadline':'Task valid until {time}','usage.deadlineUnknown':'Run validity period not recorded','usage.artifacts':'{count} items','usage.noArtifacts':'No archived artifacts yet',
  'thread.budget':'Limit {tokens} tokens','thread.counted':'{tokens} tokens counted as analysis','thread.empty':'No role roundtables started',
  'copy.default':'Copied to clipboard','copy.failed':'Copy failed; select and copy manually','copy.unsupported':'Automatic copy is not supported here','copy.code':'Code copied','copy.message':'Message copied','copy.artifact':'Artifact copied',
  'role.humanAvatar':'Y','role.human':'Human comment','role.toolAvatar':'T','role.tool':'Execution record','role.consensusAvatar':'C','role.consensus':'Consensus conclusion',
  'role.skepticAvatar':'S','role.skeptic':'Red-team challenge','role.architectAvatar':'A','role.architect':'Architect','role.reviewAvatar':'R','role.review':'Review role',
  'role.planAvatar':'P','role.plan':'Planning role','role.codeAvatar':'D','role.code':'Implementation role','role.agentResponse':'Role response','role.systemSummary':'System summary',
  'feed.emptyChat':'No messages or roundtables','feed.emptyAll':'Waiting for the first persisted activity…','feed.streamDone':'Streaming roundtable complete','feed.streaming':'Live streaming output',
  'feed.thinking':'Thinking and generating recommendations…','feed.preview':'Live roundtable preview · persisted archive is authoritative','feed.cli':'CLI activity · {tool}','feed.cliDefault':'Processing model response',
 'cli.running':'Running','cli.stopped':'Stopped; waiting for archive','cli.exited':'Process exited; waiting for stage confirmation','cli.waitingOutput':'Waiting for model output','cli.reported':'{tokens} tokens reported this call','cli.settling':'awaiting settlement','cli.lowerBound':'response lower bound only','cli.cache':'cache read {tokens}','cli.quiet':'No new output for {seconds}s. Activity does not mean the task is complete.',
  'feed.cliRefreshed':'refreshed {count} times','feed.cliDetail':'{file}Reported lower bound {tokens} tokens; not a completion receipt','feed.realRecord':'Real execution record','feed.offlineExample':'Offline example',
  'feed.round':'Round {round}/{total}','feed.fullRecord':'View full record','feed.collapse':'Collapse content ⌃','feed.expand':'Expand full report ⌄',
  'feed.consensus':'Roundtable consensus · adopt for execution','feed.suggestion':'Roundtable recommendation · task conclusion unchanged','feed.adoptNote':'Adopt as approval note ↗',
  'feed.newTask':'Create task from conclusion ↗','feed.adoptTaskPrefix':'Execute based on multi-model roundtable consensus:\n','feed.viewEvidence':'View execution evidence ↗','feed.copy':'Copy',
  'event.exitCode':'Exit code {code}','event.queued':'Operation queued · {intent}','event.queuedDetail':'Waiting for executor acknowledgment; not yet effective.',
  'event.receipt':'Operation {status} · {intent}','event.receiptDetail':'{stage} · Attempt {attempt} · Input and artifact hashes verified',
  'event.snapshot':'Run status · {status}','event.discussionQueued':'Role roundtable queued','event.discussion':'Roundtable · {status}','event.created':'Task created',
  'command.queuedToast':'Command queued. It takes effect only after a receipt is received.','command.pauseNotice':'Pause request submitted; it will reach a checkpoint after the current call or test is archived.',
  'confirm.cancelTitle':'Stop current execution?','confirm.cancelBody':'This terminates the execution process for this task. Existing commits, incurred usage, and unknown reservations remain; applied changes are not undone.',
  'confirm.adoptTitle':'Approve current artifact (discussion conclusion attached)','confirm.adoptBody':'Approve attempt {attempt} of {stage} and inject the revision instructions below into downstream stages.',
  'artifact.valid':'Content hash verified.','artifact.changed':'Warning: file contents have changed and are no longer the original approval evidence.','artifact.missing':'Original artifact is missing.',
  'artifact.truncated':' Display truncated.','diff.tag':'Code artifact · SHA verified','diff.title':'Code diff · {stage}','diff.compare':'Compare {base} → {head}','diff.truncated':' · Display truncated','diff.empty':'No code diff relative to the selected baseline','diff.emptyTree':'empty tree',
  'task.newTitle':'New task','task.deriveTitle':'Create related task','task.newNote':'Execution is determined by the selected configuration. Verify the working directory and scope first.',
  'task.deriveNote':'Create an independent worktree from the current fixed commit. The original task, budget, approvals, and historical conclusions remain; the new task uses a new budget.',
  'task.startTests':'Start fixed tests','task.startDemo':'Start demo workflow','task.configPending':'Project configuration has not refreshed yet; try again shortly.',
  'task.configUnavailable':'Configuration unavailable: {error}. Repair or re-register it and refresh the workbench.','task.scopeUnknown':'Cannot confirm the execution scope; refresh and retry.',
  'task.conflictAction':'View task','task.readFailed':'Could not read task; retry: {error}','task.exported':'Delivery report exported: {filename}',
  'task.archived':'Task archived and hidden (disk evidence retained; find it under the Archived status filter)','task.restored':'Task restored to the main list',
  'demo.missing':'The kiroween-demo.example.toml demo template is not registered. Register it in project management and retry.',
  'demo.task':'[Kiroween five-stage closed-loop demo · offline script] Implement normalize(value): trim outer whitespace, split on whitespace, join with hyphens, and lowercase. Observe the first incorrect implementation fail real tests, then inspect the repair diff, script review, and human approval.',
  'cmd.newTask':'＋ New pipeline task','cmd.newTaskSub':'Create and start a new multi-stage task','cmd.demo':'⚡ Load five-stage demo template','cmd.demoSub':'Select an available configuration and fill the standard acceptance task',
  'cmd.builder':'⚙ Arrange a dynamic multi-node project','cmd.builderSub':'Visually add, remove, and reorder 1–12 model and deterministic test nodes',
  'cmd.arena':'⚔ Open usage insights & dual-model arena','cmd.arenaSub':'Compare run metrics, failure lessons, and isolated dual-model candidates',
  'cmd.theme':'🎨 Change interface theme (light / dark / Kiroween purple-gold)','cmd.themeSub':'Current theme: {theme}','cmd.discuss':'💬 Start multi-model roundtable','cmd.discussSub':'Cross-examine the current task with multiple roles',
  'cmd.export':'📥 Export current task Markdown report','cmd.exportSub':'Download complete execution evidence and usage report','cmd.taskId':'Task ID: {id}','cmd.empty':'No matching commands or tasks',
  'theme.light':'◐ Light','theme.dark':'☾ Dark','theme.kiroween':'🎃 Kiroween','help.toast':'Chat recommendations never override test or approval conclusions. Pause takes effect at checkpoints; cancellation does not undo commits. Local single-user only; writable scope and budgets come from registered configurations.',
  'budget.tokens':'Token allowance used {percent}% (including reservations)','budget.wallClock':'{seconds} seconds remain in the run deadline','budget.metered':'Metered duration allowance used {percent}%',
  'budget.hint':'Check current progress; this notice does not increase the budget.','budget.modelCalls':'Model call limit {count}','budget.unknownCalls':'Unknown usage limit {count}',
  'budget.defaultModel':'tool default','budget.reasoning':'reasoning {value}','budget.thinking':'thinking {value}','budget.jsonReview':'JSON review','budget.observedModels':'reported models {value}',
  'budget.observedEndpoints':'reported endpoints {value}','budget.endpoint':'endpoint {value}','budget.unlimited':'Tokens are unlimited for this run, but tool, duration, and round limits still apply.',
  'budget.credits':'Qoder reported {value} credits; token counts are incomplete and must not be treated as free usage.',
  'models.responsible':'Responsible: {stages}','models.live':' · {count} models detected','models.available':'Select an available system model ({count} total)','models.id':'Model ID (manual entry allowed)',
  'models.default':'Blank uses the signed-in tool default','models.reasoning':'Reasoning effort','models.thinking':'Deep thinking','models.json':'Structured review JSON','models.enabled':'Enabled','models.disabled':'Disabled','models.refreshFailed':'Could not refresh the local model catalog: {error}',
  'templates.unavailable':' · Configuration changed / unavailable','templates.loadFailed':'Could not read task templates: {error}','templates.stale':'Template configuration changed; refresh and verify the execution scope.',
  'templates.refresh':'Refresh and select an available project configuration.','templates.saved':'Template saved; verify the scope before starting it.',
  'compare.configSame':'Configuration: identical','compare.configDifferent':'Configuration: different or unrecorded; quality cannot be inferred directly',
  'compare.tokens':'Budgeted tokens','compare.reported':'Reported tokens','compare.reserved':'Reserved tokens','compare.duration':'Metered seconds','compare.delta':'{label} delta (comparison − baseline): {value}',
  'compare.unknown':'Unknown','compare.result':'Baseline: {left}; comparison: {right}; actual invoice cost was not reported.',
  'insights.scanStalled':'Scan did not advance; retry','insights.failureSummary':'{total} failure records accumulated (categories may overlap): {counts}{errors}{more}',
  'insights.pageErrors':'; {count} unreadable metadata records on this page','insights.moreHistory':'; more history pages remain','insights.scanComplete':'; scanned to the current end','insights.rescan':'Rescan failure records',
  'lessons.selectProject':'Select an available project first','lessons.status':'Scanned {runs} historical runs: found {pending} recurring lessons awaiting approval; {approved} currently active.',
  'lessons.pending':'[Pending approval · {count} occurrences] {source}','lessons.approved':'[Active · {count} occurrences] {source}','lessons.empty':'No recurring failure lessons meet the threshold.',
  'lessons.approvedToast':'Approved {count} failure lessons; they will be injected into future task prompts automatically.',
  'route.selectProject':'Select an available project first','route.empty':'Scanned {count} terminal metrics; no usable model samples yet.','route.code':'Coding stage (code)','route.report':'Report / planning stage (report)','route.review':'Review stage (review)','route.card':'{kind} · {recommendation}',
  'route.recommended':'Recommended: {tool} / {model}','route.insufficient':'No automatic recommendation (insufficient samples)','route.status':'Status','route.tool':'Tool','route.model':'Model','route.samples':'Samples',
  'route.runPass':'Run pass rate','route.stagePass':'Stage pass rate','route.medianDuration':'Median duration','route.medianTokens':'Median tokens','route.unknownCalls':'Unknown calls',
  'route.candidate':'Candidate','route.top':'Recommended','route.smallSample':'Insufficient samples','route.applied':'Filled {count} nodes from historical data: {nodes}','route.noMatch':'Historical terminal samples are insufficient or do not match current node tools; nothing was filled automatically.',
  'route.failed':'Could not fetch historical recommendations: {error}',
  'editor.noProjects':'No registered projects yet.','editor.unavailable':'Unavailable: {error}','editor.load':'Load for editing','editor.remove':'Remove registration',
  'editor.removed':'Safely removed registration for project {name}; code directories and historical runs were not deleted.','editor.nodeName':'Node English name (e.g. lead / coding)',
  'editor.report':'Plan / report (report)','editor.code':'Implementation (code)','editor.reviewKind':'Independent review (review)','editor.testKind':'Deterministic test (test)',
  'editor.preset.pytest':'pytest (python -m pytest -q)','editor.preset.unittest':'unittest (python -m unittest discover -s tests)','editor.preset.npmTest':'npm test','editor.preset.npmBuild':'npm run build',
  'editor.up':'Move up','editor.down':'Move down','editor.delete':'Delete node','editor.command':'Allowlisted deterministic command (no arbitrary shell)',
  'editor.available':' (available{count})','editor.notFound':' (not detected)','editor.modelId':'Manual model ID (optional)','editor.approval':'Human approval required after completion',
  'editor.feedback':'On failure, send back to:','editor.noFeedback':'No automatic feedback','editor.prompt':'Custom prompt for this node (blank uses the role default)','editor.promptPlaceholder':'Supports {task}, {upstream}, and {feedback} placeholders',
  'editor.maxNodes':'At most 12 dynamic nodes are supported','editor.modelsRefreshed':'Local model catalog refreshed','editor.selectProject':'Select a project configuration above first','editor.noModels':'This project has no configurable model nodes',
  'editor.loadFailed':'Could not read project nodes: {error}','editor.saved':'Project {name} ({count} dynamic nodes) hot-loaded and ready for tasks.',
  'arena.queued':'Queued','arena.running':'Running','arena.testing':'Acceptance testing','arena.done':'Completed','arena.failed':'Failed','arena.cancelled':'Cancelled','arena.interrupted':'Interrupted',
  'arena.cancelling':'Cancelling','arena.baseline':'Baseline {sha}','arena.applied':' · Adopted candidate {label}','arena.candidate':'Candidate {label}','arena.candidateHead':'Candidate {label}','arena.statusLine':'{status} · {candidates} · Baseline {sha}{error}','arena.default':'default',
  'arena.notTested':'Not tested','arena.testPassed':'Tests passed','arena.testFailed':'Tests failed ({code})','arena.error':'Error: {error}','arena.changedFiles':'Changed files: {files}',
  'arena.noChanges':'No code changes','arena.truncated':'Diff preview truncated.','arena.apply':'Adopt candidate {label} (ff-only merge)','arena.appliedButton':'Candidate {label} adopted ✓',
  'arena.appliedToast':'Candidate {label} adopted; workspace safely fast-forwarded to {sha}.','arena.blocked':'Comparison only · {reason}',
  'arena.selectProject':'Select a Git project first','arena.enterTask':'Enter an arena coding task',
  'error.busy':'This project already has an unfinished task; pause, finish, or cancel it first.',
  'error.arenaGuard':'Offline demos, fixed-test workflows, and analysis workflows do not launch real local CLIs; select a code-development project.',
  'error.staleConfig':'The configuration changed. Refresh and verify the execution scope before retrying.',
  'error.arenaStaleConfig':'The project configuration changed while the arena job was queued. Refresh and retry.',
  'error.invalidTask':'Task must contain 1 to 64,000 characters.','error.invalidArenaTask':'Arena task description must contain 1 to 16,000 characters.',
  'error.arenaActiveWork':'This project still has an active task or roundtable. Stop it before merging an arena candidate.',
  'error.arenaIncomplete':'Only completed arena jobs can be adopted.','error.discussionStale':'The configuration bound to the roundtable changed; another configuration will not be used.',
  'error.arenaCandidateFailed':'Candidate execution failed','error.arenaNoChanges':'No eligible code changes','error.arenaNoScope':'No allowed files declared for this round',
  'error.arenaOutOfScope':'Changes exceed the allowed file scope','error.arenaNoTestPreset':'No acceptance test selected','error.arenaTestFailed':'Candidate did not pass acceptance tests'
 };
 const statusEn={pending:'Pending',running:'Running',recovering:'Recovering',paused:'Paused',waiting_approval:'Awaiting approval',waiting_external:'Waiting for external conditions',waiting_owner:'Waiting for owner action',done:'Completed',failed:'Failed',timeout:'Timed out',cancelled:'Cancelled',awaiting_approval:'Awaiting approval',stale:'Needs rerun',interrupted:'Interrupted',queued:'Queued',completed:'Roundtable complete',applied:'Applied',rejected:'Rejected',archived:'Archived',unknown:'Unknown'};
 const stageEn={requirements:'Requirements analysis',design:'Solution design',code_review:'Code review',repair_plan:'Repair plan',coding:'Implementation',testing:'Test verification','测试器':'Test runner','离线示例':'Offline example',review:'Independent review',final_review:'Final review',self_tests:'Regression tests',environment:'Environment check',acceptance:'Human acceptance',architect:'Architect',developer:'Developer',skeptic:'Red-team skeptic',reviewer:'Reviewer',consensus:'Roundtable consensus',lead:'Technical lead',cross_review:'Cross-review'};
 const adapterEn={openai_compat:'Compatible API',cli_codex:'Codex CLI',cli_qwen:'Qwen CLI',cli_claude:'Claude Code',cli_qoder:'Qoder CLI',cli_agy:'Antigravity CLI',cli_dsh:'DSH CLI',cli_grok:'Grok container',cli_doubao:'Doubao container',fake:'Offline simulation',command:'Deterministic command'};
 const eventEn={recovery_captured:'Recovery point saved',recovery_applied:'Verified candidate files restored',recovery_rejected:'Recovery point validation rejected',cli_retry_exhausted:'CLI retries exhausted',attempt_start:'Attempt started',attempt_done:'Artifact archived',attempt_fail:'Attempt failed',stage_deferred:'Stage waiting',deferred_resumed:'Resumed from verified receipt',deferred_recovered:'Waiting state restored from sealed record',deferred_rejected:'Waiting receipt validation rejected',model_call_started:'Waiting for model response',precheck:'Pre-execution check',worktree_error:'Could not create isolated test worktree',test_start:'Tests started',test_pass:'Real tests passed',test_fail:'Real tests failed',test_feedback:'Test feedback sent to coding',review_feedback:'Review feedback sent to coding',repair_cycle:'Entering repair cycle',review_pass:'Review JSON passed',review_exhausted:'Review did not pass',policy_approved:'Pre-authorization approved',approved:'Human approval applied',recovery_hint:'Preparing bounded retry',rejected:'Constraint validation failed',interrupted:'Unfinished attempt detected',control_receipt:'Command receipt',cancelled:'Run cancelled',discussion_reserved:'Roundtable budget reserved',discussion_settled:'Roundtable usage settled',inflight_reserved:'Interrupted in-flight reservation settled'};
 const arenaEn={queued:'Queued',running:'Running',testing:'Acceptance testing',done:'Completed',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted'};
 const scopeLabelsEn={fixed_tests:'Fixed-test workflow',simulation:'Workflow with simulated executors',development:'Code-development workflow',analysis:'Analysis & verification workflow'};
 const scopeSummariesEn={fixed_tests:'Runs only the fixed test commands in the configuration; no models or development stages are invoked. Passing proves only that those commands passed.',simulation:'Contains simulated executors. Simulated stages are for demos and cannot serve as real business-development delivery.',development:'Invokes development executors from the configuration; delivery scope is established by code diffs, real tests, and review evidence.',analysis:'This configuration has no code-development stage; passing reports or tests does not mean business implementation was changed.'};
 let language='zh-CN';
 const storage={get(){try{return typeof localStorage==='undefined'?null:localStorage.getItem('wb_lang');}catch{return null;}},set(value){try{if(typeof localStorage!=='undefined')localStorage.setItem('wb_lang',value);}catch{}}};
 const normalize=value=>String(value||'').toLowerCase().startsWith('en')?'en':'zh-CN';
 const format=(value,vars)=>String(value??'').replace(/\{(\w+)\}/g,(match,key)=>vars&&Object.prototype.hasOwnProperty.call(vars,key)?String(vars[key]):match);
 function t(key,fallback,vars){return format(language==='en'?(messages[key]??fallback??key):(fallback??key),vars);}
 function initial(){
  let query='';try{if(typeof location!=='undefined')query=new URLSearchParams(location.search).get('lang')||'';}catch{}
  return normalize(query||storage.get()||(typeof globalThis!=='undefined'&&globalThis.PIPELINE_LANG)||'zh-CN');
 }
 function attr(element,name,key){
  if(!key)return;
  const suffix=name==='aria-label'?'AriaLabel':name[0].toUpperCase()+name.slice(1);
  const original='i18nOriginal'+suffix,supplied='i18n'+suffix+'Original';
  if(element.dataset[original]===undefined)element.dataset[original]=element.dataset[supplied]??element.getAttribute?.(name)??'';
  const value=language==='en'?(messages[key]??element.dataset[original]):element.dataset[original];
  element.setAttribute?.(name,format(value,{}));
 }
 function apply(root){
  const doc=typeof document==='undefined'?null:document;if(!doc)return;
  const container=root||doc;
  for(const node of container.querySelectorAll?.('[data-i18n]')||[]){
   if(node.children?.length)continue;
   if(node.dataset.i18nOriginal===undefined)node.dataset.i18nOriginal=node.textContent||'';
   let vars={};try{vars=JSON.parse(node.dataset.i18nVars||'{}');}catch{}
   node.textContent=t(node.dataset.i18n,node.dataset.i18nOriginal,vars);
  }
  for(const [name,key] of [['placeholder','data-i18n-placeholder'],['aria-label','data-i18n-aria'],['title','data-i18n-title']]){
   for(const node of container.querySelectorAll?.('['+key+']')||[])attr(node,name,node.getAttribute(key));
  }
 }
 function documentChrome(){
  if(typeof document==='undefined')return;
  document.documentElement?.setAttribute?.('lang',language);
  const title=document.querySelector?.('title');
  if(title){if(title.dataset.i18nOriginal===undefined)title.dataset.i18nOriginal=title.textContent;title.dataset.i18n='doc.title';title.textContent=t('doc.title',title.dataset.i18nOriginal);}
  else document.title=t('doc.title','流水线 · 协作工作台');
  const button=document.getElementById?.('language-toggle');
  if(button){button.textContent='中文 / EN';button.setAttribute?.('aria-pressed',String(language==='en'));button.title=t('language.toggle','切换界面语言');button.setAttribute?.('aria-label',button.title);}
 }
 function setLanguage(value,persist=true){
  language=normalize(value);if(persist)storage.set(language);documentChrome();apply();
  if(typeof document!=='undefined'&&typeof document.dispatchEvent==='function'){
   const EventType=typeof CustomEvent==='function'?CustomEvent:typeof Event==='function'?Event:null;
   if(EventType)document.dispatchEvent(new EventType('workbench:languagechange',{detail:{language}}));
  }
  return language;
 }
 function mark(node,key,fallback,vars){
  if(!node)return node;node.dataset.i18n=key;node.dataset.i18nOriginal=fallback??key;node.dataset.i18nVars=JSON.stringify(vars||{});node.textContent=t(key,fallback,vars);return node;
 }
 function label(value,map,fallback){return language==='en'?(map[value]||fallback||value):(fallback||value);}
 function scopeLabel(scope){if(!scope)return '';if(language==='en')return scope.label_en||scopeLabelsEn[scope.mode]||scope.label||scope.mode;return scope.label||scope.mode||'';}
 function scopeSummary(scope){if(!scope)return '';if(language==='en')return scope.summary_en||scopeSummariesEn[scope.mode]||scope.summary||'';return scope.summary||'';}
 const errorZh={
  'error.network':'无法连接本地工作台，请确认服务已启动后重试。','error.response':'本地工作台返回了无法解析的响应，请重试。',
  'error.busy':'此项目已有未结束任务；请先暂停、完成或取消。','error.arenaGuard':'离线演练、固定测试或分析流程不会启动本机真实 CLI；请使用代码开发项目。',
  'error.arenaStaleConfig':'竞技场排队期间项目配置已变化，请刷新后重试。','error.staleConfig':'配置已变化，请刷新并核对执行范围后重试。',
  'error.invalidTask':'任务必须为 1 到 64000 字符。','error.invalidArenaTask':'竞技场任务描述须为 1..16000 字符。',
  'error.arenaActiveWork':'该项目尚有运行中的任务或会诊，请先停止再合并竞技场候选。','error.arenaIncomplete':'只能采纳已完成的竞技场任务。',
  'error.arenaCandidateFailed':'候选生成失败','error.arenaNoChanges':'候选没有可采纳的改动','error.arenaNoScope':'本轮未声明允许改动的文件',
  'error.arenaOutOfScope':'候选改动超出允许的文件范围','error.arenaNoTestPreset':'本轮未选择验收测试','error.arenaTestFailed':'候选未通过验收测试'
 };
 function error(value){
  const text=String(value||'');
  const rules=[
   [/无法连接本地工作台|Unable to reach the local workbench/i,'error.network'],
   [/本地工作台返回了无法解析的响应|returned an unreadable response/i,'error.response'],
   [/此项目已有未结束任务|project already has an unfinished task/i,'error.busy'],
   [/离线演练、固定测试或分析流程不会启动本机真实 CLI|offline demos, fixed-test workflows, and analysis workflows/i,'error.arenaGuard'],
   [/竞技场排队期间项目配置已变化/i,'error.arenaStaleConfig'],
   [/配置已变化|运行绑定的配置已变化|会诊绑定的配置已变化|configuration changed/i,'error.staleConfig'],
   [/任务必须为 1 到 64000 字符/i,'error.invalidTask'],[/竞技场任务描述须为 1\.\.16000 字符/i,'error.invalidArenaTask'],
   [/该项目尚有运行中的任务或会诊/i,'error.arenaActiveWork'],[/只能采纳已完成的竞技场任务/i,'error.arenaIncomplete'],
   [/^候选生成失败$/,'error.arenaCandidateFailed'],[/^候选没有可采纳的改动$/,'error.arenaNoChanges'],
   [/^本轮未声明允许改动的文件$/,'error.arenaNoScope'],[/^候选改动超出允许的文件范围$/,'error.arenaOutOfScope'],
   [/^本轮未选择验收测试$/,'error.arenaNoTestPreset'],[/^候选未通过验收测试$/,'error.arenaTestFailed']
  ];
  for(const [pattern,key] of rules)if(pattern.test(text))return language==='en'?messages[key]:errorZh[key];
  return text;
 }
 language=initial();
 globalThis.WorkbenchI18n={get language(){return language;},isEnglish:()=>language==='en',locale:()=>language==='en'?'en-US':'zh-CN',t,mark,apply,setLanguage,toggle:()=>setLanguage(language==='en'?'zh-CN':'en'),status:(value,fallback)=>label(value,statusEn,fallback),stage:(value,fallback)=>label(value,stageEn,fallback),adapter:(value,fallback)=>label(value,adapterEn,fallback),event:(value,fallback)=>label(value,eventEn,fallback),arena:(value,fallback)=>label(value,arenaEn,fallback),scopeLabel,scopeSummary,error};
 documentChrome();apply();
 const button=typeof document==='undefined'?null:document.getElementById?.('language-toggle');if(button)button.onclick=()=>globalThis.WorkbenchI18n.toggle();
})();
