import fs from 'fs';
import path from 'path';

const root = process.cwd();
const checks = [
  ['package.json', path.join(root, 'package.json')],
  ['capacitor.config.ts', path.join(root, 'capacitor.config.ts')],
  ['www/index.html', path.join(root, 'www', 'index.html')],
  ['android dir', path.join(root, 'android')],
];

for (const [label, file] of checks) {
  console.log(`${fs.existsSync(file) ? 'OK ' : 'MISS'} ${label}: ${file}`);
}

console.log('\nExpected remote app target: https://baysoko.up.railway.app/?source=android_app');
