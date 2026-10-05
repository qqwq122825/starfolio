import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]
(ROOT/'artifacts').mkdir(exist_ok=True)
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
  page=await browser.new_page(viewport={'width':1440,'height':1180},device_scale_factor=1)
  errors=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  await page.goto('http://127.0.0.1:8765/')
  await page.get_by_text('优先关注').wait_for()
  await page.screenshot(path=str(ROOT/'artifacts/dashboard-desktop.png'),full_page=True)
  await page.get_by_role('button',name='查看证据与反证 →').first.click()
  await page.get_by_text('失效条件',exact=True).wait_for()
  await page.screenshot(path=str(ROOT/'artifacts/evidence-detail.png'))
  await page.get_by_role('button',name='关闭',exact=True).click()
  assert not await page.locator('dialog').is_visible()
  for label in ['自选与主题','事件核验','持仓与模拟','提醒与审计','数据与设置']:
   await page.locator('nav').get_by_role('button',name=label).click()
   await page.wait_for_timeout(100)
  await page.get_by_role('combobox').select_option('imported')
  await page.wait_for_timeout(300)
  await page.locator('nav').get_by_role('button',name='研究总览').click()
  await page.get_by_text('还没有真实自选标的').wait_for()
  await page.screenshot(path=str(ROOT/'artifacts/dashboard-imported-empty.png'),full_page=True)
  await page.get_by_role('button',name='返回演示工作区 →').click()
  await page.get_by_text('优先关注').wait_for()
  await page.set_viewport_size({'width':390,'height':844})
  await page.screenshot(path=str(ROOT/'artifacts/dashboard-mobile.png'),full_page=True)
  assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile horizontal overflow'
  await browser.close()
  print(json.dumps({'js_errors':errors,'screenshots':4,'navigation':'pass','modal_close':'pass','mode_switch':'pass','mobile_overflow':'pass'},ensure_ascii=False))
  assert not errors,errors
asyncio.run(main())
