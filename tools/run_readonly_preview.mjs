import fs from 'node:fs/promises';
import {start} from './local_readonly_preview.mjs';
let input='';for await(const chunk of process.stdin)input+=chunk;
const snapshot=JSON.parse(input);input='';
if(process.argv[2]){await fs.unlink(process.argv[2]);await fs.rmdir(process.argv[2].slice(0,process.argv[2].lastIndexOf('/'))).catch(()=>{})}
const instance=await start(snapshot,process.cwd(),'/Users/aydin.ozkan1/Documents/Codex/2026-10-25/github-plugin-github-openai-curated-remote/work/harcamatakip/.venv/bin/python','http://127.0.0.1:62911');
console.log(instance.url);
