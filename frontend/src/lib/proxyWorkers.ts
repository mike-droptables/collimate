/** Only registrations for a run's proxy paths belong to its lifetime. */
type RunWorker = { runId: string; registration: ServiceWorkerRegistration };

export async function proxyWorkers(channel: string): Promise<RunWorker[]> {
  try {
    if (!navigator.serviceWorker) return [];
    const registrations = await navigator.serviceWorker.getRegistrations();
    return registrations.flatMap(registration => {
      const scope = new URL(registration.scope);
      const path = /^\/proxy\/([^/]+)\/([^/]+)\/([0-9]+)\//.exec(scope.pathname);
      if (scope.origin !== location.origin || !path
          || decodeURIComponent(path[1]) !== channel) return [];
      return [{runId: decodeURIComponent(path[2]), registration}];
    });
  } catch {
    // Unsupported/disabled workers leave nothing the host can unregister.
    return [];
  }
}

export async function removeProxyWorkers(workers: RunWorker[]): Promise<void> {
  // A failed unregister must not prevent cleanup of another port's worker.
  await Promise.allSettled(workers.map(({registration}) => registration.unregister()));
}

export async function removeRunProxyWorkers(channel: string, runId: string): Promise<void> {
  await removeProxyWorkers((await proxyWorkers(channel)).filter(worker => worker.runId === runId));
}
