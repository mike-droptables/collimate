import { cleanup } from '@testing-library/react';
import { expect, it } from 'vitest';
import { expectFitsPaneColumn, renderInPaneColumn, SECTION_CONTENT_PX } from './paneColumn.testkit';

it('checks rem utilities and inline widths against the native Commands pane column', () => {
    expect(SECTION_CONTENT_PX).toBeCloseTo(321.2);

    const fitting = renderInPaneColumn(<div className="w-[18rem]" style={{ minWidth: '18rem' }} />);
    expect(() => expectFitsPaneColumn(fitting.body)).not.toThrow();
    cleanup();

    const tooWideClass = renderInPaneColumn(<div className="w-[19rem]" />);
    expect(() => expectFitsPaneColumn(tooWideClass.body)).toThrow();
    cleanup();

    const tooWideInline = renderInPaneColumn(<div style={{ width: '19rem' }} />);
    expect(() => expectFitsPaneColumn(tooWideInline.body)).toThrow();
});
