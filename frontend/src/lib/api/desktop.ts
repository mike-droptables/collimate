import type { ClientHandshake } from '@/lib/desktop';
import { requestJson } from './client';


/** The server's record of its client. Capability keys are snake_case here —
 *  the server's shape, not the shell's camelCase wire shape. */
export interface ClientRecord {
    client: string;
    shell_version: string | null;
    bridge: number | null;
    capabilities: Record<string, boolean>;
    /** 'env' = spawned-with-this-kind but not yet confirmed; 'handshake' = posted. */
    source: 'env' | 'handshake';
}


// Tell the server which client is driving it. Posted on boot by browser
// tabs too, not just the shell: the server may have been spawned with
// COLLIMATE_CLIENT=electron and only the page knows it's really a browser.
export const postClientHandshake = async (body: ClientHandshake): Promise<ClientRecord> =>
    requestJson('/system/client', 'POST', body);
