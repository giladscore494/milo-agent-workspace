MILO

מפת דרכים חדשה — מנוע אחוד בשלושה שלבים

שלב 1: Swarm Core מלא • שלב 2: חיבור ל״ידע רכב״ • שלב 3: חיבור ל־data.gov.il

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>נקודת המוצא</p>
<p>Stage C סגור ומאומת. המטרה במסמך הזה אינה להוסיף “עוד מנועים”, אלא לבנות מנוע Agentic אחד שמתחבר בהדרגה לכלים. V1 נשאר Control יציב; כל החדש נבנה Side-by-Side, על אותם Run lifecycle, leases, checkpoints, budgets, ProviderScheduler ו־fail-closed boundaries שכבר הוכחו.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

גרסה 1.0 \| Scope: שלושת השלבים בלבד

בסיס קוד שנבדק: giladscore494/milo-agent-workspace @ main, סגירת Stage C ב־9c2cd642…; מאגר ידע רכב: reliabilityAIModelsR2/model_technical_catalog_il.json.

# 1. מטרת המסמך והחלטת הארכיטקטורה

המסמך מחליף את הפיצול הישן של Government Engine / Yeda Bridge / Vehicle Data Engine / Swarm Engine. המימוש החדש הוא MILO Unified Engine אחד. ה־Commander הוא המוח; Tool Registry הוא שכבת היכולות; ידע רכב ו־data.gov.il הם Tools, לא מנועים נפרדים.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th>Chat / Run<br />
│<br />
▼<br />
MILO Unified Swarm Engine<br />
├── Task Commander<br />
├── Dynamic Task Graph<br />
├── Generic Worker Pool<br />
├── Evidence / Claims / Conflicts<br />
├── Replanning + Verifier<br />
└── Deterministic Final Builder<br />
│<br />
▼<br />
Tool Registry<br />
├── Web/Kimi research<br />
├── YedaCatalogTool [שלב 2]<br />
└── GovernmentVehicleTool [שלב 3]</th>
</tr>
</thead>
<tbody>
</tbody>
</table>

| שלב            | מה נבנה                                                                                                     | מה לא נבנה                                                                            |
|---------------------------------------|------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| 1 — Swarm Core | מנוע מלא: Commander, משימות דינמיות, workers, evidence, replanning, verifier, final builder, Tool Registry. | אין עדיין תלות אמיתית בידע רכב או בממשלה; משתמשים ב־mock tools וב־Web research מבוקר. |
| 2 — ידע רכב    | Tool קריאה/השוואה/patch למאגר הקיים ב־GitHub, עם snapshot/index, provenance וכתיבה מבוקרת.                  | לא מעתיקים את כל מאגר ידע רכב לתוך prompt ולא נותנים ל־LLM לכתוב JSON ישירות.         |
| 3 — API ממשלתי | Tool שמסנכרן CKAN, שומר snapshots, מנרמל Manufacturer→Model→Year→Variant ומבצע diff מול ידע רכב.            | לא קוראים 100K שורות live בכל הודעת צ׳אט ולא משתמשים ב־AI לניקוי דטרמיניסטי.          |

# 2. נקודת הפתיחה — מה Stage C כבר הוכיח

Stage C הסתיים ב־PASSED על run אמיתי. לכן השכבות החדשות צריכות “להיכנס לתוך המסילה” שכבר עובדת, לא לעקוף אותה.

| עובדה מאומתת                                                                                                                                                                       | משמעות לתכנון החדש                                                                                                               |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|
| Attempt 7: run 8b4a4277… הגיע terminal completed; 84 provider calls; 312,018 tokens; 84 reservations settled; 0 dangling; 0 retries/backpressure; internal actual_cost \$0.252069. | אין צורך להמציא lifecycle חדש. מנוע V2 חייב לרוץ דרך אותו Worker, BudgetTracker, reservations, usage ledger ו־ProviderScheduler. |
| Idempotent replay החזיר את אותו Run ID ב־HTTP 202 בלי Run/Worker נוסף.                                                                                                             | לא משנים את API run creation או idempotency עבור V2. engine selection מתבצע רק אחרי שה־Run נוצר.                                 |
| בסיום: kill switch עבר; paid off; provider aliases absent; run creation off; launcher disabled; zero active executions.                                                            | ברירת המחדל של כל V2 נשארת fail-closed. כלי חדש לא מקבל secret דרך browser/run input.                                            |
| ProviderScheduler כבר קיים ומבדיל 429/backpressure מ־semantic retries.                                                                                                             | לא בונים scheduler חדש בתוך Swarm. כל Commander/worker/verifier call עובר באותו scheduler per-run.                               |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>Invariant מרכזי</p>
<p>כל durable write של worker חדש חייב להיות lease-guarded. אם worker איבד lease, הוא לא יכול לעדכן state, evidence, task graph, tool usage או output. זה הלקח החשוב ביותר לשילוב נקי עם Production.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

# 3. ממצאים בקוד הנוכחי שמשפיעים על האינטגרציה

- \`backend/worker/engine.py\` כבר מגדיר חוזה Engine קטן ונכון: \`workflow_key\` + \`run(run)\`. V2 צריך ליישם אותו, לא לשנות את חוזה ה־Run.

- \`VehicleCatalogV1Adapter\` כבר מקבל model-client, events, checkpoints, cancellation, retry/backpressure ו־provider limits בהזרקה. אותו pattern נשמר ב־SwarmV2Adapter.

- \`backend/worker/main.py\` בוחר כרגע \`VehicleCatalogV1Adapter\` כברירת מחדל. בנוסף, בדיקת latest checkpoint מתבצעת לפני בחירת המנוע ובמקרה הרגיל נופלת ל־\`vehicle_catalog_v1\`. זה blocker אמיתי ל־resume של V2 וצריך לתקן לפני Swarm.

- \`Project\` כבר מכיל \`workflow_key\`. לכן engine routing צריך להגיע מה־Project השייך ל־Conversation — מקור trusted server-side — ולא מ־metadata שהמשתמש יכול לשלוח.

- קיימות כבר טבלאות \`sources\`, \`claims\`, \`conflicts\`, \`tool_access_requests\`, \`tool_grants\`, \`tool_usage\`. אין סיבה לבנות Evidence DB נוסף.

- אבל write methods של tool/source/claim/conflict בריפו כרגע משתמשים ב־direct inserts ולא ב־lease-guarded RPC. אסור ל־V2 להשתמש בהן לכתיבות durable לפני hardening.

- \`backend/supervisor.py\` נשאר Shadow. יש בו scaffolding של active commands ו־templates קשיחים; לא “מדליקים” אותו. ה־Commander החדש יהיה בתוך \`swarm_v2\` ולא ישנה V1.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>כלל בידוד</p>
<p>אין refactor גדול של `vehicle_catalog_v1` כחלק מהבנייה. V1 הוא Control. כל חוזה שרוצים להכליל ממנו מועתק/מוגדר מחדש ב־V2, ורק אחרי שה־V2 יציב אפשר לשקול איחוד קוד משותף.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

# שלב 1 — בניית MILO Swarm Core המלא

יעד השלב: מנוע חדש \`swarm_v2\` שמקבל מטרה חופשית מהצ׳אט, יוצר taxonomy ו־task graph דינמיים, מפעיל generic workers, משתמש בכלים דרך Registry, אוסף evidence, מבצע replanning ו־verification ומחזיר תוצאה מובנית — תוך שימוש בכל שכבות Production שכבר עברו Stage C.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>Gate S1</p>
<p>השלב נחשב גמור רק אם אותה בקשת Run יכולה להיות מנותבת ל־`swarm_v2`, להיעצר/להתחדש מ־checkpoint, לעבוד עם mock tools ועם Kimi תחת budget/scheduler, ולסיים completed ללא שום שינוי בהתנהגות V1.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

### 1.1 — Freeze חוזי V1 ו־Stage C

מטרה: לקבוע גבול ברור: V1 נשאר control, וה־runtime שנבדק לא משתנה עקב refactor של V2.

מימוש טכני: להוסיף מסמך/בדיקות regression שמצמידים את \`vehicle_catalog_v1\` ל־workflow_key הקיים, את ProviderScheduler contract, ואת ה־worker lifecycle. אין העברת קבצים מ־V1 בשלב הזה.

מוקדי קוד:

- \`backend/engines/vehicle_catalog_v1/\*\*\` — read-only מבחינת feature work.

- \`tests/\` — fixture שמוודא ש־V1 עדיין נבחר לפרויקט עם workflow_key הישן.

בדיקות חובה:

- V1 unit/integration suite עוברת ללא שינוי output contract.

- בדיקה ש־new engine files לא משנים imports/module globals של V1.

Done כאשר: יש baseline ירוק שניתן להריץ לפני ואחרי כל PR של V2 ולהוכיח שאין regression.

### 1.2 — Engine Registry + trusted routing

מטרה: לאפשר ל־Worker לבחור מנוע לפי Project, בלי להכניס routing לא מאובטח לתוך run metadata.

מימוש טכני: להוסיף \`EngineRegistry\`/\`EngineResolver\`. ה־Worker קורא \`run.conversation_id → conversation.project_id → project.workflow_key\` דרך Repository. \`workflow_key\` ממופה ל־factory מאושר בלבד: \`vehicle_catalog_v1\`, \`swarm_v2\`, \`mock\`. אין dynamic import משם שהמשתמש מספק.

מוקדי קוד:

- \`backend/worker/engine.py\` — Protocol + Registry/Resolver.

- \`backend/worker/main.py\` — resolve workflow לפני checkpoint lookup ולפני engine instantiation.

- \`backend/repository/supabase.py\` — reuse get_conversation/get_project.

בדיקות חובה:

- workflow_key לא מוכר → fail closed \`ENGINE_NOT_ALLOWED\`.

- ניסיון להכניס \`workflow_key\` ב־Run metadata לא משפיע על routing.

- V1 project ממשיך לבחור V1; V2 project בוחר V2.

- checkpoint lookup מקבל את workflow_key הנכון.

Done כאשר: ה־Worker מסוגל לבחור V1/V2 בצורה trusted, וכל resume קורא checkpoint של אותו workflow בלבד.

### 1.3 — Package boundary חדש ל־Swarm V2

מטרה: ליצור שכבה חדשה ונקייה, בלי תלות סמויה ב־core.py של V1.

מימוש טכני: ליצור \`backend/engines/swarm_v2/\` עם adapters/contracts/state/commander/executor/verifier/builder. כל dependency חיצוני מוזרק דרך constructor. לא משתמשים ב־module globals כמו V1, כי workers דינמיים ירוצו במקביל.

מוקדי קוד:

- \`backend/engines/swarm_v2/\_\_init\_\_.py\`

- \`adapter.py\`, \`engine.py\`, \`contracts.py\`, \`state.py\`, \`commander.py\`, \`executor.py\`, \`verifier.py\`, \`builder.py\`

בדיקות חובה:

- Import smoke test.

- No import from \`vehicle_catalog_v1.core\` מלבד אולי constants מוכחים שאינם runtime; עדיף 0 תלות.

Done כאשר: יש package עצמאי שמיישם Engine Protocol וניתן להריץ עם fake dependencies בלבד.

### 1.4 — חוזי Task Graph דינמיים

מטרה: להגדיר מה Commander רשאי ליצור בלי לקבע “מנוע/גיר/מידות” בקוד.

מימוש טכני: להגדיר Pydantic contracts עם \`extra=forbid\`: \`DynamicTask\`, \`TaskGraph\`, \`ToolRequirement\`, \`EvidenceRequirement\`, \`CompletionCriteria\`, \`WorkerAssignment\`, \`CommanderPlan\`. כל task כולל goal, scope, dependencies, allowed tools, output schema, evidence minimum, priority, recursion depth ו־completion criteria.

מוקדי קוד:

- \`backend/engines/swarm_v2/contracts.py\`

בדיקות חובה:

- Cycle detection.

- Duplicate task signature rejection.

- Missing dependency rejection.

- Recursion/cost/task-count limits.

- Output schema חייב להיות JSON object מוגדר.

Done כאשר: ניתן לקבל 10 בקשות שונות ולייצר graph schema תקין בלי אף role רכב קשיח.

### 1.5 — Command Firewall / Graph Validator

מטרה: להפריד בין “ה־LLM הציע תוכנית” לבין “המערכת מרשה לבצע אותה”.

מימוש טכני: Commander לעולם לא מפעיל worker/tool ישירות. הוא מחזיר Plan JSON; validator דטרמיניסטי בודק allowed tools, dependency closure, max tasks, max depth, token/search budget, no duplicate work, tool scopes ו־completion criteria. רק plan מאושר נכנס ל־executor.

מוקדי קוד:

- \`swarm_v2/validation.py\` או \`contracts.py\`

- \`backend/production_config.py\` — config safety בלבד אם נוספו env vars.

בדיקות חובה:

- Prompt injection שמבקש tool לא רשום נדחה.

- Graph עם cycle/1000 tasks/recursion מוגזם נדחה.

- אין יצירת task אם budget reservation cannot fit.

Done כאשר: אין נתיב שבו output לא מאומת של Commander הופך לפעולה.

### 1.6 — Commander model resolver

מטרה: להשתמש במודל Kimi החזק ביותר שבפועל זמין/מאושר, בלי לקבע שם מודל עתידי בקוד.

מימוש טכני: להגדיר \`MILO_COMMANDER_MODEL\` ו־allowlist. מצב \`auto_best_available\` פותר מתוך allowlist/config שנבדק בזמן deployment. Worker model מוגדר בנפרד. אם model אינו זמין או אינו מותר — fail closed/fallback מוגדר מראש, לא החלפה אקראית.

מוקדי קוד:

- \`swarm_v2/models.py\`

- \`backend/production_config.py\` — verify allowlist/config.

בדיקות חובה:

- Unknown model blocked.

- Commander model priority \> worker model when config מאפשר.

- No secret/model inventory exposed to browser.

Done כאשר: Commander model selection היא config change ולא rewrite של engine.

### 1.7 — Provider facade משותף לכל הסוכנים

מטרה: להבטיח שכל model call — Commander, worker, verifier, follow-up — עובר באותו BudgetTracker ו־ProviderScheduler של ה־Run.

מימוש טכני: לבנות \`ModelGateway\`/\`KimiGateway\` שמקבל \`build_guarded_client_factory(tracker)\` ו־\`ProviderScheduler\` כ־dependencies. כל call מבצע: cancellation check → agent step accounting → budget reservation → scheduler admission → provider → settlement/usage. אין OpenAI client ישיר בשום worker class.

מוקדי קוד:

- \`backend/provider_scheduler.py\` — reuse, לא fork.

- \`backend/budget.py\` — reuse guarded client.

- \`swarm_v2/model_gateway.py\`.

בדיקות חובה:

- כל provider call מופיע ב־ledger/reservation.

- 429 לא צורך semantic retry.

- Cancellation בזמן queue wait עוצרת call.

- Concurrency של logical workers לא עוקפת provider limit.

Done כאשר: אפשר להריץ 20 logical tasks בפייק ובדיקות ולהוכיח שכל provider request עבר דרך guard יחיד.

### 1.8 — Generic Worker Pool

מטרה: להריץ תתי־משימות דינמיות בלי ליצור class נפרד לכל נושא.

מימוש טכני: \`GenericWorker\` מקבל TaskSpec + compact context + ToolRegistry. Executor מנהל ready queue לפי dependencies/priority. מתחילים ב־\`MILO_SWARM_MAX_ACTIVE_WORKERS\` שמרני (למשל 4–8), נפרד מ־Tier2 provider ceiling. ניתן להעלות לפי telemetry בלי לשנות Commander.

מוקדי קוד:

- \`swarm_v2/worker.py\`, \`executor.py\`

בדיקות חובה:

- Dependency ordering.

- Max active workers enforced.

- Worker failure לא מפיל tasks לא תלויים.

- אין fake parallelism: כל task חייב scope/completion ייחודיים.

Done כאשר: Task graph יכול להתבצע במקביל באופן bounded ודטרמיניסטי מבחינת dependencies.

### 1.9 — Tool Registry + Mock Tools

מטרה: לבנות מנגנון שבו כל capability היא Tool, כדי שבשלבים 2–3 נוסיף ידע רכב וממשלה בלי לשנות את המנוע.

מימוש טכני: להגדיר \`Tool\` protocol: name, description, read/write mode, input_schema, output_schema, required_scope, execute(context,payload). Registry הוא allowlist. בשלב 1 להוסיף \`MockSearchTool\`, \`MockCatalogTool\`, \`MockStructuredDataTool\` לבדיקות בלבד.

מוקדי קוד:

- \`backend/tools/contracts.py\`

- \`backend/tools/registry.py\`

- \`backend/tools/mock.py\`

בדיקות חובה:

- Tool לא רשום blocked.

- Input/output schema validation.

- Write tool דורש capability/approval נפרד.

- Tool exception הופך ל־structured task failure ולא raw stack.

Done כאשר: ה־Commander יכול לבחור tool מתוך registry ולסיים משימה end-to-end עם mocks.

### 1.10 — Lease-guard hardening ל־Evidence/Tool writes

מטרה: לאפשר ל־V2 לשמור evidence בצורה durable בלי לתת ל־worker stale לעקוף את invariant של Stage C.

מימוש טכני: לפני שימוש בטבלאות הקיימות, להוסיף guarded RPCs ל־tool_usage/source/claim/conflict (ול־access/grant אם worker יוצר אותם). לעדכן Repository signatures ל־worker_id/attempt/lease_token. יצירת claim + source_claim_link צריכה להיות atomic כדי שלא ייווצר claim בלי link.

מוקדי קוד:

- \`supabase/migrations/\<new\>\_swarm_guarded_evidence_writes.sql\`

- \`backend/repository/supabase.py\`

- \`tests/test_worker_rpc_acl_postgres.py\`

בדיקות חובה:

- \`set role service_role\` lifecycle מלא.

- Stale lease → 0 writes לכל טבלת evidence/tool.

- anon/authenticated blocked.

- Migration rerun-safe.

- Atomic claim+link rollback on failure.

Done כאשר: כל durable write של V2 כפוף לאותו lease token כמו events/checkpoints/usage.

### 1.11 — Evidence Board על המודלים שכבר קיימים

מטרה: לא ליצור מערכת provenance חדשה במקביל ל־\`sources/claims/conflicts\`.

מימוש טכני: להשתמש ב־Source/Claim/Conflict הקיימים וב־run_blackboard. Worker result הופך ל־claims עם scope (entity, field, market, time), source strength, confidence. Conflict detector מסמן ערכים שונים באותו scope. Blackboard מחזיק summaries בלבד; full evidence נשאר בטבלאות.

מוקדי קוד:

- \`backend/internet_governance.py\` — reuse/extend conservatively.

- \`swarm_v2/evidence.py\`

- \`backend/repository/supabase.py\` guarded methods.

בדיקות חובה:

- Claim בלי Source נדחה.

- Conflict על scopes שונים לא נפתח.

- אותו evidence אינו מוכפל בריצה מחדש/resume.

- No chain-of-thought stored; רק structured evidence + rationale summary.

Done כאשר: אפשר לעקוב מכל final field חזרה ל־claim/source/run/task.

### 1.12 — Commander replanning loop

מטרה: לאפשר למפקד להסתכל על תוצאות ולא רק לתכנן פעם אחת.

מימוש טכני: לאחר wave של tasks, engine בונה compact status: completed, failed, missing evidence, conflicts, remaining budget. Commander רשאי להחזיר \`ADD_TASKS\`, \`REVISE_TASK\`, \`REQUEST_VERIFICATION\`, \`FINISH\`. כל replan עובר שוב validator. max replans/depth/tasks קשיחים בקוד/config.

מוקדי קוד:

- \`swarm_v2/commander.py\`, \`engine.py\`, \`state.py\`

בדיקות חובה:

- Follow-up נוצר רק על gap/conflict אמיתי.

- Infinite planning loop נעצר.

- Replan respects remaining budget.

- Commander לא מקבל raw search traces; רק summaries/evidence anchors.

Done כאשר: מנוע פותר gap חדש בלי hardcoded flow ויודע לעצור כאשר completion contract הושג.

### 1.13 — Verifier + Deterministic Final Builder

מטרה: לשמור את העיקרון של V1: LLM חוקר/מאמת; קוד דטרמיניסטי מרכיב final truth/result.

מימוש טכני: Verifier מקבל claims compactים ומחזיר verdicts structured: verified/needs_review/rejected + reasons. Builder בוחר רק claims שעוברים policy ומרכיב \`SwarmRunResult\`. במשימה כללית הוא מחזיר structured result; במשימת catalog בשלבים 2–3 הוא יפיק PatchProposal ולא ישנה מקור ישירות.

מוקדי קוד:

- \`swarm_v2/verifier.py\`, \`builder.py\`

בדיקות חובה:

- Unsupported claim לא נכנס final.

- Conflict unresolved → needs_review.

- Builder output deterministic עבור אותו evidence set.

- No direct LLM write to GitHub/DB canonical.

Done כאשר: final result ניתן לשחזור מ־checkpoint/evidence בלי “לזכור” reasoning של המודל.

### 1.14 — Checkpoint / Resume אמיתי ל־Swarm

מטרה: לאפשר restart של Worker בלי להתחיל את הנחיל מחדש.

מימוש טכני: Checkpoint schema של V2 שומר engine_version, workflow_key, graph revision, completed task IDs, task outputs summaries, evidence references, replans, verifier state, token/usage snapshots. Resume טוען רק checkpoint תואם engine/workflow/version; tasks completed אינם מורצים שוב.

מוקדי קוד:

- \`swarm_v2/state.py\`, \`engine.py\`

- \`backend/worker/main.py\` — checkpoint routing fix מה־workflow resolver.

בדיקות חובה:

- Restart באמצע wave → completed tasks לא חוזרים.

- Checkpoint של V1 לא נטען ב־V2 ולהפך.

- Stale/incompatible version → controlled restart/refusal לפי policy.

- Resume לא מכפיל evidence/tool usage.

Done כאשר: kill/restart יכול להמשיך Run V2 באופן עקבי.

### 1.15 — Events, observability ו־acceptance של שלב 1

מטרה: לתת visibility כמו Stage C בלי לבנות dashboard חדש.

מימוש טכני: להשתמש ב־run_events הקיימים ולהוסיף event vocabulary קטן: commander_plan_created, task_ready, task_started, task_completed, task_failed, tool_called, evidence_added, conflict_found, commander_replanned, verification_completed. לא לשמור secrets/raw provider payloads.

מוקדי קוד:

- \`swarm_v2/engine.py\`, \`backend/runtime.py\` אם צריך allowlist event types

- \`tests/\`

בדיקות חובה:

- Full offline suite.

- Mock E2E: 3–10 dynamic tasks, dependencies, tool calls, conflict, follow-up, verify, complete.

- Failure E2E: worker failure, cancellation, budget stop, 429 simulation, stale lease.

- V1 regression full suite.

Done כאשר: פרויקט עם workflow_key=swarm_v2 מסיים Run במצב completed במבחן end-to-end, וכל hard-stop נשמר.

## סדר PRים מומלץ — שלב 1

| PR     | Scope                                                          | Gate                            |
|-------------------------------|---------------------------------------------------------------------------------------|--------------------------------------------------------|
| S1-PR1 | Freeze + EngineRegistry + trusted routing + checkpoint-key fix | V1 green; V2 mock selectable    |
| S1-PR2 | swarm_v2 contracts + model resolver + command firewall         | Dynamic graph validates offline |
| S1-PR3 | ModelGateway + GenericWorkerPool + ToolRegistry mocks          | Parallel bounded mock execution |
| S1-PR4 | Lease-guard evidence migration + Evidence Board                | Stale worker cannot write       |
| S1-PR5 | Replanning + verifier + builder + resume/events                | Full Stage-1 E2E green          |

# שלב 2 — חיבור המנוע ל״ידע רכב״

יעד השלב: להפוך את המאגר הקיים לכלי native של המנוע. ה־Commander יוכל לקרוא דגמים/שנתונים/וריאנטים, להבין coverage, להשוות, ליצור patch proposal ולבצע write מבוקר — בלי להזרים את קובץ ה־JSON המלא למודל.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>מקור שנבדק</p>
<p>ב־reliabilityAIModelsR2 המאגר הטכני החי נמצא ב־`my-flask-app/app/data/model_technical_catalog_il.json` (כ־7.3MB). ה־service הקיים טוען root `models`, ובכל model יש make/model/canonical_model/year range, sources/notes ו־`technical_variants_il`. ה־service גם מייצר variant_id דטרמיניסטי ומסיר duplicates.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

### 2.1 — Lock Yeda Catalog Contract

מטרה: לקבע את הסכמה האמיתית של הקובץ לפני כל adapter.

מימוש טכני: לקרוא את \`vehicle_catalog_service.py\` ואת קובץ ה־JSON ב־commit pinned. להוציא Pydantic read models עבור model/variant/source. לא “לנחש” fields. לשמור \`YEDA_CATALOG_SCHEMA_VERSION\` ו־fixture קטן של 3–5 models/variants.

מוקדי קוד:

- Yeda repo: \`my-flask-app/app/services/vehicle_catalog_service.py\`

- Yeda repo: \`my-flask-app/app/data/model_technical_catalog_il.json\`

- MILO: \`backend/tools/yeda/contracts.py\`

בדיקות חובה:

- Parse full catalog snapshot.

- Unknown required field / corrupted JSON → fail closed.

- Variant ID algorithm matches existing service for fixtures.

Done כאשר: יש חוזה versioned שנבדק מול הקובץ האמיתי ולא מול דוגמה ידנית.

### 2.2 — Read-only GitHub Source Adapter

מטרה: להביא את המאגר מ־GitHub באופן reproducible ו־auditable.

מימוש טכני: \`YedaGitHubSource\` קורא repository/path/ref דרך GitHub API או raw immutable blob. sync נועל \`commit_sha + blob_sha + catalog_hash\`. אסור לעבוד על “latest” לא מזוהה בתוך Run. עבור repo ציבורי read אינו צריך secret; write יתווסף בנפרד.

מוקדי קוד:

- \`backend/tools/yeda/source.py\`

בדיקות חובה:

- Pinned SHA returns expected blob hash.

- SHA changed mid-sync → abort/retry; never mix two versions.

- Network failure keeps previous active snapshot intact.

Done כאשר: כל Run יודע בדיוק מאיזה commit של ידע רכב הגיע המידע.

### 2.3 — Snapshot + normalized index בתוך MILO

מטרה: לא להוריד 7.3MB בכל worker ולא להכניס את כולו ל־context.

מימוש טכני: בזמן sync דטרמיניסטי: שמור metadata של snapshot ופרק models/variants לטבלאות read-optimized ב־Supabase. Snapshot immutable; pointer/flag של active snapshot מתחלף רק אחרי validation מלא. ניתן לשחזר snapshot ממקור GitHub pinned.

מוקדי קוד:

- \`supabase/migrations/\<new\>\_yeda_catalog_cache.sql\`

- \`backend/tools/yeda/sync.py\`

- \`backend/repository/supabase.py\` methods lease/privilege appropriate לסנכרון.

בדיקות חובה:

- Idempotent sync של אותו SHA לא יוצר כפילויות.

- Interrupted sync לא הופך snapshot חלקי ל־active.

- Row/model/variant counts and checksum validated.

- Indexes על make/model/variant_id/year range.

Done כאשר: query על יצרן/דגם אינו תלוי בקריאת קובץ GitHub בזמן אמת.

### 2.4 — YedaCatalogTool — query API קומפקטי

מטרה: לתת ל־Commander capability צרה וברורה, לא raw dump.

מימוש טכני: Tool operations: \`catalog_meta\`, \`list_makes\`, \`list_models(make)\`, \`get_model(make,model)\`, \`list_variants(make,model,year?)\`, \`resolve_variant(variant_id)\`, \`search_identity\`, \`coverage_summary\`. כל תשובה מוגבלת בכמות רשומות ומחזירה provenance snapshot/commit.

מוקדי קוד:

- \`backend/tools/yeda/tool.py\`

- \`backend/tools/registry.py\` — register capability.

בדיקות חובה:

- Output schema bounds.

- Large model list paginated/limited.

- No whole-catalog return operation.

- Every fact includes source locator/snapshot.

Done כאשר: Commander יכול להשיב “מה יש בידע רכב על Tucson” בלי לראות JSON מלא.

### 2.5 — Coverage / Gap objects מול משימת המשתמש

מטרה: לאפשר למנוע להבין מה חסר במאגר הקיים עוד לפני Government API.

מימוש טכני: Tool/utility מייצר \`CatalogCoverage\`: existing models, year ranges, technical fields missing, unresolved/support levels, duplicate/ambiguous identities. התוצאה היא רשימת \`CatalogGap\` structured שנכנסת ל־Commander כ־work items; זו לא מערכת/mנוע נפרד.

מוקדי קוד:

- \`backend/tools/yeda/coverage.py\`

בדיקות חובה:

- Known missing field becomes one gap.

- No duplicate gap for same entity/field/scope.

- Low support is flagged but אינו נמחק.

Done כאשר: Commander יכול ליצור מחקר רק על פערים ולא לחקור מחדש records שלמים ללא סיבה.

### 2.6 — Yeda evidence/provenance integration

מטרה: להפוך רשומה קיימת מידע רכב ל־source/claim אמיתי במערכת evidence.

מימוש טכני: כל record שה־Tool מספק יכול להירשם כ־Source עם GitHub URL + commit SHA + JSON locator (model/variant_id) ו־Claim עם field/scope. זה מאפשר לממשלה בשלב 3 או Web research לסתור/לחזק אותו באותו Evidence Board.

מוקדי קוד:

- \`backend/tools/yeda/provenance.py\`

- \`swarm_v2/evidence.py\`

בדיקות חובה:

- אותו record locator deduped בריצה.

- Yeda claim traceable to exact commit/blob/variant.

- No copied model data without source locator.

Done כאשר: Verifier מסוגל להשוות claim מ־Yeda מול claim ממקור אחר.

### 2.7 — Deterministic Patch Proposal

מטרה: לאפשר “תשווה ותשנה” בלי לתת למודל לכתוב קובץ של 7.3MB.

מימוש טכני: Commander/Verifier מחזירים approved change intents בלבד. \`YedaPatchBuilder\` מקבל base snapshot SHA + typed operations: add_model, add_variant, update_field, resolve_alias, mark_needs_review. הוא טוען base JSON, מחיל פעולות דטרמיניסטית, מריץ dedupe/schema/full-catalog validation ומחשב diff + new hash.

מוקדי קוד:

- \`backend/tools/yeda/patch.py\`

בדיקות חובה:

- Same operations + same base → identical output.

- Invalid field/identity blocks patch.

- No delete by default; delete דורש operation מפורש עם policy נפרד.

- Stale base SHA blocks apply.

Done כאשר: המנוע מסוגל לייצר patch reviewable בלי mutation חיצוני.

### 2.8 — Controlled GitHub Write Path

מטרה: להשלים את היכולת לעדכן את ידע רכב בצורה בטוחה.

מימוש טכני: Write הוא capability נפרד \`yeda_catalog_write\` ולא חלק מ־read tool. דורש explicit approval/action. Token נשמר רק Worker Secret Manager. Writer משתמש optimistic concurrency עם current blob SHA; יוצר branch/commit או PR מועדף, לא overwrite עיוור ל־main. לפני push: full schema + hash + Yeda regression checks.

מוקדי קוד:

- \`backend/tools/yeda/writer.py\`

- Production secret/config: worker-only \`YEDA_GITHUB_TOKEN\` אם נדרש.

- Feature flag נפרד: \`MILO_ENABLE_YEDA_WRITES=false\` default.

בדיקות חובה:

- No approval → no write.

- Stale SHA → conflict, no retry overwrite.

- Token never logged/persisted.

- PR/commit contains only deterministic patch.

- Kill switch/feature flag can disable writer independently.

Done כאשר: משתמש יכול לאשר PatchProposal ולקבל commit/PR בטוח בלי ש־LLM מחזיק GitHub credentials.

### 2.9 — Stage 2 E2E acceptance

מטרה: להוכיח שה־Tool עובד בתוך המנוע ולא רק כ־library.

מימוש טכני: תרחיש: “בדוק Hyundai בידע רכב, מצא פערים, הצע שינוי לשדה חסר.” Commander משתמש Yeda Tool → Coverage → worker research/mock → verifier → PatchProposal. בדיקת write מתבצעת רק staging/test repo.

מוקדי קוד:

- Full Swarm V2 integration tests + Yeda fixtures.

בדיקות חובה:

- Read path אינו עושה provider call אם לא צריך.

- 7.3MB catalog לא מופיע בשום prompt/event.

- Patch provenance מלא.

- V1 unaffected.

- Production write flag נשאר off עד authorization נפרד.

Done כאשר: MILO מסוגל לקרוא, להשוות ולהציע/להחיל שינוי מבוקר בידע רכב end-to-end.

## סדר PRים מומלץ — שלב 2

| PR     | Scope                                                    | Gate                                |
|-------------------------------|---------------------------------------------------------------------------------|------------------------------------------------------------|
| S2-PR1 | Yeda schema contract + pinned GitHub source reader       | Full catalog parses                 |
| S2-PR2 | Snapshot/index tables + sync + read-only YedaCatalogTool | Fast compact queries                |
| S2-PR3 | Coverage gaps + provenance integration                   | Commander consumes Yeda gaps        |
| S2-PR4 | PatchBuilder + controlled writer + flags/secrets         | Reviewable safe update              |
| S2-PR5 | End-to-end Stage 2 suite                                 | Yeda read/compare/update flow green |

# שלב 3 — חיבור ל־API הממשלתי של data.gov.il

יעד השלב: להפוך את מאגר משרד התחבורה לעוגן דטרמיניסטי של קיום דגם/שנתון/וריאנט. ה־Commander לא “מגלה” בישראל דרך Web עובדה שהממשלה כבר נותנת; הוא משתמש ב־GovernmentVehicleTool ורק פותר gaps/aliases/מידע שאינו קיים במקור הרשמי.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>Scope ראשוני בלבד</p>
<p>בשלב 3 מחברים שני resources של dataset `degem-rechev-wltp`: (A) כמויות לפי תוצר/דגם/שנת ייצור — resource_id `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6`; (B) תוצרים ודגמים WLTP — resource_id `142afde2-6228-49f9-8a29-9b6c3a0cbe40`. מאגרים חודשיים/יבוא אישי יישארו הרחבה עתידית, לא תנאי לסיום שלב 3.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

### 3.1 — CKAN Client דטרמיניסטי

מטרה: לבודד את כל פרטי data.gov.il בתוך client אחד שניתן לבדיקה.

מימוש טכני: \`DataGovClient\` משתמש ב־CKAN Action/DataStore API. \`package_show\`/resource metadata נבדקים לפני ingest; \`datastore_search\` מתבצע עם pagination מוגבל, timeouts, retry על network/5xx, schema validation ו־JSON success envelope. אין Kimi בשכבה הזאת.

מוקדי קוד:

- \`backend/tools/government/client.py\`

בדיקות חובה:

- Pagination returns all records בדיוק פעם אחת.

- \`success:false\` envelope → error.

- Unexpected schema/field type → snapshot rejected.

- Timeout/retry bounded.

Done כאשר: ניתן למשוך resource מלא באופן reproducible ללא LLM.

### 3.2 — Snapshot metadata + raw ingest

מטרה: לשמר גרסה מלאה ומבוקרת של מה שהממשלה החזירה.

מימוש טכני: ליצור \`government_dataset_snapshots\` + raw rows keyed by snapshot/resource/\_id. כל snapshot שומר resource_id, source metadata, fetched_at, row_count, checksum, schema fingerprint, status. raw הוא append-only; active only after validation.

מוקדי קוד:

- \`supabase/migrations/\<new\>\_government_vehicle_snapshots.sql\`

- \`backend/tools/government/sync.py\`

בדיקות חובה:

- Same source checksum idempotent.

- Partial import never active.

- Row count/checksum verified before activation.

- Can trace normalized row back to raw \`\_id\` + snapshot.

Done כאשר: יש source-of-truth local snapshot שלא תלוי בזמינות data.gov בזמן chat.

### 3.3 — נרמול: יצרן / דגם / שנתון / וריאנט

מטרה: לפרק ~100K שורות למבנה שמתאים לשאלות של MILO, בלי AI.

מימוש טכני: Python/SQL normalization. Quantity resource בונה manufacturer/model/model_code/year/counts; WLTP מוסיף variant/engine/trim/body/fuel/power/safety/technology fields. Identity נשמרת עם codes + commercial identifiers + year; name similarity לבד אינה merge key.

מוקדי קוד:

- \`backend/tools/government/normalize.py\`

- normalized tables: \`government_vehicle_model_years\`, \`government_vehicle_variants\`.

בדיקות חובה:

- Deterministic re-normalization from same snapshot.

- No lost raw rows without explicit rejected_rows report.

- Indexes manufacturer/code/year/commercial name.

- Name variants כגון NEW TUCSON לא מתאחדים אוטומטית רק בגלל similarity.

Done כאשר: אפשר לשאול יצרן ולקבל tree מלא Model→Years→government variants.

### 3.4 — GovernmentVehicleTool

מטרה: לתת ל־Commander view קומפקטי על data.gov בלי exposing raw DB.

מימוש טכני: Tool operations: \`dataset_meta\`, \`list_manufacturers\`, \`get_manufacturer_summary\`, \`list_models\`, \`get_model_years\`, \`get_variants\`, \`get_government_evidence\`, \`search_codes\`. כל result כולל snapshot_id/resource_id/source row locators ומוגבל בכמות.

מוקדי קוד:

- \`backend/tools/government/tool.py\`

- \`backend/tools/registry.py\` — register.

בדיקות חובה:

- No full resource dump operation.

- Query bounded/paginated.

- Every fact traceable to official resource + raw row.

- Tool read path needs no Kimi/provider budget.

Done כאשר: Commander יכול לדעת “אילו Tucson ושנתונים משרד התחבורה מציג” בלי Web search.

### 3.5 — Crosswalk Government ↔ Yeda

מטרה: להשוות את שני העוגנים בלי ליצור מנוע שלישי.

מימוש טכני: Utility בתוך כלי הרכב/Tool layer מחזיר \`CatalogMatch\` ו־\`CatalogGap\`. matching tiers: exact codes/known identifiers when available → exact normalized make/model + overlapping year → explicit alias rule → ambiguous. LLM יכול לחקור ambiguous, אבל לא מאשר merge אוטומטי ללא evidence.

מוקדי קוד:

- \`backend/tools/vehicle_reconciliation.py\` או \`backend/tools/government/reconcile.py\`

בדיקות חובה:

- Matched/government_only/yeda_only/ambiguous/missing_years/under_enriched.

- Alias ambiguous אינו merged.

- Crosswalk record כולל method/confidence/evidence.

Done כאשר: כל פער הופך ל־structured work item שה־Commander יכול לתעדף.

### 3.6 — Government provenance לתוך Evidence Board

מטרה: להעניק למקור הממשלתי weight/trace ברור, לא “טקסט מהכלי”.

מימוש טכני: כל government fact נרשם כ־Source type \`government\` ו־Claim עם source locator/resource/snapshot/raw row. Source policy נותן authority גבוהה לקיום/שנתון/קוד דגם; הוא לא בהכרח authoritative לתקלות/אמינות/מחיר שוק.

מוקדי קוד:

- \`backend/tools/government/provenance.py\`

- \`swarm_v2/evidence.py\`

בדיקות חובה:

- Government claim vs Yeda claim can open conflict.

- Field-specific authority: existence/year/code ≠ reliability/market price.

- No government row silently converted into unsupported semantic field.

Done כאשר: Verifier מבין מה הממשלה מוכיחה ומה לא.

### 3.7 — Government-first Commander policy

מטרה: לשנות התנהגות של משימות רכב בלי hardcoding קטגוריות מחקר.

מימוש טכני: אין workflow קשיח “Government→Yeda→Web”, אבל Commander instruction/tool policy קובעת: כשמשימה נוגעת לקיום/כיסוי ישראלי, קודם השתמש ב־GovernmentVehicleTool ו־YedaCatalogTool. Web research נפתח רק ל־unresolved gaps, enrichments או contradictions. הקטגוריות עצמן עדיין דינמיות.

מוקדי קוד:

- \`swarm_v2/commander.py\` prompt/policy

- \`backend/tools/registry.py\` capability metadata.

בדיקות חובה:

- בקשת build Hyundai אינה מתחילה ב־broad web discovery.

- Government evidence מספיק → no web call.

- Ambiguous I25/Accent → targeted research task, לא auto merge.

Done כאשר: המנוע חוסך provider cost ומקטין false positives תוך שמירה על Swarm דינמי.

### 3.8 — End-to-end: Government + Yeda + Swarm + Patch

מטרה: להשלים את שלושת השלבים למוצר אחד עובד.

מימוש טכני: תרחיש סופי: user מבקש “עדכן Hyundai”. Commander מקבל Government summary + Yeda coverage; reconciliation מפיק gaps; workers חוקרים רק unresolved; verifier מאשר; builder מפיק Yeda PatchProposal; write path דורש approval. כל שלב checkpointed.

מוקדי קוד:

- Integration tests/staging runbooks.

בדיקות חובה:

- Government-only model/year יוצר gap.

- Yeda-only identity יוצר investigation ולא deletion.

- Known government field לא נחקר מחדש ב־Web.

- Patch references both government source and/or web evidence.

- Resume באמצע התהליך אינו מכפיל tool calls/evidence/patch ops.

Done כאשר: בקשת catalog מלאה עוברת דרך מנוע אחד בלבד ומסתיימת ב־reviewable update.

### 3.9 — Refresh / diff operation

מטרה: להבטיח שהחיבור הממשלתי אינו חד־פעמי.

מימוש טכני: להוסיף operation \`sync_if_changed\`: metadata/checksum קודם, ingest מלא רק אם המקור השתנה. snapshot חדש יוצר diff מול active הקודם: added/changed/removed model-year/variants. המנוע יכול לקבל רק delta ולא לרוץ על כל המאגר.

מוקדי קוד:

- \`backend/tools/government/sync.py\`, \`diff.py\`

בדיקות חובה:

- No change → no Swarm research.

- New year/variant → gap ממוקד.

- Snapshot rollback means pointer change only; raw history נשמר.

Done כאשר: המערכת מוכנה לעדכונים עתידיים בלי full rebuild בכל פעם.

## סדר PRים מומלץ — שלב 3

| PR     | Scope                                             | Gate                           |
|-------------------------------|--------------------------------------------------------------------------|-------------------------------------------------------|
| S3-PR1 | CKAN client + resource contracts + fixtures       | Official resources parse       |
| S3-PR2 | Snapshot/raw + normalization tables + sync        | Deterministic government tree  |
| S3-PR3 | GovernmentVehicleTool + provenance                | Commander reads official facts |
| S3-PR4 | Crosswalk Government↔Yeda + gap objects           | Structured diff                |
| S3-PR5 | Government-first policy + full E2E + refresh diff | Three-stage product complete   |

# 4. הארכיטקטורה בסיום שלושת השלבים

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th>Browser<br />
│<br />
▼<br />
Vercel Gateway<br />
│ (unchanged trust boundary)<br />
▼<br />
Private Cloud Run API<br />
│ create idempotent Run / launch Worker<br />
▼<br />
Cloud Run Worker<br />
│ lease + heartbeat + budgets + ProviderScheduler<br />
▼<br />
EngineRegistry ───────────────────────────────┐<br />
├── vehicle_catalog_v1 [frozen control] │<br />
└── swarm_v2 [new primary] │<br />
│ │<br />
Task Commander │<br />
│ │<br />
Dynamic Task Graph │<br />
│ │<br />
Generic Worker Pool │<br />
│ │<br />
┌───────────┼────────────┐ │<br />
▼ ▼ ▼ │<br />
Web/Kimi YedaCatalog Government<br />
Tool VehicleTool<br />
└───────────┼────────────┘<br />
▼<br />
Sources / Claims / Conflicts<br />
▼<br />
Verifier<br />
▼<br />
Deterministic Builder<br />
▼<br />
Result / PatchProposal<br />
▼<br />
[explicit approval if write]<br />
▼<br />
Yeda GitHub branch / PR</th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## 5. Contract של Tool Registry בסיום

| Capability         | Mode        | מי מפעיל                                     | Authority / גבול                                                              |
|-------------------------------------------|------------------------------------|---------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| web_research       | read / paid | Generic Worker via ModelGateway              | השלמה/מחקר; לעולם לא מקור יחיד לעובדה ממשלתית כאשר Government Tool מכסה אותה. |
| yeda_catalog_read  | read        | Commander/Worker                             | מה קיים במאגר הנוכחי; provenance ל־commit/blob/variant.                       |
| yeda_catalog_write | write       | Deterministic PatchBuilder + approved writer | כבוי default; explicit approval; optimistic SHA CAS; PR/branch preferred.     |
| government_vehicle | read/sync   | Commander/Worker + deterministic sync        | עוגן רשמי לקיום/קוד/שנתון/מפרט שמופיע במאגר; לא authority לכל תחומי הרכב.     |

# 6. Acceptance Matrix — “סיימנו את שלושת השלבים”

| תחום            | קריטריון סיום                                                                                        |
|----------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| Routing         | Project.workflow_key=vehicle_catalog_v1 נשאר V1; swarm_v2 נבחר רק דרך trusted project config.        |
| Resume          | V2 checkpoint/resume עובד; no cross-workflow checkpoint contamination.                               |
| Safety          | כל V2 durable writes lease-guarded; stale worker cannot write; paid execution fail-closed.           |
| Budget/provider | כל Kimi call עובר BudgetTracker + shared ProviderScheduler; 429 אינו semantic retry.                 |
| Dynamic swarm   | Commander יוצר task taxonomies שונות; אין engine/gearbox/dimensions roles קשיחים.                    |
| Tools           | Registry allowlisted; invalid tool blocked; read/write capabilities נפרדות.                          |
| Yeda read       | מאגר pinned + snapshot/index; query compact; provenance מלא.                                         |
| Yeda write      | Patch deterministic, schema-validated, approval required, stale SHA blocks write, token worker-only. |
| Government      | שני CKAN resources מסונכרנים דטרמיניסטית עם snapshot/checksum/raw provenance.                        |
| Reconciliation  | matched/government_only/yeda_only/ambiguous/missing_years/under_enriched הופכים ל־gaps structured.   |
| Evidence        | Final claim traceable to source/run/task; conflicts explicit; unresolved never silently published.   |
| End-to-end      | בקשת “עדכן Hyundai” מפעילה Government + Yeda, מחקר ממוקד לפערים, אימות, PatchProposal וכתיבה מאושרת. |
| Regression      | Stage C/V1 suites + new V2/Yeda/Gov suites green; no changes to browser/gateway trust boundaries.    |

# 7. סדר עבודה כולל — בלי ליצור “שבעה פרויקטים”

| סדר | משימה          | תוצאה                                                     |
|----------------------------|---------------------------------------|----------------------------------------------------------------------------------|
| 1   | S1-PR1…S1-PR5  | Swarm Core מלא ומוכח עם mocks.                            |
| 2   | S2-PR1…S2-PR5  | אותו מנוע קיבל Yeda Tool — read/compare/patch/write.      |
| 3   | S3-PR1…S3-PR5  | אותו מנוע קיבל Government Tool — sync/normalize/diff.     |
| 4   | E2E acceptance | Government + Yeda + Swarm באותו Run, עם patch reviewable. |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>העיקרון שמונע “חצי שנה של שכבות”</p>
<p>אנחנו לא בונים Data Engine נפרד, Bridge נפרד, Coverage Engine נפרד ו־Workflow Adapter נפרד. כל capability נבנה כ־Tool קטן עם חוזה קבוע ומתחבר ל־Swarm Core. persistence/normalization שנדרשים לכל Tool הם implementation detail של אותו Tool, לא מערכת חדשה.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

# 8. מה אסור לעשות במהלך שלושת השלבים

- לא לשנות את \`vehicle_catalog_v1\` כדי “להכין מקום” ל־V2; הוא נשאר control עד שהחדש מוכח.

- לא לבחור engine לפי user-controlled metadata או prompt.

- לא להפעיל את \`SupervisorMode.ACTIVE\` הקיים ולחשוב שזה ה־Commander החדש; ה־Supervisor הקיים נשאר shadow.

- לא ליצור client ישיר ל־Kimi בתוך worker/commander; כל call עובר BudgetTracker + ProviderScheduler.

- לא להוסיף durable write חדש שאינו lease-guarded.

- לא לשמור chain-of-thought. נשמרים plan JSON, evidence, verdicts ו־rationale summaries בלבד.

- לא לשלוח את \`model_technical_catalog_il.json\` המלא או 100K rows מ־data.gov אל ה־LLM.

- לא לאפשר LLM לכתוב ישירות ל־Yeda GitHub או Canonical DB.

- לא hardcode categories כמו engine/gearbox/dimensions כ־agent roles; אלו דוגמאות שה־Commander רשאי להמציא לפי המשימה.

- לא להעלות concurrency ישירות ל־Tier2 ceiling. logical worker concurrency ו־provider ceiling נשלטים בנפרד ומועלים לפי telemetry.

- לא למחוק raw/snapshots היסטוריים כדי “לנקות”; diff/rebuild דורשים provenance.

# 9. Runbook מינימלי לכל PR

1.  Pull latest main ולוודא CI baseline ירוק.

2.  להגדיר Scope יחיד ל־PR; לא לערב שינוי V1, migration, Tool חדש ו־Commander rewrite באותו PR אם אינם תלויים ישירות.

3.  Unit tests + contract tests + fail-closed negative tests.

4.  אם יש migration: ephemeral PostgreSQL + rerun-safe + service_role ACL matrix + stale lease tests.

5.  Full offline suite; secret scan; unsafe-default checks; production config validation.

6.  Code review: verify no direct provider/client/DB write bypass.

7.  Merge רק כשה־V1 regression עדיין ירוק.

8.  Runtime deploy/test רק דרך release path; paid provider test דורש authorization מפורש נפרד ונשאר כבוי כברירת מחדל.

# 10. מקורות קוד ונתונים שנבדקו לצורך המפה

| מקור                                                                               | מה נלקח ממנו                                                                                             |
|-----------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| MILO main — docs/production-readiness/STAGE_C_ACCEPTANCE.md                        | סגירת Stage C, Attempt 7, fail-closed posture, Tier2/operating envelope.                                 |
| MILO main — backend/worker/main.py + backend/worker/engine.py                      | Engine Protocol, lifecycle, current hardcoded V1 selection וה־checkpoint routing issue.                  |
| MILO main — backend/engines/vehicle_catalog_v1/adapter.py + engine.py              | Dependency injection, callbacks, per-run ProviderScheduler, checkpoints.                                 |
| MILO main — backend/provider_scheduler.py                                          | Concurrency/RPM/TPM/backpressure contract שכבר הוכח.                                                     |
| MILO main — backend/schemas.py + backend/internet_governance.py + migration 005    | Tool/source/claim/conflict contracts and existing evidence tables.                                       |
| MILO main — backend/repository/supabase.py                                         | Lease-guarded worker writes הקיימים והפער ב־tool/source/claim/conflict direct inserts.                   |
| Yeda — reliabilityAIModelsR2/my-flask-app/app/data/model_technical_catalog_il.json | קובץ הקטלוג הקיים שאליו מתחברים בשלב 2.                                                                  |
| Yeda — vehicle_catalog_service.py                                                  | Root models, technical_variants_il, deterministic variant_id, source/model metadata, catalog hash/cache. |
| data.gov.il CKAN docs                                                              | Action API / datastore_search JSON envelope and read semantics.                                          |
| data.gov.il resource 5e87a7a1…                                                     | כמויות לפי תוצר/דגם/שנת יצור.                                                                            |
| data.gov.il resource 142afde2…                                                     | WLTP / model & variant technical data.                                                                   |

# 11. Verdict

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>המסלול הסופי</p>
<p>אחרי Stage C, בונים פעם אחת Swarm Core מלא. בשלב 2 מחברים אליו YedaCatalogTool. בשלב 3 מחברים GovernmentVehicleTool. כל השאר — snapshots, indexes, crosswalk, patching — הן שכבות פנימיות של Tools, לא מנועים חדשים. כך נשמרת תשתית ה־Production שכבר הוכחה, V1 נשאר control, והפרויקט מתקדם בשלושה Gates ברורים במקום שרשרת ארוכה של מערכות.</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

## נספח — Reference URLs / identifiers

GitHub main closure commit: 9c2cd6427b662d210e57694394a72c15f995a5f0

MILO: docs/production-readiness/STAGE_C_ACCEPTANCE.md

MILO: backend/worker/main.py

MILO: backend/provider_scheduler.py

Yeda: reliabilityAIModelsR2/my-flask-app/app/services/vehicle_catalog_service.py

Yeda catalog: my-flask-app/app/data/model_technical_catalog_il.json

data.gov.il: CKAN Action API / datastore_search

Government resource: 5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6

Government resource: 142afde2-6228-49f9-8a29-9b6c3a0cbe40
