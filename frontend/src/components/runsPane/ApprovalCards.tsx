import { cn } from '@/lib/utils';
import { btn, btnPrimary } from '@/components/ui/formStyles';
import type { PendingPermission } from '@/lib/runInbox';

export interface ApprovalCardsProps {
    cards: readonly PendingPermission[];
    /** An option id, or null to cancel the card. */
    onAnswer: (id: string, optionId: string | null) => boolean;
}

/** A live run's open approval cards (its inbox, hooks/useRunInbox), each
 *  with its options and Cancel; brass, since they wait on the user. */
export function ApprovalCards({ cards, onAnswer }: ApprovalCardsProps) {
    if (cards.length === 0) return null;
    return (
        <div data-testid="approval-cards" className="flex-none max-h-[45%] overflow-y-auto border-t border-line px-2 py-1.5 space-y-1.5">
            {cards.map(card => (
                <div key={card.id} data-testid="approval-card" data-id={card.id} className="rounded-lg border border-line border-l-2 border-l-brass bg-sheet p-2">
                    <div className="text-[0.8125rem] font-semibold text-ink">{card.title}</div>
                    {card.detail && (
                        <pre className="mt-1 mb-0 max-h-32 overflow-auto font-mono text-[0.6875rem] leading-[1.5] text-ink-2 whitespace-pre-wrap break-words">{card.detail}</pre>
                    )}
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {card.options.map(option => (
                            <button
                                key={option.option_id}
                                type="button"
                                data-testid="approval-option"
                                data-option-id={option.option_id}
                                onClick={() => onAnswer(card.id, option.option_id)}
                                className={cn(option.kind.startsWith('allow') ? btnPrimary : btn, 'flex-none')}
                            >
                                {option.name || option.option_id}
                            </button>
                        ))}
                        <button type="button" data-testid="approval-cancel" onClick={() => onAnswer(card.id, null)} className={cn(btn, 'flex-none')}>
                            Cancel
                        </button>
                    </div>
                </div>
            ))}
        </div>
    );
}
