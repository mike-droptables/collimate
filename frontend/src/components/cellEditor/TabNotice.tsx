import React from 'react';

/** Centered, muted in-tab message: a history or compare tab with nothing to
 *  show. */
export const TabNotice: React.FC<{ title: string; sub?: string }> = ({ title, sub }) => (
    <div className="h-full flex flex-col items-center justify-center gap-1 px-6 text-center select-none">
        <span className="text-ink-2 text-xs font-mono italic">{title}</span>
        {sub && <span className="text-ink-3 text-[0.6875rem] font-mono italic">{sub}</span>}
    </div>
);
