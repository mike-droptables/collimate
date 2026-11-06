import { fileFixture } from '@/test/fileFixtures';
import {expect,it,vi} from 'vitest';
import {buildFileTree} from './fileTree';
import {lockOwnerUnder,nodeLockOwner} from './fileLocks';
it('ordinary5000-file folders skip run-lock callbacks and descendant walks __F0845',()=>{
 const files=Array.from({length:5000},(_,i)=>(fileFixture(`ordinary/file-${i}.txt`, '', {language:'text', timestamp:''})));const owner=vi.fn(()=>null);const tree=buildFileTree(files);expect(nodeLockOwner(tree.ordinary,owner)).toBeNull();expect(lockOwnerUnder(files.map(f=>f.name),'ordinary',true,owner)).toBeNull();expect(owner).not.toHaveBeenCalled();
});
