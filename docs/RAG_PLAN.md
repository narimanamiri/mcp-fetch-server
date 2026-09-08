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
- [ ] **P1 — Ingest.** Loaders, structure-aware chunking, SQLite catalog,
      content-addressed blob store, `ingest` command.
- [ ] **P2 — Enrichment.** Summaries, tags, entities, hypothetical questions,
      taxonomy bootstrap and classification.
- [ ] **P3 — Retrieval.** Qdrant collections, hybrid search, reranking,
      `rag_search` tool.
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
