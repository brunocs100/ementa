"""PII guardrail for Brazilian Portuguese: detect, pseudonymize with stable markers, rehydrate.

```python
guard = build_pii_guard()  # once per process: it loads the spaCy model
masked = guard.pseudonymize("Sou Maria Souza, CPF 123.456.789-09", conversation_mapping)
# masked.text == "Sou [PESSOA_1], CPF [CPF_1]"
answer = rehydrate(model_answer, masked.mapping)
```

A streamed answer goes through `MarkerStream(masked.mapping)`, which holds back a marker split
across pieces until it closes.
"""

from ementa_kit.guardrails.pii.guard import (
    DEFAULT_MODEL,
    Detection,
    PiiGuard,
    Pseudonymized,
    build_pii_guard,
    canonical_value,
    find_markers,
    rehydrate,
)
from ementa_kit.guardrails.pii.recognizers import (
    LANGUAGE,
    PiiEntity,
    is_valid_cnpj,
    is_valid_cpf,
)
from ementa_kit.guardrails.pii.stream import MarkerStream

__all__ = [
    "DEFAULT_MODEL",
    "LANGUAGE",
    "Detection",
    "MarkerStream",
    "PiiEntity",
    "PiiGuard",
    "Pseudonymized",
    "build_pii_guard",
    "canonical_value",
    "find_markers",
    "is_valid_cnpj",
    "is_valid_cpf",
    "rehydrate",
]
