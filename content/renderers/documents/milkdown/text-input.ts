// Chromium's native contenteditable insertion becomes very slow in paragraphs
// containing thousands of inline nodes. Insert ordinary text through the same
// public transaction/input-rule path ProseMirror uses for complex selections.
// Composition, browser replacements, paste, and non-text selections retain
// their native handlers.
export function transactionTextInput(TextSelection) {
    return (view, event) => {
        if (!event.cancelable || event.defaultPrevented || event.inputType !== 'insertText'
            || !event.data || /[\r\n]/.test(event.data) || event.isComposing || view.composing
            || !view.editable) return false;
        const selection = view.state.selection;
        if (!(selection instanceof TextSelection) || !selection.$from.sameParent(selection.$to)) return false;
        const text = event.data;
        const from = selection.from, to = selection.to;
        const defaultTransaction = () => view.state.tr.insertText(text, from, to).scrollIntoView();
        if (!view.someProp('handleTextInput', handler => handler(view, from, to, text, defaultTransaction))) {
            view.dispatch(defaultTransaction());
        }
        event.preventDefault();
        return true;
    };
}
