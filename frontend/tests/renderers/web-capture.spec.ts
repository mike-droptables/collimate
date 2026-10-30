import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

interface CaptureResult { text: string; truncated: boolean; messages: {text:string; artifacts:{language:string; content:string}[]}[] }
const content = resolve(__dirname, '../../../content');
function source(file: string, variable: string): string {
  const text = readFileSync(resolve(content, file), 'utf8');
  const match = text.match(new RegExp(`${variable} = r"""([\\s\\S]*?)"""`));
  if (!match) throw new Error(`Missing ${variable} in ${file}`);
  return `(${match[1]})()`;
}

test('generic capture bounds text before transport __F1992', async ({ page }) => {
  await page.setContent('<title>Bounded</title><main></main>');
  await page.locator('main').evaluate(el => { el.textContent = '\u0001'.repeat(2_000_000); });
  const scrape = await page.evaluate<CaptureResult>(source('generators/web/web/web.py', '_PAGE_SCRAPE_JS'));
  expect(scrape.text.length).toBeLessThanOrEqual(1_000_000);
  expect(scrape.truncated).toBe(true);
  expect(JSON.stringify(scrape).length).toBeLessThan(8_000_000);
});

test('generic HTML snapshot is bounded inside the page __F1992', async ({ page }) => {
  await page.setContent('<main></main>');
  await page.locator('main').evaluate(el => { el.textContent = 'x'.repeat(2_000_000); });
  const snapshot = await page.evaluate<{html:string; truncated:boolean}>(source('generators/web/web/web.py', '_PAGE_HTML_JS'));
  expect(snapshot.html.length).toBe(1_000_000);
  expect(snapshot.truncated).toBe(true);
});

for (const host of ['claude.ai', 'aistudio.google.com']) {
  test(`${host} code is absent from prose and retained once as artifact __F2000_F1992`, async ({ page }) => {
    const open = host === 'claude.ai' ? '<div data-testid="assistant-message">' : '<ms-chat-turn data-turn-role="model">';
    const close = host === 'claude.ai' ? '</div>' : '</ms-chat-turn>';
    await page.setContent(`${open}<p>Before code.</p><pre><code class="language-python">print('unique-code')</code></pre><p>After code.</p>${close}`);
    const js = source(`generators/web/web/configs/web--capture-chat/files/capture_scripts/${host}.py`, '_SCRAPE');
    const scrape = await page.evaluate<CaptureResult>(js);
    expect(scrape.messages[0].text).toContain('Before code.');
    expect(scrape.messages[0].text).toContain('After code.');
    expect(scrape.messages[0].text).not.toContain('unique-code');
    expect(scrape.messages[0].artifacts).toEqual([{ language: 'python', content: "print('unique-code')" }]);
    expect(await page.locator('code').textContent()).toBe("print('unique-code')");
    await page.locator('code').evaluate(el => { el.textContent = '\u0001'.repeat(2_000_000); });
    const bounded = await page.evaluate<CaptureResult>(js);
    expect(bounded.truncated).toBe(true);
    expect(JSON.stringify(bounded).length).toBeLessThan(8_000_000);
    expect(bounded.messages[0].artifacts[0].content.length).toBeLessThanOrEqual(1_000_000);
  });
}

test('capture limits preserve UTF-8 encodable text across astral boundaries __F2000_F1992',async({page})=>{
 const assertEncodable=(value:unknown)=>{
  const program='import json,sys\ndef check(v):\n if isinstance(v,str): v.encode("utf-8")\n elif isinstance(v,dict):\n  for x in v.values(): check(x)\n elif isinstance(v,list):\n  for x in v: check(x)\ncheck(json.load(sys.stdin))';
  expect(()=>execFileSync('python3',['-c',program],{input:JSON.stringify(value),stdio:['pipe','pipe','pipe']})).not.toThrow();
 };
 await page.setContent('<title></title><main></main><a href="https://example.test/">link</a>');
 await page.evaluate(()=>{document.title='x'.repeat(4095)+'😀';document.querySelector('main')!.textContent='x'.repeat(999999)+'😀';document.querySelector('a')!.textContent='x'.repeat(119)+'😀';});
 assertEncodable(await page.evaluate(source('generators/web/web/web.py','_PAGE_SCRAPE_JS')));
 for(const host of ['claude.ai','aistudio.google.com']){
  await page.setContent('<div data-testid="assistant-message"><ms-chat-turn data-turn-role="model"><pre><code class="language-python"></code></pre></ms-chat-turn></div>');
  await page.locator('code').evaluate(el=>{el.textContent='x'.repeat(999999)+'😀';});
  assertEncodable(await page.evaluate(source(`generators/web/web/configs/web--capture-chat/files/capture_scripts/${host}.py`,'_SCRAPE')));
 }
 // Position a two-unit character exactly across the HTML source boundary.
 await page.setContent('<main></main>');
 await page.evaluate(()=>{const prefix=document.documentElement.outerHTML.indexOf('<main>')+6;document.querySelector('main')!.textContent='x'.repeat(999999-prefix)+'😀';});
 assertEncodable(await page.evaluate(source('generators/web/web/web.py','_PAGE_HTML_JS')));
});
