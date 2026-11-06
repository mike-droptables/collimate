import React, { useEffect } from 'react';
import { X } from 'lucide-react';
import { Button } from './button';
import { dialog } from './formStyles';
import { cn } from '@/lib/utils';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { useGuestOccluder } from '@/lib/guestOcclusion';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}

export const Modal: React.FC<ModalProps> = ({ isOpen, onClose, title, children }) => {
  useShortcut(SHORTCUT_IDS.GLOBAL_BACK, () => onClose(), {
    enabled: isOpen, skipWhenTyping: false,
  });
  // A native guest view would paint over this dialog; freeze guests to
  // their placeholders while it is up.
  useGuestOccluder('all', isOpen);
  useEffect(() => {
    if (isOpen) document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = 'unset'; };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-(--lift) p-4">
      <div
        className={cn(dialog, "w-full max-w-lg max-h-[90vh] flex flex-col overflow-hidden text-ink animate-in fade-in zoom-in-95 duration-200")}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-center justify-between h-9 pl-4 pr-2 border-b border-line">
          <h2 className="text-[0.84375rem] font-semibold text-ink">{title}</h2>
          <Button variant="ghost" size="icon-sm" onClick={onClose}>
            <X className="w-3.5 h-3.5" />
          </Button>
        </div>
        <div className="p-4 overflow-y-auto">
          {children}
        </div>
      </div>
    </div>
  );
};
