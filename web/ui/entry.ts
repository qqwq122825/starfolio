import { createApp } from 'vue';
import ElementPlus from 'element-plus';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import 'element-plus/dist/index.css';
import './workspace.css';
import ResearchWorkspace from './ResearchWorkspace.vue';
export function mountWorkspace(element:HTMLElement){const app=createApp(ResearchWorkspace);app.use(ElementPlus,{locale:zhCn,size:'default'});app.mount(element);return ()=>app.unmount();}
