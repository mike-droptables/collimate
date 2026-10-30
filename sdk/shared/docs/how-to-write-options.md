# Writing split options

A run works in its own cell. To offer alternatives, write their files and an
`.options_stack` declaration; the user chooses Split. Merge also writes this
declaration. Read the selected declaration with `gen.options()`.

```yaml example=options-example
options:
  - name: First
    root: first
    files: ['**', '../brief.md']
  - name: Second
    root: second
    files: ['**', '../brief.md']
rest: keep
```

Paths are relative to the declaration's folder. A root is a literal folder,
even when its name contains glob characters. Patterns use `*` and `?` within
path components and `**` across components; brackets are literal. `..` may
move within the cell but cannot traverse an unresolved glob or escape the
cell. A matched file outside the chosen root must still lie below the
declaration's folder so it has a valid destination; otherwise Split refuses.
Every option must match at least one file, and declared roots must
exist. The declaration itself is consumed rather than copied to an option.

Files under an option's root lose that root prefix in its result; other
matched files keep their path relative to the declaration's folder. Options
may overlap. `rest: keep` retains unmatched files in the source cell,
`rest: drop` drops them, and `rest: each` copies them at their original cell
paths into every option. Protected files remain in the source. Conflicting
destination paths refuse Split rather than overwrite another selected file.

Write files first and the declaration last, then `validate` the declaration.
The accepted options handler checks its schema, roots and matches against the
current cell paths. Validation does not perform Split or approve adding a
definition to Capabilities.

## Literal root and pattern walkthrough

Each row is one option in a root-level declaration. Available paths are the
files before Split; output paths are the option's destinations. These are
mapping examples, with `rest: drop` and no protected files.

<!-- example=options-literal-walkthrough -->
| Root | Pattern | Available paths | Output paths |
|---|---|---|---|
| `copy[1]` | `**` | `copy[1]/a.txt, copy1/b.txt` | `a.txt` |
| `copy*` | `**` | `copy*/a.txt, copyOther/b.txt` | `a.txt` |
| `copy?` | `**` | `copy?/a.txt, copyX/b.txt` | `a.txt` |
| `folder` | `?.txt` | `folder/a.txt, folder/ab.txt` | `a.txt` |
| `folder` | `[x].txt` | `folder/[x].txt, folder/x.txt` | `[x].txt` |
| `folder` | `**/*.txt` | `folder/a.txt, folder/deep/b.txt` | `a.txt, deep/b.txt` |
