# Research Wiki Schema

This Wiki stores source-linked research records. A stored page is not proof of a
scientific result and does not authorize code execution or a new Mission.

## Page Directories

| Directory | Frontmatter type | Purpose |
| --- | --- | --- |
| `concepts/` | `concept` | Definitions and relationships |
| `findings/` | `finding` | Claims, counterexamples, and unresolved evidence |
| `experiments/` | `experiment` | Inputs, results, and limitations |
| `queries/` | `query` | Questions and source-linked answers |
| `raw/` | `source` | Source records and immutable published material |

## Page Metadata

Every page in these directories needs YAML frontmatter with `title`, `created`,
`updated`, `type`, `status`, `tags`, and `sources`. Dates use `YYYY-MM-DD`.
`sources` is a list of project-relative files or HTTP(S) URLs; local files must
exist. A page may link to another Wiki page with `[[directory/page-slug]]`.
Every page must appear in `index.md` with the same link.

Curator proposals use `draft` or `candidate` status, retain traceable sources,
and state uncertainty. Execution records, model statements, and successful
checks must remain distinguishable from reviewed scientific conclusions.
Existing-page proposals include the current content hash as `base_sha256`;
publication rejects stale edits.

Keep credentials, private model configuration, runtime databases, model
weights, and raw task secrets outside Wiki pages. Runtime publication maintains
the source records and appends its own index and log entries.
