# INP Patching

`abqbatch` supports two patch styles.

## Anchor Patch

If an input contains markers such as:

```text
** @BEGIN AUTO_MATERIAL Steel
...
** @END AUTO_MATERIAL Steel
```

the content between markers is replaced with generated include text.

## Block Patch

If no anchor exists, the parser locates a keyword block such as
`*Material, name=Steel` and replaces that block with:

```text
*Include, input=material.inc
```

Loads are inserted before the target step's `*End Step` when no load anchor exists.

## Include Organization

Generated case input directories contain `generated.inp`, `material.inc`, `load.inc`,
`restart.inc`, and `output.inc`. This keeps generated changes small and auditable.

Raw `.inp` files are never overwritten.
