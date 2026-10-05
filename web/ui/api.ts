import type { Workspace } from './types';
export async function request<T>(path:string,data?:unknown,token='same-origin-required'):Promise<T>{
 const response=await fetch(path,data===undefined?{credentials:'same-origin',cache:'no-store'}:{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Watch-Token':token,'X-Request-ID':crypto.randomUUID()},body:JSON.stringify(data)});
 let result:unknown;try{result=await response.json()}catch{throw new Error('响应无效，请检查登录状态后重试；保留尚未保存的输入')}
 if(!response.ok)throw new Error(typeof result==='object'&&result!==null&&'error'in result?String(result.error):'请求失败');return result as T;
}
export const readWorkspace=()=>request<Workspace>('/api/state');
