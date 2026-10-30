import { useContext, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { railBtn, railBtnOff, railToggleOn } from '@/components/ui/formStyles';
import { RunsPaneChromeContext } from './paneChrome';

/**
 * A status pane view's rail: 30px, on the tab strip's and the Files rail's
 * icon line (0.25rem above its bottom, the 1px border included), its lead
 * (back, title, Stop) then the Hide toggle at the far end, an X (a
 * right-click on it docks the pane on its other side, a drag on the nearer
 * edge).
 */
export function PaneRail({ children }: { children?: ReactNode }) {
    const chrome = useContext(RunsPaneChromeContext);
    return (
        <div className="flex-none h-[1.875rem] flex items-end gap-0.5 pb-[calc(0.25rem-1px)] px-1.5 border-b border-line select-none">
            <div className="min-w-0 flex-1 flex items-end gap-1">{children}</div>
            {chrome && (
                <span
                    data-runs-dock-handle
                    className="inline-flex ml-auto flex-none"
                    title={chrome.hideTitle}
                    onContextMenu={(e) => { e.preventDefault(); chrome.cycleSide(); }}
                    {...chrome.dockDrag}
                >
                    <button
                        type="button"
                        aria-label="Hide status"
                        aria-pressed
                        title={chrome.hideTitle}
                        onClick={chrome.close}
                        className={cn(railBtn, railBtnOff, railToggleOn)}
                    >
                        <X className="w-3.5 h-3.5" strokeWidth={1.6} />
                    </button>
                </span>
            )}
        </div>
    );
}
