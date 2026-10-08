const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const python=process.env.FLOWLENS_TEST_PYTHON || [
  path.join(root,'runtime','python.exe'),
  path.join(root,'.venv','Scripts','python.exe'),
  path.join(root,'.venv','bin','python'),
].find(candidate=>fs.existsSync(candidate)) || 'python';
module.exports={python};
