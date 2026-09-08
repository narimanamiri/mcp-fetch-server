# Offline corpus, simulated internet, and RAG

This document is the build plan for turning the web fetch server into an
offline document archive that a local model can browse and search as if it
were the open web.

## The idea

Three layers stack on top of the existing server:

1. **Corpus** — offline documents (PDF, DOCX, PPTX, EPUB, HTML, Markdown) are
   ingested, chunked and embedded into Qdrant.
2. **Simulated internet** — `fetch_url`, `web_search`, `extract_links` and
   `batch_fetch` keep their existing signatures but resolve against the local
   corpus. An agent browses ingested documents exactly as it browses the web.
3. **Retrieval** — `rag_search` and `rag_answer` expose the same corpus as
   ranked, citable passages for when an agent wants an answer rather than a
   page.

The URL is the primary key throughout. Documents that came from the web keep
their origin URL; loose files get one minted under `FETCH_SITE_BASE_URL`
(default `https://local.archive`), for example
`https://local.archive/doc/annual-report-2024#p12`. Because every passage,
search result and citation is addressed by URL, retrieval and browsing agree
with each other and citations stay stable across re-ingestion.

## Two models, two jobs

| | MCP sampling | Local model (`rag.llm`) |
|---|---|---|
| Whose model | The connected client's | One the server owns |
| Reached via | `ctx.session.create_message` | HTTP to Ollama or an OpenAI-compatible server |
| Available during ingestion | No | Yes |
| Used for | `summarize_url` | enrich, categorise, repair, query expansion |

Ingestion is unattended and long-running, so it cannot depend on a client
being connected. That is why the server has its own model client.

## Modes

`FETCH_NET_MODE` decides how the fetch tools resolve a request:

| Mode | Behaviour |
|---|---|
| `online` | Always the real network. Default; identical to the original server. |
| `offline` | Only the local corpus. The network is never touched. |
| `hybrid` | Local corpus first, real network as a fallback. |

## Preflight

```bash
uv run mcp-fetch-server doctor
```

Checks the local model, its chat and embedding models, the embedding size,
Qdrant, and the corpus directory. Exits non-zero if anything is broken, so it
can gate an ingestion run in a script. `--json` emits the same report as JSON.

## Ingesting

```bash
uv run mcp-fetch-server ingest ./corpus
```

Walks directories, loads every supported file, and stores each document as
canonical Markdown in the blob store with its chunks in the catalog. Runs
incrementally: a file whose bytes have not changed is skipped, so re-running
over a large corpus costs a directory walk. `--reingest` forces the work,
`--dry-run` lists what would be ingested, and `--json` emits a machine-readable
summary.

Supported formats: `.pdf`, `.docx`, `.pptx`, `.epub`, `.html`, `.md`, `.txt`.
Scanned PDFs are rejected with a pointer to `ocrmypdf` rather than ingested as
empty documents.

| Setting | Default | Effect |
|---|---|---|
| `FETCH_CHUNK_TARGET_TOKENS` | `600` | Token budget per chunk |
| `FETCH_CHUNK_OVERLAP_RATIO` | `0.15` | Overlap carried between chunks |
| `FETCH_CHUNK_SECTION_BREAK_LEVEL` | `2` | Headings at or above this level start a new chunk |

## Enriching and categorising

```bash
mcp-fetch-server enrich              # local model describes each document
mcp-fetch-server taxonomy bootstrap  # propose categories from the corpus
mcp-fetch-server taxonomy show       # review them
mcp-fetch-server classify            # assign documents to those categories
```

Enrichment produces a title, summary, genre, tags, entities, publication date
and a set of hypothetical questions per document. The questions are indexed
alongside the text later: a user's phrasing resembles a question far more than
it resembles the document's prose.

Categorisation runs as a separate pass against a fixed taxonomy stored in
`data/taxonomy.yaml`. Bootstrap proposes it once from the enriched corpus;
after that the file is edited by hand and `classify` may only choose from it.
Documents that fit nothing become `uncategorised` for review rather than
inventing a new category. `taxonomy bootstrap` refuses to overwrite an
existing file without `--force`, because re-proposing categories orphans every
classification already made.

**Schemas must be explicit.** Pydantic leaves fields with defaults out of
`required`, and a 4B model then omits them entirely: genre, language,
entities and published_at all came back missing, with defaults silently
filling in `"other"` and empty lists. Both enrichment and classification now
push hand-built JSON schemas where every field is required and closed sets are
enums. Constraining the schema also made generation *faster*, from 8.6 s to
4.8 s per document.

Measured on the development machine: about 4.8 s per document to enrich, and
about 0.4 s to classify (classification only sees the title, summary and
tags).

## Embedding and searching

```bash
mcp-fetch-server embed                  # chunks -> vectors
mcp-fetch-server search "your question" # search from the terminal
```

Qdrant runs either as a server (`FETCH_QDRANT_URL=http://localhost:6333`, via
Compose) or **embedded on local disk** when that setting is empty. Embedded
mode means a single-user offline setup needs no Docker at all.

Every chunk is indexed twice. The `dense` vector comes from `bge-m3` and
carries meaning; the `sparse` vector is term frequency computed locally, and
Qdrant applies IDF itself, so ingestion stays stateless and adding documents
never invalidates weights already written. A query runs both arms and fuses
them with reciprocal rank fusion, which needs no calibration between two
incomparable scoring scales.

That second arm is not a nicety. Searching the test corpus for `bge-m3`
returns the *Persian* document that mentions it ahead of the English ones — an
exact-token match a purely semantic index loses. Persian text is normalised
before tokenising (Arabic vs Persian yeh and kaf, alef variants, ZWNJ,
diacritics, Persian and Arabic-Indic digits), without which lexical search on
a Persian corpus misses most of its matches.

Chunk text lives in SQLite, not in the Qdrant payload. The catalog stays
authoritative, payloads stay small, and the whole index can be dropped and
rebuilt without re-parsing a single source file.

MCP tools added: `rag_search` (ranked passages with citable URLs) and
`corpus_stats` (what the corpus can answer). Resources: `corpus://stats`,
`corpus://taxonomy`, `corpus://doc/{doc_id}`. They register only when the
`rag` extra is installed; without it this stays a plain web fetch server.

## Hardware notes

Measured on the development machine (RTX 4060 8 GB, i7-14700K, 128 GB RAM):

- `bge-m3` is 664 MB on the GPU and produces 1024-dimensional vectors.
- Warm embedding throughput is about 37 chunks/s for 600-token chunks.
- VRAM is the binding constraint. The embedding model and the chat model do
  not comfortably co-reside on 8 GB, so ingestion runs them as two sequential
  passes and evicts one before loading the other.

Budget a corpus by ingestion hours rather than by disk. Enrichment dominates
at roughly 11 s per document; 10k documents is about a day and a half of GPU
time and roughly 8 GB on disk.

## Accuracy decisions

These are the choices that decide whether answers are trustworthy:

- **Hybrid dense + sparse retrieval** fused with RRF, so exact tokens (error
  codes, API names, transliterated technical terms) are not lost to a purely
  semantic match.
- **Cross-encoder reranking** of the top 50 candidates down to the top 8.
- **Structure-aware chunking** on heading and page boundaries, with the
  heading path prepended to the embedded text.
- **Small-to-big**: embed small chunks, return the surrounding parent window.
- **Repair never rewrites.** The local model may restructure badly extracted
  text, but its output is rejected unless it preserves at least 95% of the
  original content tokens. A corpus that invents source text is worse than no
  corpus.
- **Fixed taxonomy.** Documents are classified against a stored, editable
  taxonomy rather than free-form labelled, which otherwise produces hundreds
  of near-duplicate categories.
- **Citations carry offsets** so a quote can be checked against the source.

## Phases

- [x] **P0 — Foundation.** Settings, `rag.llm` local model client (Ollama +
      OpenAI-compatible), `doctor` preflight command, Qdrant in Compose,
      optional `rag` dependency extra.
- [x] **P1 — Ingest.** Loaders (PDF, DOCX, PPTX, EPUB, HTML, Markdown,
      text), structure-aware chunking, SQLite catalog, content-addressed
      blob store, incremental `ingest` command.
- [x] **P2 — Enrichment.** Summaries, tags, entities, hypothetical questions,
      taxonomy bootstrap and classification.
- [x] **P3 — Retrieval.** Qdrant collections, hybrid dense + sparse search
      fused with RRF, `rag_search` and `corpus_stats` tools, `corpus://`
      resources. Cross-encoder reranking moves to P5.
- [ ] **P4 — Simulated internet.** Site generator, URL resolver, local search
      backend, link graph; the fetch tools start serving the corpus.
- [ ] **P5 — Accuracy.** Small-to-big, query expansion, deduplication,
      metadata filters, `rag_answer`.
- [ ] **P6 — Operations.** Evaluation harness, admin corpus tab, watch-folder
      re-ingestion.

## Configuration

All settings are listed in `.env.example` under "Offline corpus / RAG". The
ones worth knowing:

| Variable | Default | Meaning |
|---|---|---|
| `FETCH_NET_MODE` | `online` | `online`, `offline` or `hybrid` |
| `FETCH_SITE_BASE_URL` | `https://local.archive` | Base URL minted for local documents |
| `FETCH_LLM_BACKEND` | `ollama` | `ollama` or `openai` |
| `FETCH_LLM_BASE_URL` | `http://localhost:11434` | Where the local model listens |
| `FETCH_LLM_CHAT_MODEL` | `gemma3:4b` | Enrichment and answering |
| `FETCH_LLM_EMBED_MODEL` | `bge-m3` | Embeddings (multilingual) |
| `FETCH_LLM_KEEP_ALIVE` | `5m` | How long a model stays in VRAM |
| `FETCH_QDRANT_URL` | `http://localhost:6333` | Vector store |
| `FETCH_CORPUS_DATA_DIR` | `./data` | Catalog, blobs, page cache, taxonomy |
| `FETCH_CHUNK_TARGET_TOKENS` | `600` | Chunk size |
| `FETCH_RAG_CANDIDATES` | `50` | Candidates fetched before reranking |
| `FETCH_RAG_TOP_K` | `8` | Passages returned after reranking |

Ollama is not part of the Compose stack because it needs the host GPU. Run
`ollama serve` on the host; containers reach it at
`http://host.docker.internal:11434`.
