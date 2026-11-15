# AGENTS.md

Instruções para agentes de código (GitHub Copilot, Claude Code e outros) que trabalham neste repositório.

## O projeto

A Ementa é uma plataforma de agentes de IA para escritórios de advocacia, construída como projeto de portfólio. O plano está em `docs/PLANO.md` e a arquitetura em `docs/ARQUITETURA.md`. Hoje o repositório só tem documentação; o código entra a partir da F0, uma fase por sessão.

## Onde fica cada coisa

| Caminho | Conteúdo |
|---|---|
| `packages/ementa-kit/` | Kit genérico de agentes: config, llm, telemetry, guardrails, policy, rag, context, memory, skills, mcp, evals, audit |
| `packages/ementa/` | Domínio jurídico: domain, ingest, integrations, tools, agents, prompts |
| `apps/` | Pontos de entrada: `api`, `worker`, `mcp_server`, `compliance_sk` |
| `skills/` | Skills jurídicas de runtime (SKILL.md) |
| `data/` | Corpus público com fonte e hash; dados sintéticos |
| `evals/` | Datasets dourados e red team |
| `infra/` | Docker Compose, OpenTelemetry, dashboards, SQL inicial |
| `docs/` | Plano, arquitetura, ADRs e guias |

## Comandos

Os da F0 primeiro; os da F1 em seguida.

```powershell
# A partir da F0
uv sync                                          # instala tudo do workspace
docker compose --profile core --profile obs up -d   # Postgres e Grafana LGTM
uv run ruff check . ; uv run ruff format --check .
uv run mypy packages apps
uv run pytest -m "not eval"                      # unitários; integração com EMENTA_TEST_DATABASE_URL

# A partir da F1
uv run ementa ingest cdc                         # carrega o CDC no Postgres (--reset recria o schema)
uv run ementa search "cobrança indevida"         # busca híbrida, sem modelo
uv run ementa ask "Posso desistir da compra online?"   # agente legal_research (gasta API)
uv run ementa smoke --retrieval-only             # 10 perguntas de fumaça, só a busca
uv run ementa smoke                              # as 10 com o agente (cerca de US$ 0,10)
uv run ementa-mcp                                # servidor MCP via stdio

# A partir da F2
uv run ementa ingest all --reset                 # CDC, LGPD, CPC, súmulas do STJ e do STF (cdc, lgpd, cpc, stj ou stf, um por vez)
uv run ementa smoke --retrieval-only --file evals/legal_research.jsonl   # 30 perguntas, só a busca
uv run ementa smoke --file evals/legal_research.jsonl --max-cost-usd 0.5 --markdown out.md   # agente, com teto
uv run ementa-api                                # API REST em http://127.0.0.1:8000 (/docs, /v1/legal-research)
uv run ementa-mcp --http                         # o mesmo MCP em Streamable HTTP, porta 8001
uv run lint-imports                              # contratos de dependência entre pacotes
uvx pre-commit install                           # uma vez por clone: ruff e higiene a cada commit

# A partir da F3
uv run ementa seed-users                         # seis usuários fictícios em firm.users (senhas na doc de segurança)
uv run ementa audit verify                       # confere a cadeia de hash de audit.events
docker compose --profile core up -d redis        # fila do worker, em 127.0.0.1:6379
uv run ementa-worker                             # worker arq: ingestão de POST /v1/documents e as execuções de redação (F4)
uv run ementa-worker --burst                     # roda a fila até esvaziar e sai
# Integração da fila e do worker: EMENTA_TEST_REDIS_URL=redis://127.0.0.1:6379/0, junto com EMENTA_TEST_DATABASE_URL
```

A avaliação das 30 perguntas roda no GitHub Actions (`evals.yml`) com o rótulo `avaliar` no PR, toda noite ou na mão. A parte com o modelo precisa do segredo `OPENAI_API_KEY` no repositório e para no teto de custo.

## Convenções

- Código, identificadores, nomes de arquivo e mensagens de commit em inglês. Documentação em português.
- Termos jurídicos sem tradução fiel ficam em português sem acento no código (`sumula`, `ementa`). O glossário está em `docs/ARQUITETURA.md`.
- Commits no formato Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Um branch por fase (`feat/f1-legal-research`) e um PR por fase, com o checklist do template.
- Ids fixos: agentes (`legal_research`, `assistant`, `contract_review`, `brief_drafting`, `case_monitor`, `intake`, `firm_analytics`, `lgpd_compliance`, `pr_reviewer`), papéis de modelo (`reasoning`, `default`, `fast`, `judge`, `local`, `embeddings`, `rerank`) e ids de fonte (`cdc:art49`, `cdc:art42:pu`, `cpc:art219`, `stj:sumula:385`).

## Regras de arquitetura

- `ementa_kit` nunca importa `ementa`. `ementa.domain` não importa infraestrutura. O import-linter confere no CI.
- Toda ferramenta tem schema Pydantic estrito, declara a classe de dado que devolve (público, restrito ou sigiloso) e passa pelo pipeline de guardrails.
- Cálculo (prazo, valor) é código determinístico com teste. O LLM nunca calcula.
- Toda chamada de modelo passa pelo `ModelRegistry` e pelo motor de políticas. Nunca instancie um cliente de provedor direto num agente.
- O histórico enviado ao modelo é só de acréscimo: não edite mensagem que o modelo já viu, trunque resultado de ferramenta antes de inserir, e compacte começando conversa nova a partir de um resumo.
- Todo agente, ferramenta e guardrail gera span OpenTelemetry com os atributos de `docs/OBSERVABILIDADE.md`.
- Decisão de arquitetura nova vira ADR em `docs/adr/`.

## Segurança

- Nunca use dado real de cliente, processo ou pessoa. Dados de teste são fictícios e marcados como tal.
- Nunca grave segredo no repositório. Chaves ficam em `.env`, que não é versionado; o modelo é `.env.example`.
- Corpus novo entra com URL de origem, data de coleta e hash.
- Pacote novo só depois de conferir que existe, é mantido e tem licença compatível.
- Saída de ferramenta para cliente MCP externo segue a política de saída: pseudonimiza o restrito e nega o sigiloso.

## Testes exigidos

| Mudança | Teste |
|---|---|
| Regra de domínio (prazo, citação, numeração CNJ) | Unitário, com Hypothesis quando houver propriedade |
| Parser de corpus | Unitário com arquivo de ouro |
| Ferramenta nova | Unitário do schema e teste com cliente MCP em memória |
| Guardrail novo | Casos que devem passar e casos que devem ser bloqueados |
| Consulta ao banco | Integração com Postgres real (testcontainers ou serviço do CI) |
| Prompt ou modelo | Avaliação com `-m eval`, comparada com a linha de base |

## Como adicionar

- **Ferramenta MCP:** schema em `packages/ementa/src/ementa/tools/`, registro no `apps/mcp_server`, classe de dado, testes e linha no catálogo de `docs/AGENTES.md`.
- **Skill:** pasta em `skills/<nome>/` com `SKILL.md` (nome, descrição, instruções); scripts só da lista permitida; caso de avaliação.
- **Agente:** módulo em `packages/ementa/src/ementa/agents/<id>/`, montado pelo `AgentFactory`, com grafo, estado tipado, testes com modelo falso e seção em `docs/AGENTES.md`. Agente que redige para aprovação chama `ask_for_approval` antes de finalizar, cumpre o protocolo `DraftingAgent` e entra em `services.drafting_agents()` (ver "Agentes de redação com aprovação" em `docs/AGENTES.md`).
- **Caso de avaliação:** linha no JSONL de `evals/`, com id, pergunta, citações esperadas e trechos obrigatórios.

## Definição de pronto

Código tipado e formatado, testes passando, span e guardrail no lugar, avaliação rodada se mexeu em prompt ou modelo, docs atualizados (status em `README.md`, `docs/COMPETENCIAS.md` e `docs/PLANO.md`), sem segredo, CI verde.
