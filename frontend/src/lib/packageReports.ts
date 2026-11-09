/** A source package skipped by approval; imported files are never rendered as HTML. */
export interface PackageSkip { cell_id: string; path: string; id: string; reason: string }
export async function reportPackageSkips(skipped: readonly PackageSkip[]): Promise<void> {
    if (!skipped.length) return;
    const { useCollimateStore } = await import('@/stores/useCollimateStore');
    const lines = skipped.map(item => `${item.path} (${item.id}): ${item.reason}`);
    await useCollimateStore.getState().openAlert({ title: 'Packages skipped', body: lines.join('\n') });
}

/** A copied or uploaded repository whose git history a new channel did not
 *  keep (`POST /api/channels`'s `skipped`): its files came across. */
export interface RepositorySkip { path: string; reason: string }
export async function reportRepositorySkips(skipped: readonly RepositorySkip[] | undefined): Promise<void> {
    if (!skipped?.length) return;
    const { useCollimateStore } = await import('@/stores/useCollimateStore');
    const lines = skipped.map(item => `${item.path}: ${item.reason}`);
    await useCollimateStore.getState().openAlert({ title: 'Repository history not kept', body: lines.join('\n') });
}
