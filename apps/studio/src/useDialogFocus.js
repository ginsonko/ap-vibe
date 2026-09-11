import {useEffect, useRef} from 'react';

// Keep keyboard navigation in an open dialog and restore the launch control.
export function useDialogFocus(close, busy = false, enabled = true) {
  const dialog = useRef(null), latest = useRef({close, busy});
  latest.current = {close, busy};
  useEffect(() => {
    if (!enabled) return;
    const previous = document.activeElement;
    const root = dialog.current;
    if (!root) return;
    const focusable = () => [...root.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')]
      .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    (root.querySelector('[data-dialog-initial-focus]') || focusable()[0] || root).focus();
    const keydown = event => {
      if (event.key === 'Escape' && !latest.current.busy) {
        event.preventDefault(); latest.current.close();
      }
      if (event.key !== 'Tab') return;
      const controls = focusable(), first = controls[0], last = controls.at(-1);
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    root.addEventListener('keydown', keydown);
    return () => {
      root.removeEventListener('keydown', keydown);
      document.body.style.overflow = oldOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [enabled]);
  return dialog;
}
