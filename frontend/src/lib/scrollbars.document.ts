// The scrollbars for a document of their own: the sandbox document loads
// this as /scrollbars.js (scripts/build-scrollbars.mjs bundles it into
// public/), so an iframe renderer scrolls like the app.
import { adoptScrollbars, installScrollbars } from './scrollbars';

adoptScrollbars(document);
installScrollbars(document);
