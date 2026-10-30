import { expect, it } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import { LoaderCircle, TriangleAlert } from 'lucide-react';
import { MarkdownView } from './MarkdownView';
import { DynamicIcon } from './ui/DynamicIcon';

it('renders migrated Markdown and canonical status/group glyphs in the browser __F1861_F1862_F1854', async () => {
  const view = await render(<div>
    <LoaderCircle aria-label="Loading" size={24}/>
    <TriangleAlert aria-label="Warning" size={24}/>
    <DynamicIcon name="code2" aria-label="Stored code group"/>
    <MarkdownView className="dependency-markdown" content={'# Dependency seam\n\n| State | Result |\n| --- | --- |\n| ready | ~~old~~ **new** |\n\n[Docs](https://example.test/docs)\n\n- [x] done\n\n`<script>literal</script>`'}/>
  </div>);
  await expect.element(page.getByRole('heading', { name: 'Dependency seam' })).toBeVisible();
  await expect.element(page.getByRole('table')).toBeVisible();
  await expect.element(page.getByRole('checkbox')).toBeChecked();
  const link = document.querySelector('.dependency-markdown a');
  expect(link?.getAttribute('href')).toBe('https://example.test/docs');
  expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  expect(document.querySelector('.dependency-markdown code')?.textContent).toBe('<script>literal</script>');
  expect(document.querySelector('.dependency-markdown script')).toBeNull();
  for (const name of ['Loading', 'Warning', 'Stored code group']) {
    const glyph = document.querySelector(`svg[aria-label="${name}"]`)!;
    expect(glyph.getBoundingClientRect().width).toBe(24);
    expect(glyph.getBoundingClientRect().height).toBe(24);
    expect(glyph.querySelector('path,circle,rect,line,polyline')).not.toBeNull();
  }
  await view.unmount();
});
