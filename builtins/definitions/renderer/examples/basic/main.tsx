import { defineRenderer } from '/api/sdk/react.js';
const Component = () => <div style={{padding: 8}}>Example</div>;
export default defineRenderer({ component: Component });
