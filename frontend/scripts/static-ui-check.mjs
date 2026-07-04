import { readFileSync } from 'node:fs';
const page = readFileSync('app/page.tsx','utf8');
const reducer = readFileSync('lib/runReducer.ts','utf8');
const required = ['Projects','Conversations','Workflow proposal','Live run','Live event stream','Final artifacts','Agents','Workflow','Sources','Claims','Conflicts','Costs','Developer','forbidden','approved','active'];
for (const item of required) { if (!page.includes(item)) throw new Error(`Missing UI marker: ${item}`); }
for (const item of ['some(e => e.id === event.id)','reconstructRun','tool_access_granted','source_recorded']) { if (!reducer.includes(item)) throw new Error(`Missing reducer marker: ${item}`); }
console.log('Static UI/reducer coverage markers found.');
