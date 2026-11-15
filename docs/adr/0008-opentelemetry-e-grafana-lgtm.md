# ADR 0008: OpenTelemetry com convenções GenAI e Grafana LGTM

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F0 · **Ver também:** [Observabilidade](../OBSERVABILIDADE.md)

## Contexto

Um agente em produção falha de jeitos que log solto não explica:

- a busca trouxe a fonte errada;
- o guardrail removeu a única citação;
- a nova tentativa dobrou o custo;
- o modelo demorou 15 s pensando.

Para depurar e para auditar, cada execução precisa virar um trace. A cascata vai do agente à busca, à chamada ao modelo (com tokens e custo), ao guardrail e à ferramenta.

A camada de modelos é agnóstica (ADR 0005). Prender a observabilidade a um fornecedor ligado a um framework ou provedor iria contra isso.

Na máquina de dev, com 15 GB de RAM, a stack precisa ser leve.

## Decisão

**SDK e convenções.** O SDK do OpenTelemetry instrumenta todos os componentes, seguindo as convenções semânticas GenAI (`gen_ai.*`). Elas ainda estão em desenvolvimento e os nomes podem mudar; o kit isola isso em `ementa_kit.telemetry`. A versão das convenções usada fica fixada no `uv.lock` e anotada neste ADR na F1.

**Spans próprios:** `invoke_agent`, `chat`, `execute_tool`, `retrieval` e `guardrail`. Atributos próprios: `ementa.cost_usd`, `ementa.prompt.id`, `ementa.policy.route` e `ementa.guardrail.decision`, além do id do processo pseudonimizado.

**Instrumentação pronta:**

- chamadas à OpenAI pela `opentelemetry-instrumentation-openai-v2` (conferido na F1: a 2.4b0 cobre Chat Completions e embeddings, não a Responses API; ver [ADR 0005](0005-camada-de-modelos-agnostica.md));
- FastAPI, httpx e psycopg pelas instrumentações automáticas.

**Logs** em JSON com structlog, correlacionados por `trace_id`.

**Dev.** O container `grafana/otel-lgtm`, no perfil `obs`, traz num container só o OTel Collector, Prometheus, Tempo, Loki e Pyroscope, com o Grafana na frente.

**Produção.** Qualquer backend que receba OTLP: o mesmo trio Tempo, Prometheus e Loki gerenciado, ou um fornecedor compatível.

**Dashboards** versionados em `infra/` como JSON.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| LangSmith | Integração direta com LangGraph; avaliação e datasets prontos | SaaS pago; prende a observabilidade ao ecossistema LangChain | Lock-in contrário ao ADR 0005; fora do orçamento |
| Langfuse | Aberto, focado em LLM, com prompts e avaliações | Auto-hospedagem pesada para a máquina de dev de 15 GB (conferir os componentes na F7) | Pode ser plugado depois como destino OTLP, se valer a pena |
| Arize Phoenix | Nativo em OpenTelemetry; bom para avaliação e análise de RAG | Mais uma UI a operar; dashboards de operação ficam fora | Candidato para análise de avaliação na F7, sem trocar a instrumentação |

## Consequências

**Positivas**

- A instrumentação é neutra: trocar de backend é trocar o endpoint OTLP.
- Um container só no dev; sobe com o perfil `obs` e cabe na RAM.
- Traces, métricas e logs ligados pelo mesmo `trace_id`, que também aparece na auditoria.

**Negativas e riscos**

- As convenções GenAI ainda mudam. Mitigação: nomes centralizados no kit e teste que confere os atributos dos spans.
- O conteúdo de prompt e de resposta no trace pode carregar dado pessoal. O padrão é não registrar conteúdo; quando registrar, só depois da pseudonimização.
- O otel-lgtm é para dev e demo, não para produção. Isso fica escrito na [Observabilidade](../OBSERVABILIDADE.md).

## Como validar

- O trace da demo do Marco 1 mostra todos os spans esperados (agente, busca vetorial e textual, fusão, chamada ao modelo com tokens e custo, verificador, ferramenta), com os atributos do kit.
- Os dashboards leem as métricas no Prometheus: latência por rota, tokens, custo por papel e decisões de guardrail.
- Teste automatizado: com um exportador em memória, uma execução de `legal_research` produz a árvore de spans esperada.
- Gatilho para revisitar: as convenções GenAI ficarem estáveis com nomes diferentes (migrar o kit), ou o time precisar de avaliação integrada que só uma ferramenta específica dá.

## Referências

- Convenções semânticas GenAI do OpenTelemetry: <https://opentelemetry.io/docs/specs/semconv/gen-ai/>
- `grafana/otel-lgtm`: <https://github.com/grafana/docker-otel-lgtm>
- Instrumentação da OpenAI no OpenTelemetry Python: <https://pypi.org/project/opentelemetry-instrumentation-openai-v2/>

---

[Índice de ADRs](README.md)
