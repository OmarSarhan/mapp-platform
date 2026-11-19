import {defineConfig} from 'vite';
import {execFileSync} from 'node:child_process';
export default defineConfig({plugins:[{
  name:'baseline-source',
  load(id) {
    if (!id.endsWith('?baseline')) return;
    const file=id.split('?')[0].split('/').pop();
    if (!['main.jsx','api.js'].includes(file)) return;
    return execFileSync('git',['show',`5ac990aa8a8059c99314d2f56caf64bc3387fbbe:config-ui/src/${file}`],{encoding:'utf8'}).replace("from './api.js'", "from './api.js?baseline'");
  },
}],server:{host:'127.0.0.1',port:5179,strictPort:true}});
