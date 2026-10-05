import { getChatGPTUser } from '../../chatgpt-auth';
import { readWorkspace, commitOperation } from '../../../lib/store';
import { view, digest, PAPER_DISABLED } from '../../../lib/watch-engine.mjs';
import { authorizeWrite } from '../../../lib/security.mjs';
export const dynamic='force-dynamic';
const headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'};
function json(data:unknown,status=200){return Response.json(data,{status,headers});}
export async function GET(request:Request){
 const user=await getChatGPTUser();if(!user)return json({error:'请先登录'},401);
 try {const path=new URL(request.url).pathname,state=await readWorkspace(user.userId),now=new Date().toISOString();if(path==='/api/state')return json(view(state,now));if(path==='/api/digest')return new Response(digest(state,now),{headers:{...headers,'Content-Type':'text/plain;charset=utf-8'}});return json({error:'接口不存在'},404);}catch {return json({error:'持久存储暂不可用，请稍后重试；没有展示缓存为新数据'},503);}
}
export async function POST(request:Request){
 const user=await getChatGPTUser();if(!user)return json({error:'请先登录'},401);
 const denied=authorizeWrite(request);if(denied)return json({error:denied},403);
 const path=new URL(request.url).pathname;if(path.startsWith('/api/paper/'))return json({error:PAPER_DISABLED},501);
 try {if(Number(request.headers.get('content-length'))>524288)return json({error:'文件超过 512 KB 限制'},413);const raw=await request.text();if(new TextEncoder().encode(raw).length>524288)return json({error:'文件超过 512 KB 限制'},413);const data=JSON.parse(raw);return json(await commitOperation(user.userId,request.headers.get('x-request-id')!,path,data,new Date().toISOString()));}catch(error){const message=error instanceof Error?error.message:'保存失败';if(/D1_|SQLITE|database|binding/i.test(message)){console.error('workspace persistence error');return json({error:'持久存储暂不可用，内容未确认保存；请保留输入后重试'},503);}return json({error:message.slice(0,500)},400);}
}
