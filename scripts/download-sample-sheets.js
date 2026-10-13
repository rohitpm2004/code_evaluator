import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const targetDir = path.resolve(__dirname, '../../excel-submissions-test');

if (!fs.existsSync(targetDir)) {
  fs.mkdirSync(targetDir, { recursive: true });
}

const sheets = [
  {
    name: 'sheet-1-sales-pivot.xlsx',
    url: 'https://docs.google.com/spreadsheets/d/1KO09PjXZgrrfQNp0617SPxs96lc_utxO/export?format=xlsx'
  },
  {
    name: 'sheet-2-student-grades.xlsx',
    url: 'https://docs.google.com/spreadsheets/d/1mYYtlGyArLtBXG9MIKGOWx5mwP11vNWR/export?format=xlsx'
  },
  {
    name: 'sheet-3-profit-margin.xlsx',
    url: 'https://docs.google.com/spreadsheets/d/1IyKPyzR79tlMyDQdxi2NcXEPsL0iBVBa/export?format=xlsx'
  }
];

async function downloadSheets() {
  console.log('Downloading sample sheets to:', targetDir);
  for (const s of sheets) {
    const dest = path.join(targetDir, s.name);
    try {
      console.log(`Downloading ${s.name}...`);
      const res = await fetch(s.url, { redirect: 'follow' });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const buffer = Buffer.from(await res.arrayBuffer());
      fs.writeFileSync(dest, buffer);
      console.log(`Saved ${s.name} (${buffer.length} bytes)`);
    } catch (err) {
      console.error(`Failed to download ${s.name}:`, err.message);
    }
  }
}

downloadSheets();
