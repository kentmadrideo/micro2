const fs = require('fs');
const path = require('path');
const files = ['public/dashboard1.html','public/dashboard2.html','public/dashboard3.html','public/dashboard4.html','public/dashboard5.html','public/dashboard_camera.html','public/dashboard_all.html'];
for (const file of files) {
  const data = fs.readFileSync(file, 'utf8');
  const match = data.match(/<nav class="nav-bar">([\s\S]*?)<\/nav>/);
  console.log(`\n== ${file} ==\n`);
  if (!match) {
    console.log('NO NAV');
    continue;
  }
  const block = match[0];
  console.log(block);
}