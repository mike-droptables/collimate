import { fileFetchCache } from './fileFetchCache';
import { getApiUrl } from './api/client';
import type { FileDescriptor } from './fileProtocol';
import type { VirtualFile } from './types';

export const descriptorOf = (file: VirtualFile): FileDescriptor => ({path:file.name,file_revision:file.file_revision,body_hash:file.body_hash,deleted:file.deleted??false,writer_id:file.writer_id,edit_seq:file.edit_seq,language:file.language,encoding:file.encoding});
export function pinnedFileUrl(channel:string,cellId:string,path:string,revision:number):string {
    return `${getApiUrl()}/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/files/${path.split('/').map(encodeURIComponent).join('/')}?revision=${revision}`;
}
export async function readPinnedText(channel:string,cellId:string,file:FileDescriptor,init?:RequestInit):Promise<string> {
    if(file.deleted)throw new Error(`File ${file.path} was deleted.`);
    if(file.encoding==='binary')throw new Error(`File ${file.path} contains binary bytes.`);
    if(!file.body_hash)throw new Error(`File ${file.path} has no admitted body.`);
    const cached=fileFetchCache.get<string>(`body:${file.body_hash}`);if(cached!==undefined)return cached;
    return fileFetchCache.fetch(`body:${file.body_hash}`,async()=>{
        const response=await fetch(pinnedFileUrl(channel,cellId,file.path,file.file_revision),{cache:'no-store',...init});
        if(!response.ok)throw new Error(`Could not read ${file.path}: HTTP ${response.status}`);
        if(Number(response.headers.get('X-File-Revision'))!==file.file_revision || response.headers.get('X-Body-Hash')!==file.body_hash)throw new Error('File revision changed during pinned read.');
        return response.text();
    });
}
