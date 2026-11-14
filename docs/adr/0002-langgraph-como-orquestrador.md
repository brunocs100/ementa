# ADR 0002: LangGraph como orquestrador

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 · **Ver também:** [Agentes](../AGENTES.md), [Arquitetura](../ARQUITETURA.md)

## Contexto

Os agentes da Ementa não são todos iguais. O `legal_research` é um laço curto, com verificação e nova tentativa. O `contract_review` faz map-reduce por cláusula. O `brief_drafting` planeja, redige, critica e para no meio, à espera do advogado, às vezes por dias. O `case_monitor` é um workflow agendado, quase todo determinístico. O `assistant` roteia para os demais.

Três exigências pesam mais:

- **Parar e retomar.** A revisão humana (HITL) precisa sobreviver a reinício do processo, com o estado no banco.
- **Estado explícito.** Auditoria e avaliação precisam saber em que passo cada execução estava e o que o modelo viu.
- **Controle de fluxo no código.** A regra jurídica (verificar citação, nunca calcular prazo no modelo) não pode depender de o modelo decidir chamar a ferramenta certa.

## Decisão

- **LangGraph** orquestra todos os agentes:
  - `StateGraph` com estado tipado;
  - checkpointer no Postgres (schema `langgraph`);
  - `interrupt()` para a revisão humana;
  - `Send` para map-reduce;
  - subgrafos (a pesquisa vira um passo da redação de peças);
  - streaming de eventos para SSE e MCP.
- **LangChain 1.x** fornece chat models, ferramentas e middleware. Quando um agente simples basta (ferramentas com laço padrão), usamos `create_agent` com o middleware de guardrails do kit.
- Cada agente é montado pelo `AgentFactory` do kit, que liga modelo, ferramentas, guardrails, memória e telemetria do mesmo jeito em todos.
- As versões de `langgraph` e `langchain` ficam fixadas no `uv.lock` (conferir as versões exatas na F0).

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| CrewAI | Rápido para montar equipes de agentes por papel | Abstração de "papéis" esconde o fluxo; pouco controle de estado e de retomada | A Ementa precisa de fluxo explícito e de HITL durável |
| Microsoft Agent Framework / Semantic Kernel | Forte em .NET e no ecossistema Microsoft; workflows e conectores | Python é a linguagem do projeto; a comunidade de grafos com checkpoint é maior no LangGraph | Usado de propósito no `lgpd_compliance`, na F8 (ADR 0009), para mostrar interoperabilidade |
| OpenAI Agents SDK | Simples; handoffs e guardrails prontos | Preso a um provedor; menos controle de grafo e de checkpoint | A camada de modelos é agnóstica (ADR 0005) |
| Laço próprio | Controle total, zero dependência | Refaz checkpoint, retomada, streaming, map-reduce e HITL | Seria reescrever o que o LangGraph já testa |

## Consequências

**Positivas**

- Cada agente tem um grafo desenhável. O diagrama nos docs e o código contam a mesma história.
- A revisão humana é um nó do grafo, com estado persistido. Aprovar depois de dias é só retomar a thread.
- O map-reduce do `contract_review` roda as cláusulas em paralelo sem código de concorrência próprio.

**Negativas e riscos**

- Mais uma biblioteca de ritmo rápido. Mitigação: versões fixadas e testes com modelo falso cobrindo cada grafo.
- O estado do LangGraph é a fonte da verdade da execução, mas a visão que vai ao modelo é derivada dele. A regra do histórico só de acréscimo (ADR 0005) exige não usar redutores que reescrevem mensagens antigas.
- Usar LangChain e LangGraph juntos dá duas camadas de abstração. A regra é: LangGraph para o fluxo, LangChain só para modelo, ferramenta e middleware.

## Como validar

- Teste de integração: um `brief_drafting` para no `interrupt()`, o processo é encerrado, sobe de novo e a aprovação retoma a thread do ponto certo, com o mesmo estado.
- Cada grafo tem testes com modelo falso que cobrem todos os ramos (citação verificada, citação falha e nova tentativa, limite de tentativas).
- Gatilho para revisitar: o LangGraph mudar a API de checkpoint de forma incompatível, ou um agente precisar de algo que o grafo não expresse sem gambiarra.

## Referências

- Documentação do LangGraph: <https://langchain-ai.github.io/langgraph/>
- Documentação do LangChain: <https://python.langchain.com/>

---

[Índice de ADRs](README.md)
