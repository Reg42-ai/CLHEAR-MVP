# L1 clause-boundary golden set

Gate `l1_boundary_f1` (HLD v2 §4.1: clause boundary F1 ≥ 0.98) runs every
`*.json` case here through the adapter's **offline** parser and scores clause
boundaries against the expected clauses.

Case format:

```json
{
  "adapter": "fca_handbook",          // adapter key (fleet schedule name)
  "sourcebook": "PRIN",               // adapter-specific constructor args (optional)
  "channel": "finra",                 // sec_edgar / finra channel (optional)
  "source_key": "fca/handbook",
  "title": "…", "url": "…",
  "html": "<html>…</html>",           // OR
  "pages": ["page 1 text", "…"],      // text pages for PDF publishers
  "clauses": [ {"ref": "PRIN 2.1.1", "text": "…"} ]
}
```

A clause matches when its `ref` **and** whitespace-normalised text agree with
the parsed clause (`node.subtree_text()`). The reported `f1` is the exact
(ref + text) score; `refs` is the ref-only score for diagnosis.

Rules for authoring cases:

* Fixtures are hand-written excerpts that mirror the publisher's real markup
  or page layout (rule numbering, status letters, headings). Never paste
  licensed text beyond what the rights basis allows.
* Expected clauses are authored by a human from the fixture — never generated
  from the parser output. If the parser disagrees, fix the parser.
* One case per publisher convention at minimum; add a case whenever a parser
  bug is fixed (regression guard).
