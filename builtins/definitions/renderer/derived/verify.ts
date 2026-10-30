import { parseYaml, parseJson, validateSchema, rendererSchemas, bundle, checkRendererExport,
 type VerifyInput, type Verdict } from 'collimate/verify';
const relative=(path:string)=> !!path && !path.startsWith('/') && !path.includes('\\') && !path.includes('\0')
 && ! /^[A-Za-z]:/.test(path) && path.split('/').every(part=>part!=='' && part!=='.' && part!=='..' && part!=='_runs');

export default function verify(input:VerifyInput):Verdict {
 try {
  if(typeof input.text!=='string') return {valid:false,error:'definition must be UTF-8 text'};
  if(input.path.toLowerCase().endsWith('.renderer_example')) {
   const value=parseJson(input.text) as {renderer:string,file:string};
   const verdict=validateSchema(rendererSchemas.example,value);if(!verdict.valid)return verdict;
   if(!relative(value.renderer) || !value.renderer.toLowerCase().endsWith('.renderer') || !relative(value.file))
    return {valid:false,error:'preview must name relative renderer and file paths'};
   if(!input.cell_paths.includes(value.renderer) || !input.cell_paths.includes(value.file))
    return {valid:false,error:'preview renderer or sample file does not exist in the cell'};
   return {valid:true};
  }
  if(!input.path.toLowerCase().endsWith('.renderer'))return {valid:false,error:'unsupported renderer handler type'};
  const manifest=parseYaml(input.text) as {entrypoint:string};
  const verdict=validateSchema(rendererSchemas.renderer,manifest);if(!verdict.valid)return verdict;
  if(!relative(manifest.entrypoint))return {valid:false,error:'entrypoint must stay inside the package'};
  const files:Record<string,string>={};
  for(const [path,value] of Object.entries(input.package))if(typeof value==='string')files[path]=value;
  if(!(manifest.entrypoint in files))return {valid:false,error:'entrypoint text is unavailable in the package'};
  const compiled=bundle(files,manifest.entrypoint);
  checkRendererExport(compiled.entrypoint_js);
  return {valid:true};
 } catch(error) {return {valid:false,error:error instanceof Error ? error.message : 'renderer verification failed'};}
}
