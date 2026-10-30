import { expect, type APIRequestContext } from '@playwright/test';

/** Create a channel seeded with the Standard suggestion's generators and
 *  renderers, on its Capabilities stack after the app's built-in cards: the
 *  server reads its suggested catalog from the content installed with it
 *  (its default source), and the channel starts from the "standard"
 *  suggestion, without explanation cells or runners. Returns the new
 *  channel's id. */
export async function createSeededChannel(request: APIRequestContext, name: string): Promise<string> {
  const created = await request.post(`/api/channels?name=${encodeURIComponent(name)}`, {
    data: { suggested: { id: 'standard', tutorial: false, runners: [] } },
  });
  expect(created.ok(), await created.text()).toBe(true);
  const { id } = await created.json();
  expect(id).toMatch(/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/);
  return id;
}
