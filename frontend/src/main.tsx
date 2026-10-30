import { installRendererInputContainment } from './lib/rendererInputContainment'
import { adoptScrollbars, installScrollbars } from './lib/scrollbars'
// Must run before any renderer module can load: patches the event surfaces
// so input listeners registered from renderer bundles are focus-gated.
installRendererInputContainment()
// The workbench's scrollbars (lib/scrollbars): the sheet on this document,
// the activity mark on whichever scroller moves or receives the pointer, and
// the thumb drag; each renderer shadow root and the sandbox get their own.
adoptScrollbars(document)
installScrollbars(document)

import React from 'react'
import ReactDOM from 'react-dom/client'
import './app/globals.css'
import { GuestPoolProvider } from './contexts/GuestPoolContext'
// Applies the cached editing preferences (spellcheck) to <html> before the
// first paint; the store keeps <html> and the renderer hosts in step after.
import './stores/useEditingStore'
// Applies the cached renderer styles before the first renderer mounts; the
// store keeps the renderer hosts in step after.
import './stores/useRendererStylesStore'
import { MovingProvider } from './contexts/MovingContext'
import StackApp from './components/StackApp'

// The PWA machinery is gone: the installable offering is the desktop app,
// and the browser tier is a plain web page. What remains is cleanup for
// browsers that installed the old workbox service worker — unregister it
// so no stale precache can ever serve an old UI against a newer server.
// Gated on the runtime config snippet FastAPI injects, so the cleanup only
// runs against a served build.
if (
  typeof window !== 'undefined' &&
  window.__COLLIMATE_CONFIG__ &&
  'serviceWorker' in navigator
) {
  void navigator.serviceWorker
    .getRegistrations()
    .then((registrations) => registrations.forEach((r) => void r.unregister()))
    .catch(() => { /* cleanup is best-effort */ })
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <MovingProvider>
    <GuestPoolProvider>
      <StackApp />
    </GuestPoolProvider>
  </MovingProvider>
)
