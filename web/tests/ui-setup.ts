import { vi } from 'vitest';
class ResizeObserverStub {observe(){} unobserve(){} disconnect(){}}
globalThis.ResizeObserver=ResizeObserverStub;
Object.defineProperty(window,'matchMedia',{value:vi.fn().mockImplementation(()=>({matches:false,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
Element.prototype.scrollTo=vi.fn();
