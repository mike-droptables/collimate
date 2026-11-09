import { FileContextMenu, type FileMenuTarget } from './FileContextMenu';
import type { TreeNode } from '@/lib/fileTree';
import { parentOf } from '@/lib/fileTransfer';
import type { useFileTransfer } from '@/hooks/useFileTransfer';
type Transfer=Pick<ReturnType<typeof useFileTransfer>,'clipboardLabel'|'copyNode'|'cutNode'>;
interface Props {
    menu:{x:number;y:number;target:FileMenuTarget};readOnly:boolean;locked:boolean;transfer:Transfer;
    /** useFilesPanePaste's pasteHere; absent = no Paste item (no cell to write). */
    pasteHere?:(folder:string)=>void;
    cellId?:string;pinnedName?:string|null;onClose:()=>void;onOpen?:()=>void;onRename?:()=>void;
    onOpenFileWith?:(path:string,renderer:string|undefined)=>void;onNewFile:(path:string)=>void;onNewFolder:(path:string)=>void;
    onDelete:(node:TreeNode)=>void;onDuplicate:(node:TreeNode)=>void;onTogglePin?:(path:string)=>void;
    onExpandAll?:()=>void;onCollapseAll?:()=>void;
}
/** View adapters supply open/rename/lock rules; cell clipboard and file actions have one owner. */
export function CellFileContextMenu({menu,readOnly,locked,transfer,pasteHere,cellId,pinnedName,onClose,onOpen,onRename,onOpenFileWith,onNewFile,onNewFolder,onDelete,onDuplicate,onTogglePin,onExpandAll,onCollapseAll}:Props) {
    const {target}=menu;
    const node=target.kind==='empty'?null:target.node;
    const file=target.kind==='file'?target.node.fileData:null;
    return <FileContextMenu x={menu.x} y={menu.y} target={target} readOnly={readOnly} locked={locked} clipboardLabel={transfer.clipboardLabel} compareCellId={cellId} isPinned={!!file&&pinnedName===file.name} canPin={!!onTogglePin} onClose={onClose} onOpen={onOpen} onRename={onRename}
        onOpenWith={file&&onOpenFileWith?renderer=>onOpenFileWith(file.name,renderer):undefined}
        onNewFile={onNewFile} onNewFolder={onNewFolder}
        onDelete={node?()=>onDelete(node):undefined} onDuplicate={node?()=>onDuplicate(node):undefined}
        onCopy={node?()=>transfer.copyNode(node):undefined} onCut={node?()=>transfer.cutNode(node):undefined}
        onPaste={pasteHere?()=>pasteHere(target.kind==='empty'?target.basePath:target.node.type==='folder'?target.node.path:parentOf(target.node.path)):undefined}
        onTogglePin={file&&onTogglePin?()=>onTogglePin(file.name):undefined}
        onExpandAll={onExpandAll} onCollapseAll={onCollapseAll}/>
}
