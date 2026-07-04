import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
const roots = ['app','components','lib'];
const forbidden = [/SUPABASE_SERVICE_ROLE/i,/KIMI_API_KEY/i,/MOONSHOT_API_KEY/i,/service_role/i,/sk-[A-Za-z0-9_-]{20,}/];
let failed = false;
function walk(dir){ for (const f of readdirSync(dir)){ const p=join(dir,f); const s=statSync(p); if(s.isDirectory()) walk(p); else if(/\.(ts|tsx|js|jsx|mjs)$/.test(p)){ const txt=readFileSync(p,'utf8'); for(const r of forbidden){ if(r.test(txt)){ console.error(`Forbidden secret marker ${r} in ${p}`); failed=true; } } } } }
roots.forEach(walk); if(failed) process.exit(1); console.log('No browser secret markers found.');
