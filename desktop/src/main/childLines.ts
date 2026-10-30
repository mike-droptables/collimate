/**
 * Split a stream of text chunks into lines, the way the server log wants
 * them: a line split across two chunks is joined, CRLF and LF both end a
 * line, blank lines are kept, and the partial after the last newline is
 * held until the rest arrives (or `end()` flushes it).
 */
export interface LineSplitter {
  push(chunk: string): void;
  /** Emit whatever partial line is still held. */
  end(): void;
}

/** Longest partial held before it is emitted as a line of its own. */
export const MAX_PARTIAL_LINE = 8192;

export function createLineSplitter(emit: (line: string) => void): LineSplitter {
  let partial = '';
  return {
    push(chunk: string): void {
      const lines = (partial + chunk).split(/\r?\n/);
      // Last element is whatever came after the final newline — hold it
      // until the rest arrives so progress bars and partial writes don't
      // get split mid-line in the tail.
      partial = lines.pop() ?? '';
      for (const line of lines) emit(line);
      // Don't let output that never carries a newline (a spinner) grow
      // the buffer without bound.
      if (partial.length > MAX_PARTIAL_LINE) {
        emit(partial);
        partial = '';
      }
    },
    end(): void {
      if (!partial) return;
      emit(partial);
      partial = '';
    },
  };
}
