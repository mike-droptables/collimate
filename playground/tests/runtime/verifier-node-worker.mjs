import {parentPort} from 'node:worker_threads';
globalThis.self = globalThis;
const {dispatch} = await import('../verifier-worker.js');
parentPort.on('message', async request => {
  try {parentPort.postMessage({id:request.id,ok:true,result:await dispatch(request)});}
  catch(error) {parentPort.postMessage({id:request.id,ok:false,error:error.message});}
});
