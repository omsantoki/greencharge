"""Phase 7 LLM layer: three narrow capabilities, not a general chatbot (BUILD_SPEC Phase 7).

    client.py    the one place that talks to a provider (Gemini) over HTTP
    extract.py   a driver's sentence -> target SoC + deadline (English, Hindi, Gujarati)
    explain.py   an already-computed schedule -> 2-3 sentences of plain language
    copilot.py   operator Q&A over exactly three tools, all answered by our own code

**THE RULE:** the LLM never produces a number that appears in the UI. It receives pre-computed
values and narrates them; it classifies and extracts; it does not calculate. Structurally:
explain() is handed only finished numbers as formatted strings and is told arithmetic is
forbidden, every answer is parsed into a Pydantic model and thrown away if it does not validate,
and the copilot's tools return numbers computed by GreenCharge, not by the model.

**Nothing blocks on the LLM.** ``client.complete()`` returns None on every failure, including no
API key at all; each caller has a non-LLM path, and the routers answer 503 with
``configuration_problem()`` when the layer is switched off. The whole layer is the last item on
the build spec's cut list: the product must work with it removed.

Nothing is re-exported here: import from the module that owns it, e.g.
``from app.llm.client import complete`` or ``from app.llm.extract import extract_constraints``.
"""
