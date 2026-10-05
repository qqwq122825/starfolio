export function identity(request){const id=request.headers.get('oai-authenticated-user-id'),email=request.headers.get('oai-authenticated-user-email');return id&&email?{id,email}:null;}
export function authorizeWrite(request){
 if(!identity(request))return '请先通过 ChatGPT 登录';
 if(request.headers.get('origin')!==new URL(request.url).origin)return '禁止跨站或缺少来源的写入';
 if(!request.headers.get('content-type')?.startsWith('application/json'))return '仅接受 JSON';
 if(request.headers.get('x-watch-token')!=='same-origin-required')return '请通过本工作台操作';
 if(!/^[a-zA-Z0-9-]{16,80}$/.test(request.headers.get('x-request-id')||''))return '缺少有效操作编号';
 return null;
}
