import React from 'react';
import {Folder} from 'lucide-react';
import {getFileIcon} from './fileIcon';
import {cn} from '@/lib/utils';

interface FolderTileProps {
    identity:string;
    name:string;
    isFolder:boolean;
    selected:boolean;
    onSelect:(identity:string,event:React.MouseEvent)=>void;
    onOpen:(identity:string)=>void;
    draggable?:boolean;
    onDragStart?:(identity:string,event:React.DragEvent)=>void;
    onDragEnd?:()=>void;
    onDragOver?:(event:React.DragEvent)=>void;
    onDrop?:(identity:string,event:React.DragEvent)=>void;
}

/** Selection changes repaint the affected tiles, keeping the remaining icons mounted. */
export const FolderTile=React.memo(function FolderTile({identity,name,isFolder,selected,onSelect,onOpen,draggable,onDragStart,onDragEnd,onDragOver,onDrop}:FolderTileProps){
    return <div role="button" tabIndex={0} draggable={draggable}
        onDragStart={onDragStart?event=>onDragStart(identity,event):undefined}
        onDragEnd={onDragEnd}
        onDragOver={isFolder&&onDragOver?event=>{event.stopPropagation();onDragOver(event)}:undefined}
        onDrop={isFolder&&onDrop?event=>{event.stopPropagation();onDrop(identity,event)}:undefined}
        onClick={event=>onSelect(identity,event)}
        onDoubleClick={event=>{event.stopPropagation();if(isFolder)onOpen(identity)}}
        title={name}
        className={cn('relative flex flex-col items-center gap-1 p-2 rounded-md border transition-colors select-none outline-none cursor-pointer',selected?'bg-brass-soft border-brass':'border-transparent hover:bg-sheet-2 focus:bg-sheet-2')}>
        <div className="relative">{isFolder?<Folder className="w-9 h-9 text-ink-3"/>:<div className="scale-150">{getFileIcon('',name)}</div>}</div>
        <span className={cn('text-[0.6875rem] text-center leading-tight line-clamp-2 break-all',selected?'text-ink':'text-ink-2')}>{name}</span>
    </div>;
});
