import { describe, expect, it } from 'vitest';
import { splitLevels } from './splitLevels';

const levelIds = (paths: string[], dirs: string[] = []) => splitLevels(paths, dirs, 'root').map(l => l.level);
const at = (level: number, paths: string[], dirs: string[] = [], rootName = 'root', repositories: string[] = []) => {
    const found = splitLevels(paths, dirs, rootName, repositories).find(l => l.level === level);
    if (!found) throw new Error(`level ${level} not offered`);
    return found;
};

describe('splitLevels: which levels a tree offers (TR-11)', () => {
    it('nothing to import offers nothing', () => {
        expect(levelIds([])).toEqual([]);
    });

    it('loose files only: one cell', () => {
        expect(levelIds(['a.txt', 'b.md'])).toEqual([0]);
    });

    it('a level needs a folder at its depth', () => {
        expect(levelIds(['a/x.txt'])).toEqual([0, 1]);
        expect(levelIds(['s/c/x.txt'])).toEqual([0, 1, 2]);
        expect(levelIds(['ch/s/c/x.txt'])).toEqual([0, 1, 2, 3]);
        expect(levelIds(['ch/s/c/d/x.txt'])).toEqual([0, 1, 2, 3]);
    });

    it('an empty folder counts for its depth', () => {
        expect(levelIds(['x.txt'], ['empty'])).toEqual([0, 1]);
        expect(levelIds([], ['s/c'])).toEqual([0, 1, 2]);
    });
});

describe('splitLevels: the stacks and cells each level makes (TR-11)', () => {
    const tree = ['notes.md', 'docs/a.md', 'docs/img/b.png', 'src/main.ts'];

    it('0: one cell named after the root, every path kept', () => {
        const level = at(0, tree, [], 'proj');
        expect(level.preview).toEqual({ stacks: [{ name: 'proj', cells: [{ name: 'proj', fileCount: 4 }] }] });
        expect(level.stacks[0].cells[0].files.map(f => f.path)).toEqual(tree);
    });

    it('1: each top folder a cell in one stack; loose files a cell named after their parent', () => {
        const level = at(1, tree, [], 'proj');
        expect(level.preview).toEqual({ stacks: [{ name: 'proj', cells: [
            { name: 'proj', fileCount: 1 },
            { name: 'docs', fileCount: 2 },
            { name: 'src', fileCount: 1 },
        ] }] });
        const docs = level.stacks[0].cells[1];
        expect(docs.files).toEqual([{ source: 'docs/a.md', path: 'a.md' }, { source: 'docs/img/b.png', path: 'img/b.png' }]);
    });

    it('2: top folders are stacks, their folders cells; loose files at each depth keep their own cell', () => {
        const level = at(2, ['readme.md', 'web/notes.md', 'web/home/index.html', 'web/home/css/a.css', 'api/server/main.py'], [], 'site');
        expect(level.preview).toEqual({ stacks: [
            { name: 'site', cells: [{ name: 'site', fileCount: 1 }] },
            { name: 'web', cells: [{ name: 'web', fileCount: 1 }, { name: 'home', fileCount: 2 }] },
            { name: 'api', cells: [{ name: 'server', fileCount: 1 }] },
        ] });
        expect(level.stacks[1].cells[1].files.map(f => f.path)).toEqual(['index.html', 'css/a.css']);
    });

    it("3: an all-export's channel folder is no stack; its stacks and cells are", () => {
        const level = at(3, ['ch/Main/First/a.txt', 'ch/Main/Second/b.txt', 'ch/Capabilities/Gen/gen.generator', 'ch/Main/loose.txt']);
        expect(level.preview).toEqual({ stacks: [
            { name: 'Main', cells: [{ name: 'First', fileCount: 1 }, { name: 'Second', fileCount: 1 }, { name: 'Main', fileCount: 1 }] },
            { name: 'Capabilities', cells: [{ name: 'Gen', fileCount: 1 }] },
        ] });
    });

    it('a loose-file cell and a folder cell of the same name stay apart', () => {
        const level = at(1, ['root.txt', 'root/inner.txt']);
        expect(level.preview.stacks[0].cells).toEqual([{ name: 'root', fileCount: 1 }, { name: 'root', fileCount: 1 }]);
    });

    it('3: two channel folders holding the same stack and cell names make two stacks of one cell each (TR-11)', () => {
        const level = at(3, ['A/Main/C/x.txt', 'B/Main/C/x.txt']);
        expect(level.preview).toEqual({ stacks: [
            { name: 'Main', cells: [{ name: 'C', fileCount: 1 }] },
            { name: 'Main', cells: [{ name: 'C', fileCount: 1 }] },
        ] });
        expect(level.stacks.map(s => s.cells[0].files)).toEqual([
            [{ source: 'A/Main/C/x.txt', path: 'x.txt' }],
            [{ source: 'B/Main/C/x.txt', path: 'x.txt' }],
        ]);
    });

    it("2: the root's loose files and a top folder named like the root stay two stacks", () => {
        const level = at(2, ['readme.md', 'site/home/index.html'], [], 'site');
        expect(level.preview).toEqual({ stacks: [
            { name: 'site', cells: [{ name: 'site', fileCount: 1 }] },
            { name: 'site', cells: [{ name: 'home', fileCount: 1 }] },
        ] });
    });

    it('a path reserved inside its cell (_runs/, .collrun) is left out of that level and listed (TR-08)', () => {
        const paths = ['a/x.txt', 'a/_runs/r1/out.txt', 'b/.collrun/stage.bin', '_runs/top.txt'];
        const dirs = ['a/_runs/empty', 'b/.collrun'];
        const one = at(1, paths, dirs);
        expect(one.leftOut.map(l => l.path)).toEqual(['a/_runs/r1/out.txt', 'b/.collrun/stage.bin', 'a/_runs/empty', 'b/.collrun']);
        expect(new Set(one.leftOut.map(l => l.reason))).toEqual(new Set(['Reserved for runs']));
        // A top folder named _runs is a cell, its files ordinary; b holds
        // nothing else, so it makes no cell.
        expect(one.stacks[0].cells).toEqual([
            { name: 'a', files: [{ source: 'a/x.txt', path: 'x.txt' }], dirs: [], repositories: [] },
            { name: '_runs', files: [{ source: '_runs/top.txt', path: 'top.txt' }], dirs: [], repositories: [] },
        ]);
        // At level 0 the same files sit under their top folder: only the
        // root's own _runs/ is reserved.
        expect(at(0, paths, dirs).leftOut).toEqual([{ path: '_runs/top.txt', reason: 'Reserved for runs' }]);
    });

    it('empty folders travel relative to their cell; an empty folder at the split depth is an empty cell', () => {
        const level = at(1, ['a/x.txt'], ['a/empty/deep', 'b']);
        expect(level.stacks[0].cells).toEqual([
            { name: 'a', files: [{ source: 'a/x.txt', path: 'x.txt' }], dirs: ['empty/deep'], repositories: [] },
            { name: 'b', files: [], dirs: [], repositories: [] },
        ]);
        expect(at(0, ['a/x.txt'], ['a/empty']).stacks[0].cells[0].dirs).toEqual(['a/empty']);
    });
});

// Each repository (a folder whose .git was split off) goes to the cell its
// folder lands in, at its folder inside that cell; one whose work tree the
// level spreads over several cells is left out (§7, import).
describe('splitLevels: repositories', () => {
    const tree = ['proj/a.txt', 'proj/src/main.ts', 'docs/x.md'];

    it('a repository at or below the split depth lands with its folder', () => {
        const zero = at(0, tree, [], 'root', ['proj', 'proj/vendor/lib']);
        expect(zero.stacks[0].cells[0].repositories).toEqual([
            { source: 'proj', repo_path: 'proj' },
            { source: 'proj/vendor/lib', repo_path: 'proj/vendor/lib' },
        ]);
        const one = at(1, tree, [], 'root', ['proj', 'proj/vendor/lib']);
        expect(one.stacks[0].cells.map(c => [c.name, c.repositories])).toEqual([
            ['proj', [{ source: 'proj', repo_path: '' }, { source: 'proj/vendor/lib', repo_path: 'vendor/lib' }]],
            ['docs', []],
        ]);
        expect(one.leftOut).toEqual([]);
    });

    it('the root repository is the one cell’s at level 0 and spans cells above it', () => {
        expect(at(0, tree, [], 'root', ['']).stacks[0].cells[0].repositories).toEqual([{ source: '', repo_path: '' }]);
        const one = at(1, tree, [], 'root', ['']);
        expect(one.leftOut).toEqual([{ path: '.git', reason: 'repository spans cells' }]);
        expect(one.stacks.flatMap(s => s.cells.flatMap(c => c.repositories))).toEqual([]);
        // Its files still import.
        expect(one.stacks[0].cells.flatMap(c => c.files.map(f => f.source))).toEqual(tree);
    });

    it('a repository above the split level spans cells; one holding only loose files keeps its loose cell', () => {
        const two = at(2, ['web/home/index.html', 'web/notes.md', 'api/readme.md'], [], 'site', ['web', 'api']);
        expect(two.leftOut).toEqual([{ path: 'web/.git', reason: 'repository spans cells' }]);
        const api = two.stacks.flatMap(s => s.cells).find(c => c.name === 'api')!;
        expect(api.repositories).toEqual([{ source: 'api', repo_path: '' }]);
        expect(api.files).toEqual([{ source: 'api/readme.md', path: 'readme.md' }]);
    });

    it('a folder that cannot hold a repository leaves it out with why', () => {
        const zero = at(0, ['node_modules/pkg/index.js'], [], 'root', ['node_modules/pkg']);
        expect(zero.leftOut).toEqual([{ path: 'node_modules/pkg/.git', reason: 'A repository cannot live at node_modules/pkg' }]);
    });

    it('a tree holding only a repository still offers one cell', () => {
        expect(splitLevels([], [], 'root', ['']).map(l => l.level)).toEqual([0]);
        expect(at(0, [], [], 'root', ['']).stacks[0].cells[0]).toEqual({ name: 'root', files: [], dirs: [], repositories: [{ source: '', repo_path: '' }] });
    });
});
