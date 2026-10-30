import { fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { Profiler } from 'react';
import { render } from 'vitest-browser-react';
import { expect, it } from 'vitest';
import { OverviewCard } from '../overview/OverviewCard';
import { OVERVIEW_CARD_HEIGHT } from '@/stores/collimate/overviewSlice';

// The overview card's preview reads a bounded head of the open file (the
// first 14 lines of its first 4096 characters), so a card over a file at
// the stripped-size boundary costs what a small one does. Profiled across
// three mounts, as zoom-out mounts every card at once.
it('profiles an overview card over a large markdown file: the preview stays bounded __F0817 (O01)', async () => {
  const content = ('# Section\n\n- **bold** and `code`\n- [link](https://example.invalid)\n\n').repeat(900).slice(0, 50_000);
  const cell = { id: 'preview-profile', name: 'Preview', pinned: 'note.md', created_at: '2026-10-05T08:20:00', files: [
    fileFixture('note.md', content, {language: 'markdown', timestamp: 'stable'}),
  ] };
  const durations: number[] = [];
  let shown = '';
  for (let i = 0; i < 3; i++) {
    const view = await render(<Profiler id="overview-card" onRender={(_id, _phase, ms) => durations.push(ms)}>
      <div style={{ width: 272 }}>
        <OverviewCard cell={cell} selected={false} isOffscreen={false} />
      </div>
    </Profiler>);
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    const pre = view.container.querySelector('pre');
    shown = pre?.textContent ?? '';
    expect(view.container.querySelector('article')?.getBoundingClientRect().height).toBe(OVERVIEW_CARD_HEIGHT);
    await view.unmount();
  }
  expect(fixtureText(cell.files[0])).toBe(content);
  expect(shown.split('\n').length).toBeLessThanOrEqual(14);
  expect(shown.length).toBeLessThanOrEqual(4096);
  expect(shown.startsWith('# Section')).toBe(true);
  console.info('O01 overview card preview profile', JSON.stringify({ sourceBytes: content.length,
    previewChars: shown.length, mounts: 3, commits: durations.length,
    totalRenderMs: durations.reduce((sum, ms) => sum + ms, 0),
    maxRenderMs: Math.max(0, ...durations), renderMs: durations }));
});
