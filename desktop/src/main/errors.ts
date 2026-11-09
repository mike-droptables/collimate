/** Consistent user-facing text for arbitrary errors caught by the shell. */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
