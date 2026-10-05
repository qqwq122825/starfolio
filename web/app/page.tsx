import { requireChatGPTUser } from './chatgpt-auth';
import VueWorkspace from './VueWorkspace';
export const dynamic='force-dynamic';
export default async function Page(){await requireChatGPTUser('/');return <><VueWorkspace/><noscript>此工作台需要启用 JavaScript。数据仅在登录后加载。</noscript></>;}
