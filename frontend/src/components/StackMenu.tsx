// The stack menu (C07): opened from the … button on a stack head and on a
// fanned stack's bracket. The shared Menu (ui/Menu) right-aligned under the
// button, 236px wide as the mock's `.stack-menu`; its rows come from the
// policy in stackMenuItems.ts.

import { Menu, type MenuCloseReason, type MenuEntry } from '@/components/ui/Menu';

interface StackMenuProps {
    /** The … button: positions the menu, takes focus back on Esc / Tab /
     *  activation, and is exempt from the outside-press close so its own
     *  click toggles. */
    anchorRef: React.RefObject<HTMLElement | null>;
    open: boolean;
    id: string;
    label: string;
    items: MenuEntry[];
    onClose: (reason: MenuCloseReason) => void;
}

export function StackMenu({ anchorRef, open, id, label, items, onClose }: StackMenuProps) {
    return (
        <Menu
            anchor={anchorRef}
            open={open}
            id={id}
            role="menu"
            label={label}
            items={items}
            align="end"
            minWidth={236}
            onClose={onClose}
        />
    );
}
