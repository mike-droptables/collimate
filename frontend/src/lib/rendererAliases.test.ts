import {beforeEach,expect,it} from 'vitest';
import {rendererFilename,rendererRealPath,rendererChangedFiles,setRendererHandlers} from './rendererAliases';
beforeEach(()=>setRendererHandlers([]));
it('case-folds suffixes once while retaining exact original path boundaries __D11',()=>{
 setRendererHandlers([{types:[{ext:'.strasse',render_as:'.HTML'},{ext:'.html',render_as:'.md'}]}]);
 expect(rendererFilename('Über/name.STRAẞE')).toBe('Über/name.html');
 expect(rendererFilename('Upper/Name.HTML')).toBe('Upper/Name.md');
 expect(rendererFilename('plain.txt')).toBe('plain.txt');
});
it('lets longest plain or clashing types block a shorter alias __D11',()=>{
 setRendererHandlers([{types:[{ext:'.doc',render_as:'.html'},{ext:'.plain.doc'}]},{types:[{ext:'.clash.doc',render_as:'.md'}]},{types:[{ext:'.CLASH.DOC',render_as:'.txt'}]}]);
 expect(rendererFilename('x.doc')).toBe('x.html');
 expect(rendererFilename('x.plain.doc')).toBe('x.plain.doc');
 expect(rendererFilename('x.clash.doc')).toBe('x.clash.doc');
 setRendererHandlers([{disabled:true,types:[{ext:'.doc',render_as:'.html'}]}]);
 expect(rendererFilename('x.doc')).toBe('x.doc');
});
it('maps only the exact own alias and own change notification __D11',()=>{
 const real='dir/NAME.page_html',display='dir/NAME.html';
 expect(rendererRealPath(real,display,display)).toBe(real);
 expect(rendererRealPath(real,display,'dir/another.html')).toBe('dir/another.html');
 expect(rendererRealPath(real,display,'dir/NAME.comments')).toBe('dir/NAME.comments');
 expect(rendererChangedFiles(real,display,[real,'dir/another.html'])).toEqual([display,'dir/another.html']);
});
it('keeps unclashed aliases of partially clashing handlers and skips disabled claims __D09_D11_P3F11',()=>{
 setRendererHandlers([{types:[{ext:'.long.foo',render_as:'.html'},{ext:'.bar',render_as:'.md'}],clashes:['.LONG.FOO']},{types:[{ext:'.foo',render_as:'.txt'}]},{disabled:true,types:[{ext:'.bar',render_as:'.html'}]}]);
 expect(rendererFilename('sample.long.foo')).toBe('sample.long.foo');
 expect(rendererFilename('sample.BAR')).toBe('sample.md');
 expect(rendererFilename('sample.foo')).toBe('sample.txt');
});
