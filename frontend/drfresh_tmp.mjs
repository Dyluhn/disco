import { firefox } from 'playwright';
const SP = process.env.SP, CID = process.env.CID, TAG = process.env.TAG;
const b = await firefox.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
await p.goto(`http://localhost:5173/deep/${CID}`, { waitUntil: 'load' });
await p.waitForTimeout(4000);
await p.screenshot({ path: `${SP}/uisweep/dr_fresh_${TAG}.png` });
// grab the visible progress text for comparison
const txt = await p.evaluate(() => document.body.innerText.slice(0, 1200));
console.log('---SNAPSHOT', TAG, '---');
console.log(txt.split('\n').filter(l => /sub-question|source|Searching|Round|Synthesizing|plan|%|of /i.test(l)).slice(0, 10).join('\n'));
await b.close();
