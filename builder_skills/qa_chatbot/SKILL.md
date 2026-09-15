---
name: qa_chatbot
description: Question-and-answer chatbot stub - keyword retrieval over a few synthetic documents, one model call to answer with citations. The simplest output format; built first.
---

# Output format: question-and-answer chatbot (illustrative stub)

You are building a **small, clearly labeled illustrative prototype**, not a product. It exists
to show the stakeholder and reviewer what "a Q&A chatbot over your documents" would feel like
in their domain. Keep it tiny, readable, and honest about what it is.

## Files to create (exactly these; no others)

```
README.md          what it is, what it is NOT, how to run it, what a real version would need
knowledge/*.md     2 or 3 short synthetic documents in the stakeholder's domain (150-300 words each)
qa.py              the prototype: retrieval + answer
test_qa.py         tests for retrieval only; must pass with no API key and no network
```

## qa.py requirements

- Standard library plus the `anthropic` package only. No other dependencies.
- `load_documents(folder) -> list[Document]`: read every `knowledge/*.md`.
- `retrieve(query, docs, k=3) -> list[Passage]`: split documents into paragraphs and rank them
  by simple keyword overlap (lower-case tokens, stopwords removed). Deterministic; no
  embeddings.
- `answer(question, passages) -> str`: ONE call to the Claude API (`claude-opus-5`) with the
  retrieved passages as context; the reply must cite which document each fact came from. Import
  `anthropic` **inside** this function so the module imports without the package installed.
- `main()`: a terminal loop (`question> `) that prints the answer and the cited sources.
  `quit` exits. If `ANTHROPIC_API_KEY` is not set, print the retrieved passages instead of
  calling the model, and say so.
- Total file length under 150 lines.

## test_qa.py requirements

- Uses `pytest`. Tests `load_documents` and `retrieve` against the real `knowledge/` folder.
- At least: documents load; a question about a topic in document A retrieves a passage from
  A first; a nonsense query returns an empty list or low-scored results without crashing.
- Never calls `answer`, never needs a key.

## The banner

The first lines of `qa.py` and `test_qa.py`, and the first heading of `README.md`, must say:

```
ILLUSTRATIVE PROTOTYPE - not production code. Built automatically from a reviewed discovery
spec to demonstrate the requested output format. Synthetic data only.
```

## README.md must include

1. The banner as the first heading.
2. One paragraph, plain language, saying what this prototype does *for this stakeholder*.
3. "What it does NOT do": no real documents, no write access to any system, no authentication,
   no persistence, answers are illustrative.
4. How to run: `pip install anthropic pytest`, set `ANTHROPIC_API_KEY`, `python qa.py`,
   `pytest -q`.
5. "What a real version would need": the stakeholder's actual documents and where they live,
   access controls, evaluation on real questions, a UI. Three to five bullets, no more.

## Verification

Run `pytest -q` in the workspace and make sure it passes before you report back. Then report:
the files you wrote, the test output, and one sentence on what the stakeholder will see.
