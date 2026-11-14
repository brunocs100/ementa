# Matriz de competências

Onde cada competência de Engenheiro de Agentes de IA aparece na Ementa, em que fase entra e como mostrar.

**Status:** Documentado (atualizada a cada fase) · **Ver também:** [Resumo](RESUMO.md), [Plano](PLANO.md), [Roteiro da demo](ROTEIRO-DEMO.md)

A matriz é honesta: o status reflete o que existe hoje no repositório, não o que está planejado. Hoje o desenho está completo nos docs e o código começa na F0.

| Status | Significado |
|---|---|
| Planejado | Está no plano, sem desenho detalhado |
| Documentado | Desenho detalhado nos docs e nos ADRs; sem código |
| Em andamento | Código em construção na fase indicada |
| Pronto | Código, testes e demo funcionando |

## Requisitos principais

| Competência | Onde no projeto | Fase | Como demonstrar | Status |
|---|---|---|---|---|
| Desenvolvimento e arquitetura de soluções corporativas | Portas e adaptadores (hexagonal), diagramas C4, 12 ADRs, kit separado do domínio; [Arquitetura](ARQUITETURA.md) | F0 em diante | Abrir a arquitetura de referência e um ADR com alternativas e consequências | Documentado |
| Python, C# e/ou Java, com boas práticas de engenharia e padrões de projeto | Python 3.12 tipado (mypy strict a partir da F2), ruff, import-linter; Strategy (provedores de modelo), Adapter (armazenamentos), Chain of Responsibility (guardrails), Repository, Builder (`AgentFactory`), Template Method (template de agente), Observer (eventos de telemetria e auditoria), listados em [Arquitetura](ARQUITETURA.md) | F0 em diante | Mostrar o `AgentFactory` e o pipeline de guardrails; regras de dependência quebrando o CI | Documentado |
| APIs REST, Git/GitHub, integração contínua e testes automatizados | FastAPI (`POST /v1/legal-research` na F2, rotas `/v1` na F3), GitHub Actions (`ci.yml` na F0, `evals.yml` e gitleaks e pip-audit na F2, `security.yml` completo na F10), pytest, Hypothesis, testcontainers | F0, F2, F3, F10 | CI verde no GitHub; teste fixo do prazo; teste de integração da busca | Documentado |
| IA generativa, LLMs, engenharia de prompt e de contexto | Prompts versionados (id, versão, changelog); montador de contexto `ementa_kit.context` com orçamento por seção; histórico só de acréscimo | F1, F2, F3 | Trace com o tamanho de cada seção do contexto; teste do prefixo no CI | Documentado |
| RAG, embeddings, bases vetoriais, function calling e memória de agentes | `legal_research`; embeddings locais; pgvector + FTS + RRF; Structured Outputs e function calling `strict`; checkpointer e store do LangGraph | F1, F2, F3 | Demo passo 1 (resposta com `cdc:art49` verificado); demo passo 2 (`calculate_deadline`) | Documentado |
| Desenvolvimento e sustentação de agentes de IA em ambiente produtivo | Docker Compose por perfis, timeouts, retries, fallback, orçamento com parada automática, SLOs por rota, runbooks, auditoria | F3, F7 | Dashboard de custo e erro; alerta de orçamento; runbook de incidente em [Segurança e governança](SEGURANCA-E-GOVERNANCA.md); runbooks de operação na F7 | Documentado |
| Arquiteturas multiagentes, MCP e skills customizadas | `assistant` (supervisor), `contract_review` (map-reduce), `brief_drafting` (plan-and-execute com crítico); `ementa-mcp` (spec 2026-07-28); skills em `skills/` e `.github/skills/` | F1, F2, F3, F4 | Copilot chamando o `ementa-mcp`; grafo do `brief_drafting` parado na aprovação | Documentado |
| Governança e segurança para IA (guardrails e uso corporativo do GitHub Copilot) | Pipeline de guardrails, verificador de citações, pseudonimização, roteamento por sigilo, RLS, red team; playbook de Copilot corporativo | F1, F2, F6, F8 | Demo passo 3 (citação inventada removida); [Segurança e governança](SEGURANCA-E-GOVERNANCA.md); [Copilot corporativo](COPILOT-CORPORATIVO.md) | Documentado |
| Bancos relacionais, SQL e manipulação de dados | Postgres com schemas `firm`, `knowledge`, `ai`, `audit`; RLS; FTS em português; views `analytics` com CTEs e funções de janela; `firm_analytics` com sqlglot | F1, F3, F6 | Consulta híbrida em SQL; teste de RLS (advogado A não vê o processo B) | Documentado |

## Diferenciais

| Competência | Onde no projeto | Fase | Como demonstrar | Status |
|---|---|---|---|---|
| Ecossistema GitHub Copilot e VS Code | `AGENTS.md`, `.vscode/mcp.json`, instruções por caminho, prompt files, custom agents, skills de desenvolvimento | F0, F1, F8 | Copilot em modo agente usando o `ementa-mcp`; skill `new-mcp-tool` gerando uma ferramenta no padrão do repo | Documentado |
| LangGraph | Todos os agentes: `StateGraph`, checkpointer, `interrupt()`, `Send`, subgrafos | F1 em diante | Grafo do `legal_research` com laço de reflexão; `brief_drafting` retomado após aprovação | Documentado |
| LangChain | Chat models (ChatOpenAI, ChatOllama, ChatAnthropic), ferramentas, middleware dos guardrails | F1 em diante | Troca de provedor só por configuração; [ADR 0005](adr/0005-camada-de-modelos-agnostica.md) | Documentado |
| Semantic Kernel | `lgpd_compliance` consumindo o `ementa-mcp`, com porte para o Microsoft Agent Framework 1.x | F8 | Agente SK chamando as mesmas ferramentas MCP; [ADR 0009](adr/0009-semantic-kernel-e-agent-framework.md) | Documentado |
| OpenTelemetry e observabilidade de agentes | Spans com atributos `gen_ai.*` e `ementa.*`; Grafana LGTM; dashboards versionados | F1, F7 | Demo passo 4 (cascata do trace no Grafana) | Documentado |
| Knowledge graphs | Neo4j com leis, artigos, súmulas, decisões e partes; `expand_citation_graph`; conflito de interesses no `intake` | F5 | Consulta Cypher de conflito; resposta da pesquisa trazendo súmula ligada ao artigo | Documentado |
| Fine-tuning | QLoRA de modelo aberto pequeno para triagem, no Colab ou Kaggle; GGUF no Ollama | F9 | Tabela do modelo ajustado contra Luna e Terra; [ML e fine-tuning](ML-E-FINE-TUNING.md) | Documentado |
| MLOps | MLflow (experimentos, registro, aliases champion e challenger), cascata por confiança, drift pelas correções humanas, model card | F9 | Experimento de embeddings e do modelo de triagem no MLflow | Documentado |
| Criação de frameworks e componentes reutilizáveis para agentes | `ementa-kit` sem domínio jurídico; template Copier `templates/novo-agente/` testado no CI | F0, F10 | Gerar um agente novo pelo template e rodar os testes dele; [ADR 0001](adr/0001-monorepo-uv-com-kit-separado.md) | Documentado |

## Responsabilidades da função

| Competência | Onde no projeto | Fase | Como demonstrar | Status |
|---|---|---|---|---|
| Agentes de IA voltados ao ciclo de desenvolvimento de software | `pr_reviewer` no GitHub Actions; repositório preparado para o Copilot; skills de desenvolvimento | F0, F8 | Comentário do `pr_reviewer` num PR real do repo | Documentado |
| Arquiteturas de referência para sistemas multiagentes | Arquitetura de referência genérica do kit; padrões supervisor, map-reduce e plan-and-execute documentados | F0, F4 | Seção de arquitetura de referência em [Arquitetura](ARQUITETURA.md); [Agentes](AGENTES.md) | Documentado |
| Integração de agentes com sistemas corporativos | DataJud (CNJ), e-mail, Teams ou Slack, Postgres do escritório, MCP para clientes de IA | F1, F5 | `case_monitor` detectando movimentação e calculando prazo | Documentado |
| Apoio às equipes na adoção de IA com governança e segurança | Playbook de Copilot corporativo, template de agente, skills de desenvolvimento, docs e ADRs | F8, F10 | [Copilot corporativo](COPILOT-CORPORATIVO.md); template gerando agente com guardrails e telemetria prontos | Documentado |

## Evidência por marco

O que passa de "Documentado" para "Pronto" em cada marco, para conferir em entrevista sem depender da palavra do autor.

### Marco 1, demo mínima (F0 e F1)

- RAG sobre o CDC com pgvector, busca textual em português e fusão RRF.
- `legal_research` com verificador de citações e uma nova tentativa.
- `calculate_deadline` com o caso fixo de 03/12/2026 a 26/01/2027 e testes por propriedade.
- `ementa-mcp` via stdio com 4 ferramentas, usado pelo Copilot no VS Code.
- Traces no Grafana; CI com ruff, mypy e pytest; 10 perguntas de fumaça.

### Marco 2, núcleo e API (F2 e F3)

- Pseudonimização de PII brasileira, skills de runtime, MCP via HTTP com política de saída.
- Rotas REST com JWT e RBAC, SSE, memória curta e longa, `assistant`.
- 30 perguntas douradas no CI, registro de prompts e teste do prefixo.

### Marco 3, multiagente, integrações e governança (F4 a F6)

- `contract_review` e `brief_drafting` com aprovação humana no console.
- DataJud, `case_monitor`, Neo4j, GraphRAG e `intake` com conflito.
- Sigilo roteado para o modelo local, RLS, `firm_analytics`, red team.

### Marco 4, operação e ecossistema (F7 a F10)

- Dashboards, SLOs por rota, avaliação noturna comentada no PR.
- `lgpd_compliance` em Semantic Kernel, artefatos do Copilot, `pr_reviewer`.
- Modelo de triagem ajustado e registrado no MLflow.
- Kit publicado, template Copier e site de docs.

## Como manter a matriz

- Toda fase fechada atualiza o status das linhas que ela toca, no mesmo PR.
- "Pronto" exige três coisas: código no `main`, teste no CI e um passo de demo que mostre.
- Item que mudou de fase no plano muda aqui também; a matriz e o [Plano](PLANO.md) não podem divergir.

## Não coberto e por quê

| Competência | Motivo | Alternativa |
|---|---|---|
| C# | O projeto foi feito em Python para cobrir o ecossistema de IA (LangGraph, LangChain, MLflow) com uma linguagem só | Porte .NET do agente `lgpd_compliance` (Semantic Kernel ou Agent Framework têm SDK .NET) fica como extra opcional |
| Java | Mesmo motivo | Nenhuma prevista |

> [!NOTE]
> Políticas de organização do GitHub Copilot (exclusão de conteúdo, lista permitida de MCP, auditoria, métricas de uso) exigem plano Business ou Enterprise. No projeto elas ficam documentadas com exemplos de configuração; o que é do repositório é demonstrado com Copilot Free ou Pro.

---

[Voltar ao README](../README.md)
