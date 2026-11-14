# Decisões de arquitetura (ADRs)

Cada decisão de arquitetura da Ementa fica registrada aqui: o contexto, a escolha, as alternativas descartadas e o que confirmaria ou derrubaria a decisão.

| Nº | Decisão | Status | Fase |
|---|---|---|---|
| [0001](0001-monorepo-uv-com-kit-separado.md) | Monorepo uv com kit separado do domínio | Aceito | F0 |
| [0002](0002-langgraph-como-orquestrador.md) | LangGraph como orquestrador; LangChain 1.x para modelos, ferramentas e middleware | Aceito | F1 |
| [0003](0003-postgres-pgvector-busca-hibrida.md) | Postgres com pgvector e busca híbrida, com RLS | Aceito | F1 |
| [0004](0004-neo4j-grafo-juridico.md) | Neo4j para o grafo jurídico e o conflito de interesses | Aceito | F5 |
| [0005](0005-camada-de-modelos-agnostica.md) | Camada de modelos agnóstica, com papéis, roteamento por sigilo e histórico só de acréscimo | Aceito | F1 |
| [0006](0006-guardrails-em-pipeline.md) | Guardrails em pipeline próprio, com Presidio e verificador de citações | Aceito | F1 |
| [0007](0007-mcp-como-padrao-de-integracao.md) | MCP como padrão de integração, com política de saída para cliente externo | Aceito | F1 |
| [0008](0008-opentelemetry-e-grafana-lgtm.md) | OpenTelemetry com convenções GenAI e Grafana LGTM | Aceito | F0 |
| [0009](0009-semantic-kernel-e-agent-framework.md) | Semantic Kernel no agente LGPD, com porte para o Microsoft Agent Framework | Aceito | F8 |
| [0010](0010-embeddings-e-rerank-locais.md) | Embeddings e rerank locais, escolhidos por medição | Aceito | F1 |
| [0011](0011-fine-tuning-de-modelo-pequeno.md) | Fine-tuning de modelo pequeno para a triagem, servido local | Aceito | F9 |
| [0012](0012-console-fastapi-htmx.md) | Console em FastAPI, Jinja2 e HTMX | Aceito | F4 |

"Aceito" quer dizer que a decisão foi tomada no desenho. O código chega na fase indicada; até lá, o ADR descreve a intenção e o critério de validação.

## Como escrever um ADR

- Abra um ADR quando a escolha for difícil de desfazer, afetar mais de um pacote ou trocar uma dependência de infraestrutura, um provedor ou um padrão de integração.
- Numere em sequência, com quatro dígitos, e use um nome de arquivo curto em português sem acento: `0013-nome-da-decisao.md`.
- Status possíveis: **Proposto** (em discussão, aberto num PR), **Aceito** (decidido) e **Substituído por NNNN** (um ADR novo tomou o lugar; o antigo fica, com link para o novo).
- Não reescreva um ADR aceito. Mudou a decisão, abra outro e marque o anterior como substituído.
- Todo ADR diz como validar: a medida ou o teste que confirma a escolha e o sinal que manda revisitá-la.

Template:

```markdown
# ADR NNNN: Título da decisão

**Status:** Proposto · **Data:** AAAA-MM-DD · **Fase:** FN · **Ver também:** [Arquitetura](../ARQUITETURA.md)

## Contexto

O problema, as restrições e as forças em jogo, no caso da Ementa.

## Decisão

O que foi escolhido, em frases afirmativas.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| ... | ... | ... | ... |

## Consequências

**Positivas**

- ...

**Negativas e riscos**

- ...

## Como validar

A medida ou o teste que confirma a decisão, e o gatilho para revisitá-la.

## Referências

- ...

---

[Índice de ADRs](README.md)
```

---

[Voltar ao README](../../README.md)
