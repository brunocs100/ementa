# ADR 0009: Semantic Kernel no agente LGPD, com porte para o Microsoft Agent Framework

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F8 · **Ver também:** [Agentes](../AGENTES.md), [Copilot corporativo](../COPILOT-CORPORATIVO.md)

## Contexto

Empresas que vivem no ecossistema Microsoft (Azure, .NET, GitHub Copilot) costumam ter agentes em Semantic Kernel (SK). Em abril de 2026, a Microsoft lançou a versão 1.0 do Microsoft Agent Framework (conferir na F8), sucessor do SK e do AutoGen; os conceitos de kernel e de plugin do SK seguem nele.

Quem desenha uma plataforma de agentes nesse ambiente precisa de duas coisas:

- fazer agentes de frameworks diferentes conversarem;
- saber migrar do SK para o Agent Framework.

A Ementa usa LangGraph como orquestrador (ADR 0002). O MCP (ADR 0007) é o ponto natural de encontro: um agente em outro framework consome as mesmas ferramentas, com os mesmos guardrails, sem código de integração novo.

O `lgpd_compliance` é um bom candidato. Ele revisa um contrato ou um fluxo de dados contra um checklist da LGPD: base legal, dado sensível, transferência internacional, direitos do titular, encarregado. A tarefa é bem delimitada e consome ferramentas que já existem no `ementa-mcp`.

## Decisão

**Primeiro em SK.** O `lgpd_compliance` é escrito em Semantic Kernel, em Python, no app `apps/compliance_sk` (perfil `ms` do Docker Compose, F8). Ele consome o `ementa-mcp` como fonte de ferramentas (`search_legal_corpus`, `get_statute_article`) e usa a skill `lgpd-checklist`.

**Depois, o porte.** O mesmo agente é portado para o Microsoft Agent Framework 1.x. O diff entre as duas versões vira um ADR novo (0013) na F8, sem reescrever este: o que mudou em conceitos, registro de ferramentas, MCP, estado e telemetria.

**As duas versões** usam o modelo pela configuração do ADR 0005 e emitem spans no mesmo padrão do ADR 0008.

**Versões.** As do SK e do Agent Framework ficam fixadas no `uv.lock` (conferir na F8 o pacote Python e a versão do Agent Framework).

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Só Agent Framework | É o caminho recomendado para código novo | Perde a demonstração de SK, que muitas empresas ainda rodam | O porte mostra as duas pontas e o caminho entre elas |
| Só LangGraph | Uma stack só; menos manutenção | Não mostra interoperabilidade nem o ecossistema Microsoft | A interoperabilidade via MCP é o ponto da decisão |
| Agente em C# (.NET) | É onde o SK é mais forte | Segunda linguagem e segundo toolchain; fora do escopo do projeto | Fica como extra opcional, fora do roadmap |

## Consequências

**Positivas**

- Prova que o `ementa-mcp` serve a um framework diferente sem adaptação, que é a promessa do MCP.
- O documento do porte é material concreto para quem vai migrar do SK.
- O agente LGPD aproveita guardrails e auditoria do servidor MCP, sem reimplementar.

**Negativas e riscos**

- Duas implementações do mesmo agente para manter. Mitigação: escopo pequeno e a mesma avaliação para as duas.
- APIs do SK e do Agent Framework em Python mudam rápido. Mitigação: versões fixadas e testes de contrato contra o servidor MCP.
- O agente roda fora do LangGraph, então o HITL e o checkpoint do resto da plataforma não valem nele. Ele só gera relatório e não toma ação externa.

## Como validar

- As duas versões (SK e Agent Framework) passam na mesma avaliação: um conjunto de contratos fictícios com lacunas de LGPD plantadas, medindo o F1 das lacunas encontradas.
- O trace mostra o agente externo chamando as ferramentas do `ementa-mcp`, com os guardrails aplicados.
- Gatilho para revisitar: o SK ser descontinuado de vez. A versão SK vira só histórico, registrada no ADR 0013.

## Referências

- Semantic Kernel: <https://learn.microsoft.com/semantic-kernel/>
- Microsoft Agent Framework 1.0: <https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/>
- Guia de migração do SK para o Agent Framework (conferir na F8)

---

[Índice de ADRs](README.md)
