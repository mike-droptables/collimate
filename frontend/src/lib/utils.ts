import axios from "axios"
import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** The server's `detail` message from an axios-style error, if any. */
export function apiErrorDetail(e: unknown): string | undefined {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
}

/** True when an error is an aborted request (axios cancel / DOMException
 *  AbortError) rather than a real failure: the UI stays silent. */
export const isAbortError = (e: unknown): boolean =>
  axios.isCancel(e)
  || (e instanceof DOMException && e.name === 'AbortError')
  || (e as { name?: string })?.name === 'CanceledError'
