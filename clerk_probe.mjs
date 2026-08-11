import { readFileSync } from 'fs';

const env = readFileSync('.env', 'utf8');
const m = env.match(/^CLERK_SECRET_KEY=.*$/m);
const SK = m[1].trim().replace(/^["']|["']$/g, '');

const h = { Authorization: 'Bearer ' + SK, 'Content-Type': 'application/json' };

console.log('Key prefix:', SK.slice(0, 10), '...len', SK.length);

// 1. List current domains
console.log('\n=== Current domains ===');
const doms = await (await fetch('https://api.clerk.com/v1/domains', { headers: h })).json();
console.log(JSON.stringify(doms, null, 2));

const satellite = doms.data && doms.data.find(d => d.name === 'restorepdfbooks.com');
const primary = doms.data && doms.data.find(d => !d.is_satellite);
console.log('\nPrimary:', primary && primary.id, primary && primary.name);
console.log('Satellite:', satellite && satellite.id, satellite && satellite.name);

// 2. Try PATCH satellite is_satellite:false (demote to non-satellite)
if (satellite) {
  console.log('\n=== PATCH satellite is_satellite=false ===');
  const r1 = await fetch('https://api.clerk.com/v1/domains/' + satellite.id, {
    method: 'PATCH', headers: h,
    body: JSON.stringify({ is_satellite: false }),
  });
  console.log('status', r1.status);
  console.log(await r1.text());
}
