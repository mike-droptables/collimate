import { expect, test } from '@playwright/test';

// The served runtime's primary file belongs to the host's document owner
// (RendererAPI.onSetContent): a file-change notice never re-reads the primary
// file, so no external read can race a newer save; an accepted body arrives
// as an in-place push that emits no reportChange.
test('stale external read cannot overwrite a newer save __F0260_P3F02_F0308', async ({ page }) => {
  await page.goto('/__renderer_rollout/page');
  await page.evaluate(`(async () => {
    const React = await import('https://esm.sh/react@19.3.1?target=es2022');
    const {defineRenderer, useCollimateFile} = await import('/api/sdk/react.js');
    const listeners = new Set(), pushes = new Set();
    window.runtimeProbe = {saves:[], reads:0, listeners, pushes};
    const api = {getContent:async()=>'server-before-save', getFilename:()=>'probe.txt', isReadOnly:()=>false,
      onFilesChange:fn => {listeners.add(fn); return()=>listeners.delete(fn);},
      onSetContent:fn => {pushes.add(fn); return()=>pushes.delete(fn);},
      getSiblingFile:()=>{window.runtimeProbe.reads++;return new Promise(()=>{});},
      reportChange:text=>window.runtimeProbe.saves.push(text), ready:()=>{}};
    const renderer = defineRenderer({component:()=>{const {content,saveContent}=useCollimateFile();
      return React.createElement('input', {'data-testid':'runtime-editor', value:content, onChange:e=>saveContent(e.target.value)});}});
    document.body.innerHTML='<div id="runtime-mount"></div>';
    window.runtimeProbe.dispose=await renderer.mount(document.getElementById('runtime-mount'),api);
  })()`);
  const field = page.getByTestId('runtime-editor');
  await expect(field).toHaveValue('server-before-save');
  await field.fill('first edit');
  await page.evaluate('(()=>{for(const fn of window.runtimeProbe.listeners) fn({changedFiles:["other.txt"]})})()');
  await page.evaluate('(()=>{for(const fn of window.runtimeProbe.listeners) fn({changedFiles:["probe.txt"]})})()');
  await field.fill('second edit');
  await expect(field).toHaveValue('second edit', {timeout: 2000});
  expect(await page.evaluate('window.runtimeProbe.reads')).toBe(0);
  expect(await page.evaluate('window.runtimeProbe.pushes.size')).toBe(1);
  expect(await page.evaluate('window.runtimeProbe.saves')).toEqual(['first edit','second edit']);
  await page.evaluate('window.runtimeProbe.dispose()');
  expect(await page.evaluate('[window.runtimeProbe.listeners.size,window.runtimeProbe.pushes.size]')).toEqual([0,0]);
});

test('newest read and host push win; disposed runtime releases callbacks __F0260_P3F02', async ({page}) => {
  await page.goto('/__renderer_rollout/page');
  await page.evaluate(`(async()=>{
    const React=await import('https://esm.sh/react@19.3.1?target=es2022');
    const sdk=await import('/api/sdk/react.js');
    const listeners=new Set(), pushes=new Set();
    window.runtimeProbe={listeners,pushes,reads:0,saves:[],ready:0};
    const api={getContent:async()=>'initial',getFilename:()=>'probe.txt',isReadOnly:()=>false,
      onFilesChange:fn=>{listeners.add(fn);return()=>listeners.delete(fn);},
      onSetContent:fn=>{pushes.add(fn);return()=>pushes.delete(fn);},
      getSiblingFile:()=>{window.runtimeProbe.reads++;return new Promise(()=>{});},
      reportChange:value=>window.runtimeProbe.saves.push(value),ready:()=>window.runtimeProbe.ready++};
    const renderer=sdk.defineRenderer({component:()=>React.createElement('output',{'data-testid':'runtime-content'},sdk.useCollimateFile().content)});
    document.body.innerHTML='<div id="runtime-mount"></div>';
    window.runtimeProbe.dispose=await renderer.mount(document.getElementById('runtime-mount'),api);
    window.runtimeProbe.fire=()=>{for(const fn of listeners)fn({changedFiles:['probe.txt']});};
    window.runtimeProbe.push=value=>{for(const fn of pushes)fn(value);};
  })()`);
  const content=page.getByTestId('runtime-content');
  await expect(content).toHaveText('initial');
  await expect.poll(()=>page.evaluate('window.runtimeProbe.ready')).toBe(1);
  await page.evaluate('window.runtimeProbe.fire();window.runtimeProbe.fire()');
  await page.evaluate('window.runtimeProbe.push("older");window.runtimeProbe.push("newest")');
  await expect(content).toHaveText('newest');
  await page.evaluate('window.runtimeProbe.fire()');
  await expect(content).toHaveText('newest');
  await page.evaluate('window.runtimeProbe.push("host push")');
  await expect(content).toHaveText('host push');
  expect(await page.evaluate('window.runtimeProbe.reads')).toBe(0);
  expect(await page.evaluate('window.runtimeProbe.saves')).toEqual([]);
  await page.evaluate('window.runtimeProbe.dispose();window.runtimeProbe.push("late after dispose")');
  await expect(content).toHaveCount(0);
  expect(await page.evaluate('[window.runtimeProbe.listeners.size,window.runtimeProbe.pushes.size]')).toEqual([0,0]);
});

test('compiled runtime preserves default diff, mount error and deferred readiness __F0259_F1847',async({page})=>{
  await page.goto('/__renderer_rollout/page');
  await page.evaluate(`(async()=>{
    const React=await import('https://esm.sh/react@19.3.1?target=es2022');const sdk=await import('/api/sdk/react.js');
    window.runtimeProbe={ready:0,saves:[]};
    const api={getContent:async()=>'after line',getDiffContent:async()=>'before line',getDiffLabels:()=>({base:'version A',current:'version B'}),
      getFilename:()=>'probe.txt',isReadOnly:()=>false,onFilesChange:()=>()=>{},onSetContent:()=>()=>{},
      reportChange:value=>window.runtimeProbe.saves.push(value),ready:()=>window.runtimeProbe.ready++};
    document.body.innerHTML='<div id="diff"></div><div id="error"></div><div id="deferred"></div>';
    const diff=sdk.defineRenderer({component:()=>null});await diff.mountDiff(document.getElementById('diff'),api);
    const broken=sdk.defineRenderer({component:()=>{throw Error('actual render failure');}});await broken.mount(document.getElementById('error'),api);
    const deferred=sdk.defineRenderer({deferReady:true,component:()=>React.createElement('span',null,'deferred body')});await deferred.mount(document.getElementById('deferred'),api);
  })()`);
  await expect(page.locator('#diff')).toContainText('version A');
  await expect(page.locator('#diff')).toContainText('version B');
  await expect(page.locator('#diff')).toContainText('before line');
  await expect(page.locator('#diff')).toContainText('after line');
  await expect(page.locator('#error')).toContainText('Renderer crashed at mount:');
  await expect(page.locator('#error')).toContainText('actual render failure');
  await expect(page.locator('#deferred')).toHaveText('deferred body');
  await expect.poll(()=>page.evaluate('window.runtimeProbe.ready')).toBe(2);
  expect(await page.evaluate('window.runtimeProbe.saves')).toEqual([]);
});

test('sibling URL follows the current path without text reads __F0259_F1883',async({page})=>{
  await page.goto('/__renderer_rollout/page');
  await page.evaluate(`(async()=>{
    const React=await import('https://esm.sh/react@19.3.1?target=es2022');const sdk=await import('/api/sdk/react.js');
    window.runtimeProbe={renders:[],textReads:0};
    const api={getContent:async()=>'',getFilename:()=>'probe.txt',isReadOnly:()=>false,onFilesChange:()=>()=>{},onSetContent:()=>()=>{},reportChange:()=>{},ready:()=>{},
      getFileUrl:path=>'https://files.example.invalid/'+encodeURIComponent(path),
      getSiblingFile:async()=>{window.runtimeProbe.textReads++;throw Error('binary sibling must not use text read');}};
    const component=()=>{const [path,setPath]=React.useState('first image.png');const file=sdk.useSiblingFile(path);
      window.runtimeProbe.renders.push({path,...file});
      return React.createElement('button',{'data-testid':'sibling-path',onClick:()=>setPath('next image.png')},file.url);};
    document.body.innerHTML='<div id="runtime-mount"></div>';
    window.runtimeProbe.dispose=await sdk.defineRenderer({component}).mount(document.getElementById('runtime-mount'),api);
  })()`);
  const button=page.getByTestId('sibling-path');
  await expect(button).toHaveText('https://files.example.invalid/first%20image.png');
  await button.click();
  await expect(button).toHaveText('https://files.example.invalid/next%20image.png');
  const renders=await page.evaluate<Array<{path:string;url:string;loading:boolean}>>('window.runtimeProbe.renders');
  expect(renders.length).toBeGreaterThanOrEqual(2);
  for(const render of renders)expect(render).toEqual({path:render.path,url:'https://files.example.invalid/'+encodeURIComponent(render.path),loading:false});
  expect(await page.evaluate('window.runtimeProbe.textReads')).toBe(0);
  await page.evaluate('window.runtimeProbe.dispose()');
});

for (const realm of ['native', 'iframe'] as const) {
  test(`React19 component shares the served runtime tree in ${realm} __F1869`,async({page})=>{
    await page.goto('/__renderer_rollout/page');
    const cdnRequests:string[]=[];
    page.on('request',request=>{if(request.url().startsWith('https://esm.sh/'))cdnRequests.push(request.url());});
    if(realm==='iframe')await page.evaluate(()=>{const frame=document.createElement('iframe');frame.src='/__renderer_rollout/page';document.body.append(frame);});
    const target=realm==='iframe' ? page.frameLocator('iframe') : page;
    const frame=realm==='iframe' ? await (await page.locator('iframe').elementHandle())!.contentFrame() : page;
    await frame!.evaluate(`(async()=>{
      const React=await import('https://esm.sh/react@19.3.1?target=es2022');const sdk=await import('/api/sdk/react.js');
      window.runtimeProbe={version:React.version,saves:[]};
      const api={getContent:async()=>'shared initial',getFilename:()=>'same.txt',isReadOnly:()=>false,
        onFilesChange:()=>()=>{},onSetContent:()=>()=>{},getSiblingFile:async()=>'',reportChange:text=>window.runtimeProbe.saves.push(text),ready:()=>{}};
      const component=()=>{const [count,setCount]=React.useState(0);const file=sdk.useCollimateFile();
        return React.createElement('button',{'data-testid':'react19-shared',onClick:()=>{setCount(count+1);file.saveContent('shared edit');}},file.content+':'+count);};
      const host=document.createElement('div');document.body.append(host);const shadow=host.attachShadow({mode:'open'});
      window.runtimeProbe.dispose=await sdk.defineRenderer({component}).mount(shadow,api);
    })()`);
    const control=target.getByTestId('react19-shared');
    await expect(control).toHaveText('shared initial:0',{timeout:3000});
    await control.click();await expect(control).toHaveText('shared edit:1');
    expect(await frame!.evaluate('window.runtimeProbe.version')).toBe('19.3.1');
    expect(await frame!.evaluate('window.runtimeProbe.saves')).toEqual(['shared edit']);
    expect(cdnRequests.some(url=>/react(?:-dom)?@18(?:[/?]|$)/.test(url))).toBe(false);
    await frame!.evaluate('window.runtimeProbe.dispose()');await expect(control).toHaveCount(0);
  });
}
