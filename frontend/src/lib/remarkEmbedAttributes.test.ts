import { describe, it, expect } from 'vitest';
import { embedStyle, parseEmbedBlock } from './remarkEmbedAttributes';

describe('parseEmbedBlock', () => {
    it('reads width and align in either order', () => {
        expect(parseEmbedBlock('{width=60% align=center} rest')).toEqual({ width: 60, align: 'center', length: 24 });
        expect(parseEmbedBlock('{align=right width=5%}')).toEqual({ width: 5, align: 'right', length: 22 });
        expect(parseEmbedBlock('{align=left}')).toEqual({ width: null, align: 'left', length: 12 });
    });

    it('rejects anything outside the grammar', () => {
        for (const text of [
            '{}', '{ }', '{foo}', '{width=0%}', '{width=101%}', '{width=50}', '{width=1000%}',
            '{align=top}', '{width=50% width=60%}', '{align=left align=right}', '{width=50% x}',
            ' {width=50%}', '{width=50%', '{width=50%\n}', 'width=50%',
        ]) expect(parseEmbedBlock(text), text).toBeNull();
    });
});

describe('embedStyle', () => {
    it('sizes, centres and right-aligns a lone image on its own line', () => {
        expect(embedStyle({ width: 50, align: 'center' }, true)).toBe('width:50%;display:block;margin-left:auto;margin-right:auto');
        expect(embedStyle({ width: null, align: 'right' }, true)).toBe('display:block;margin-left:auto');
        expect(embedStyle({ width: 30, align: 'left' }, true)).toBe('width:30%');
    });

    it('floats an image inside text', () => {
        expect(embedStyle({ width: 25, align: 'left' }, false)).toBe('width:25%;float:left');
        expect(embedStyle({ width: null, align: 'right' }, false)).toBe('float:right');
    });
});
