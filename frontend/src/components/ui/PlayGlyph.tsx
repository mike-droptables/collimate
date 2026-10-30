import React from 'react';
import type { LucideProps } from 'lucide-react';
import { cn } from '@/lib/utils';

/**
 * The mock's `i-play`: a small solid triangle in a 16-unit box, the Run
 * glyph of the runners panel and the Commands pane's "Run…" row. Takes the
 * icon props the lucide icons take, so it slots in wherever one does;
 * the stroke props are ignored (the triangle is filled).
 */
export const PlayGlyph = React.forwardRef<SVGSVGElement, Omit<LucideProps, 'ref'>>(
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    function PlayGlyph({ className, strokeWidth, absoluteStrokeWidth, size, color, ...rest }, ref) {
        return (
            <svg
                ref={ref}
                data-play-glyph
                viewBox="0 0 16 16"
                aria-hidden
                className={cn('flex-none', className)}
                {...rest}
            >
                <path d="M5 3.5v9l7-4.5z" fill="currentColor" />
            </svg>
        );
    },
);
