import { env } from 'cloudflare:workers';
import { initialState, mutate } from './watch-engine.mjs';
type Row = { revision:number; payload:string };
function database(): D1Database { const db=(env as unknown as {DB?:D1Database}).DB;if(!db)throw new Error('持久存储暂不可用，请稍后重试');return db; }
export async function readWorkspace(owner:string){const row=await database().prepare('SELECT revision,payload FROM workspaces WHERE owner = ?').bind(owner).first<Row>();return row?JSON.parse(row.payload):initialState();}
export async function commitOperation(owner:string,requestId:string,path:string,data:unknown,now:string){
 const db=database(),key=`${owner}:${requestId}`,requestHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify([path,data])))),x=>x.toString(16).padStart(2,'0')).join('');
 async function existing(){const old=await db.prepare('SELECT request_hash,response FROM operations WHERE key = ? AND owner = ?').bind(key,owner).first<{request_hash:string;response:string}>();if(old){if(old.request_hash!==requestHash)throw new Error('操作编号已用于不同内容，请刷新后重试');return JSON.parse(old.response);}return null;}
 const old=await existing();if(old)return old;
 await db.prepare('INSERT OR IGNORE INTO workspaces (owner,revision,payload,updated_at) VALUES (?,0,?,?)').bind(owner,JSON.stringify(initialState()),now).run();
 for(let attempt=0;attempt<4;attempt++){
  const already=await existing();if(already)return already;
  const row=await db.prepare('SELECT revision,payload FROM workspaces WHERE owner = ?').bind(owner).first<Row>();if(!row)throw new Error('工作区暂不可用');
  const state=JSON.parse(row.payload),result=mutate(state,path,data,now),serialized=JSON.stringify(state),response=JSON.stringify(result);
  if(new TextEncoder().encode(serialized).length>1500000)throw new Error('工作区已达容量限制，本次内容未保存');
  try {
   const batch=await db.batch([
    db.prepare('UPDATE workspaces SET payload = ?, revision = revision + 1, updated_at = ? WHERE owner = ? AND revision = ?').bind(serialized,now,owner,row.revision),
    db.prepare('INSERT INTO operations (key,owner,request_hash,response,created_at) SELECT ?,?,?,?,? WHERE changes() = 1').bind(key,owner,requestHash,response,now)
   ]);
   if(batch[0].meta.changes===1&&batch[1].meta.changes===1)return result;
  } catch(error){const retry=await existing();if(retry)return retry;throw error;}
 }
 throw new Error('另一项操作正在保存，请稍后重试；本次未写入');
}
