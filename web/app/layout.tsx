import type { Metadata } from 'next';
import './globals.css';
export const metadata:Metadata={title:'星策 · 私有研究工作台',description:'自选观察、证据核验、人工数据导入与按需扫描。无实盘交易，云端模拟执行尚未启用。',robots:{index:false,follow:false},icons:{icon:'/favicon.svg',shortcut:'/favicon.svg'}};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="zh-CN"><body>{children}</body></html>;}
