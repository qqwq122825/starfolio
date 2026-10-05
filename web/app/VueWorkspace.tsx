'use client';
import { useEffect, useRef, useState } from 'react';
export default function VueWorkspace(){const host=useRef<HTMLDivElement>(null),[error,setError]=useState('');useEffect(()=>{let cancelled=false;let cleanup:(()=>void)|undefined;void import('../ui/entry').then(({mountWorkspace})=>{if(!cancelled&&host.current)cleanup=mountWorkspace(host.current)}).catch(()=>{if(!cancelled)setError('工作台加载失败，请刷新页面后重试')});return()=>{cancelled=true;cleanup?.()};},[]);return <>{error&&<p role="alert">{error}</p>}<div ref={host}><p className="initial-loading">正在加载星策工作台…</p></div></>;}
