import { existsSync } from "node:fs";
import type { ElectronApplication, Page, TestInfo } from "@playwright/test";

/** Manual tracing: Electron's context is not Playwright's built-in page fixture. */
export class ShellTrace {
  private active = true;
  private bootstrap = true;
  private exported: string[] = [];
  private screenshot: string | undefined;

  private constructor(private readonly app: ElectronApplication) {}

  static async start(app: ElectronApplication): Promise<ShellTrace> {
    await app.context().tracing.start({ screenshots: true, snapshots: true, sources: true });
    return new ShellTrace(app);
  }

  async begin(info: TestInfo): Promise<void> {
    this.exported = [];
    this.screenshot = undefined;
    if (this.bootstrap) {
      await this.exportChunk(info.outputPath("bootstrap-trace.zip"));
      this.bootstrap = false;
    }
    await this.app.context().tracing.startChunk({ title: info.title });
    this.active = true;
  }

  private async exportChunk(file: string): Promise<void> {
    if (!this.active) return;
    await this.app.context().tracing.stopChunk({ path: file });
    this.active = false;
    this.exported.push(file);
  }

  /** Closing Electron destroys the context: retain evidence before that boundary. */
  async beforeClose(info: TestInfo, page?: Page): Promise<void> {
    if (page && !page.isClosed()) {
      const file = info.outputPath("screenshot.png");
      try {
        await page.screenshot({ path: file, timeout: 5_000 });
        this.screenshot = file;
      } catch (error) {
        // A closing/crashed page must not prevent the context trace export.
        await info.attach("screenshot capture error", { body: String(error), contentType: "text/plain" });
      }
    }
    await this.exportChunk(info.outputPath("trace.zip"));
  }

  async finish(info: TestInfo, page?: Page, bootstrapFailure = false): Promise<void> {
    const failed = bootstrapFailure || info.status !== info.expectedStatus;
    if (failed) {
      await this.beforeClose(info, page);
    } else {
      // Keep the exported chunk for explicit-close tests, otherwise discard it.
      if (this.active) {
        await this.app.context().tracing.stopChunk();
        this.active = false;
      }
    }
    if (failed) {
      for (const file of this.exported) {
        if (existsSync(file)) await info.attach(file.endsWith("bootstrap-trace.zip") ? "bootstrap trace" : "trace", {
          path: file, contentType: "application/zip",
        });
      }
      if (this.screenshot) await info.attach("screenshot", { path: this.screenshot, contentType: "image/png" });
    }
  }
}
