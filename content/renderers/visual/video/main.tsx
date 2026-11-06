import React from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, MediaViewer } from '/api/sdk/react.js';

function VideoViewer() {
    return <MediaViewer kind="video"/>;
}

export default defineRenderer({ component: VideoViewer });
