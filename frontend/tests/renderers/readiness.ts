import type { Page } from '@playwright/test';
import { marker } from './fixtures';

/** Every probe observes the real shipped module's DOM, never fixture JS.
 * Frame traversal includes the actual pooled host and nested document frame.
 * Playwright CSS locators pierce native open shadow roots.
 */
export async function ready(page: Page, id: string): Promise<boolean> {
  for (const frame of page.frames()) {
    try {
      switch (id) {
        case 'text':
          if (await frame.locator('textarea').filter({ visible: true }).first().inputValue().catch(() => '') === marker(id)) return true;
          break;
        case 'image':
          if (await frame.locator('img[src*="sample.png"]').evaluateAll(nodes => nodes.some(n => (n as HTMLImageElement).complete && (n as HTMLImageElement).naturalWidth > 0))) return true;
          break;
        case 'audio': case 'video':
          if (await frame.locator(id).evaluateAll(nodes => nodes.some(n => {
            const media = n as HTMLMediaElement;
            return media.readyState >= 2 && Number.isFinite(media.duration) && media.duration > 0 && !media.error
              && (media.tagName !== 'VIDEO' || (media as HTMLVideoElement).videoWidth > 0);
          }))) return true;
          break;
        case 'schema_form':
          if (await frame.getByText('Greeting', { exact: true }).count()
            && await frame.locator('input').evaluateAll(nodes => nodes.some(n => (n as HTMLInputElement).value === 'hello'))) return true;
          break;
        case 'pdf':
          // The EmbedPDF renderer: RenderLayer's page image (a blob <img>)
          // has loaded. Its view controls live in the corner popover.
          if (await frame.locator('img[src^="blob:"]').evaluateAll(nodes => nodes.some(n => (n as HTMLImageElement).complete && (n as HTMLImageElement).naturalWidth > 0))) return true;
          break;
        case 'excalidraw':
          if (await frame.locator('.excalidraw canvas').count() && await frame.locator('.excalidraw').isVisible()) return true;
          break;
        case 'monaco':
          if (await frame.locator('.monaco-editor .view-lines').filter({ hasText: marker(id) }).count()) return true;
          break;
        case 'milkdown':
          if (await frame.locator('.ProseMirror').filter({ hasText: marker(id) }).count()) return true;
          break;
        case 'mermaid':
          if (await frame.locator('svg').filter({ hasText: marker(id) }).count()) return true;
          break;
        case 'website': case 'webview':
          if (await frame.getByRole('heading', { name: 'P3 local page ready', exact: true }).count()) return true;
          break;
        case 'terminal':
          if (await frame.locator('.xterm-screen').count() && await frame.locator('.xterm-helper-textarea').count()) return true;
          break;
        case 'assistant':
          if (await frame.getByText('P3 assistant transcript ready', { exact: true }).count()) return true;
          break;
        case 'renderer_example': {
          // The local candidate reads sample.txt through the preview relay.
          // Outer renderer chrome or an empty iframe is not readiness.
          if (frame === page.mainFrame()) break;
          const host = await frame.frameElement();
          if (await host.getAttribute('title') !== 'Sealed renderer preview'
            || await host.getAttribute('sandbox') !== 'allow-scripts') break;
          if (await frame.getByRole('heading', { name: marker(id), exact: true }).isVisible()) return true;
          break;
        }
        default:
          if (await frame.getByText(marker(id), { exact: true }).count()) return true;
      }
    } catch {
      // Frame creation/navigation can detach a previous document. Poll the
      // current real frames again; exceptions never count as readiness.
    }
  }
  return false;
}
