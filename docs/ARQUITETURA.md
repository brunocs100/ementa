# Arquitetura

Como a Ementa é montada: princípios, arquitetura de referência, containers, componentes, fluxos, dados, modelos, contexto, memória, MCP e skills.

**Status:** Documentado; implementação a partir da F0 · **Ver também:** [Agentes](AGENTES.md), [Segurança e governança](SEGURANCA-E-GOVERNANCA.md), [Observabilidade](OBSERVABILIDADE.md), [ADRs](adr/README.md)

## Princípios

| Princípio | Na prática |
|---|---|
| Determinístico onde dá | Prazo, verificação de citação, SQL guardado e parsing são código testado; o LLM decide e redige |
| A IA rascunha, o advogado decide | Ação externa e peça final passam por aprovação humana (`interrupt()` do LangGraph) |
| Defesa em profundidade | RBAC na API, política antes do modelo, RLS no banco, guardrails na saída e no MCP |
| Dado sensível não sai sem necessidade | Embeddings locais, pseudonimização antes da nuvem, sigilo vai para o modelo local |
| Observável por padrão | Todo agente, chamada de modelo, busca, ferramenta e guardrail é um span |
| Agnóstico de provedor | Papéis de modelo no `ModelRegistry`; trocar provedor é configuração |
| Histórico só de acréscimo | O que o modelo já viu não é editado; mantém o cache e a portabilidade |
| Kit separado do domínio | O `ementa-kit` não conhece direito; o domínio jurídico usa o kit |
| Portas e adaptadores | Arquitetura hexagonal: o domínio fala com portas (`VectorStore`, `GraphStore`, `ChatModel`, `DocumentStorage`); Postgres, Neo4j e provedores são adaptadores trocáveis |

Os diagramas seguem os níveis do modelo C4: contexto, containers e componentes.

## Padrões de projeto

| Padrão | Onde | Para quê |
|---|---|---|
| Strategy | Provedores no `ModelRegistry` | Trocar OpenAI, Ollama ou Anthropic sem mudar o agente |
| Adapter | `VectorStore`, `GraphStore`, `DocumentStorage` | Isolar Postgres, Neo4j e disco atrás de portas |
| Chain of Responsibility | Pipeline de guardrails | Cada verificação decide permitir, mascarar, marcar ou bloquear |
| Repository | Acesso a `firm` e `knowledge` | Consultas e RLS num lugar só, testáveis com banco real |
| Builder | `AgentFactory` | Montar modelo, ferramentas, guardrails, memória e telemetria de um agente |
| Template Method | Template Copier `novo-agente` | Esqueleto fixo com os pontos de extensão de cada agente |
| Observer | Eventos de telemetria e auditoria | Guardrail, ferramenta e modelo publicam; span e trilha registram |

## Arquitetura de referência

A parte genérica, que qualquer time pode reaproveitar com o `ementa-kit`. O domínio jurídico é só uma instância dela.

```mermaid
flowchart TB
    canais["Canais: MCP, REST, console, eventos"]
    orq["Orquestração: grafos LangGraph"]
    subgraph kit["ementa-kit"]
        pol["Políticas e guardrails"]
        ctx["Contexto e prompts"]
        mem["Memória"]
        rag["RAG"]
        llm["ModelRegistry"]
        tel["Telemetria e auditoria"]
        evl["Avaliação"]
    end
    dados[("Postgres, Neo4j, Redis")]
    prov["Provedores: OpenAI, Ollama, Anthropic"]
    obs["Backend OTLP"]
    canais --> pol
    pol --> orq
    orq --> ctx
    orq --> mem
    orq --> rag
    ctx --> llm
    rag --> dados
    mem --> dados
    llm --> prov
    tel -.-> obs
    evl -.-> orq
```

| Camada | Responsabilidade | Contrato para o domínio |
|---|---|---|
| Canais | Autenticar, limitar taxa, traduzir para chamada de agente | Ferramenta com schema Pydantic e classe de dado |
| Políticas e guardrails | Decidir se pode, por qual modelo, e o que sai | Checks plugáveis em entrada, ferramenta e saída |
| Orquestração | Grafo com estado, checkpoint, HITL, paralelismo | `AgentFactory` monta o grafo com tudo ligado |
| Contexto e prompts | Montar o que vai ao modelo, com orçamento | Seções declaradas e prompts versionados |
| Memória | Curta, longa, episódica e esquecimento | Namespaces e permissões herdadas |
| RAG | Ingestão, busca híbrida, rerank, grafo | Porta `VectorStore` e `GraphStore` |
| ModelRegistry | Papéis, provedores, custo, orçamento | Nunca instanciar provedor no agente |
| Telemetria e auditoria | Spans, métricas, trilha imutável | Atributos `gen_ai.*` e `ementa.*` |
| Avaliação | Datasets, métricas, gates | JSONL e marcador `eval` no pytest |

## Contexto (C4 nível 1)

```mermaid
flowchart LR
    adv["Advogados e sócia"]
    ctrl["Controladoria e DPO"]
    dev["Time de plataforma"]
    ementa["Ementa"]
    openai["OpenAI"]
    datajud["DataJud do CNJ"]
    corpus["Planalto, STJ e STF"]
    email["E-mail, Teams ou Slack"]
    github["GitHub"]
    adv -->|"VS Code, Claude, console"| ementa
    ctrl -->|"console e alertas"| ementa
    dev -->|"Copilot e PRs"| github
    github -->|"revisor de PR"| ementa
    ementa -->|"modelos de nuvem"| openai
    ementa -->|"metadados de processos"| datajud
    ementa -->|"leis, súmulas, decisões"| corpus
    ementa -->|"avisos"| email
```

## Containers (C4 nível 2)

| Serviço | Imagem ou execução | Perfil | Porta | RAM aproximada | Fase |
|---|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | `core` | 5432 | 300 MB | F0 |
| `otel-lgtm` | `grafana/otel-lgtm` (OTel Collector, Prometheus, Tempo, Loki, Pyroscope) | `obs` | 3000, 4317, 4318 | 1 a 1,5 GB | F0 |
| `mcp` | `apps/mcp_server` (HTTP); stdio roda local | `mcp` | 8100 | 300 MB | F2 |
| `api` | `apps/api` (FastAPI e console) | `core` | 8000 | 500 MB | F2 |
| `redis` | `redis:7` | `core` | 6379 | 50 MB | F3 |
| `worker` | `apps/worker` (arq) | `core` | — | 500 MB a 1 GB (embeddings) | F3 |
| `neo4j` | `neo4j:5` Community | `grafo` | 7474, 7687 | 1 GB | F5 |
| `mailpit` | `axllent/mailpit` | `mail` | 1025, 8025 | 50 MB | F5 |
| `mlflow` | MLflow com SQLite | `ml` | 5000 | 300 MB | F9 |
| `compliance-sk` | `apps/compliance_sk` | `ms` | 8200 | 300 MB | F8 |
| Ollama | Nativo no Windows | (host) | 11434 | 3 a 4 GB com modelo de 3B | F6 |

Com 15,4 GB de RAM e 8 GB para o WSL2, o dia a dia usa `core` e `obs`; os demais perfis sobem só quando a fase precisa.

## Componentes (C4 nível 3)

```mermaid
flowchart LR
    subgraph apps["apps"]
        a_api["api"]
        a_mcp["mcp_server"]
        a_wrk["worker"]
        a_sk["compliance_sk"]
    end
    subgraph dominio["ementa"]
        d_agents["agents"]
        d_tools["tools"]
        d_ingest["ingest"]
        d_integ["integrations"]
        d_domain["domain"]
    end
    k_kit["ementa_kit"]
    a_api --> d_agents
    a_mcp --> d_tools
    a_wrk --> d_ingest
    a_sk --> a_mcp
    d_agents --> d_tools
    d_tools --> d_domain
    d_agents --> k_kit
    d_ingest --> k_kit
    d_integ --> k_kit
```

Regras do import-linter:

- `ementa_kit` não importa `ementa` nem `apps`.
- `ementa.domain` não importa `ementa_kit`, `ementa.integrations` nem bibliotecas de infraestrutura.
- `apps` importam só módulos públicos de `ementa` e `ementa_kit`.

## Estrutura de pastas

```text
ementa/
├── packages/ementa-kit/src/ementa_kit/
│   ├── config/  llm/  telemetry/  guardrails/  policy/  rag/
│   └── context/  memory/  skills/  mcp/  evals/  audit/
├── packages/ementa/src/ementa/
│   ├── domain/  ingest/  integrations/  tools/  agents/  prompts/
├── apps/  api/  worker/  mcp_server/  compliance_sk/
├── skills/  data/  evals/  ml/  infra/  templates/novo-agente/
├── .github/  .vscode/mcp.json
└── docs/
```

## Fluxos

### Pesquisa com citação verificada (F1)

```mermaid
sequenceDiagram
    actor U as Advogado
    participant C as Copilot no VS Code
    participant M as ementa-mcp
    participant A as legal_research
    participant R as RAG no Postgres
    participant L as OpenAI
    U->>C: pergunta sobre o CDC
    C->>M: answer_legal_question
    M->>M: guardrails de entrada
    M->>A: pergunta e papel do usuário
    A->>R: busca vetorial e textual, fusão RRF
    R-->>A: trechos com ids de fonte
    A->>L: contexto montado, cita só ids do contexto
    L-->>A: rascunho com citações
    A->>A: verify_citations, uma nova tentativa se falhar
    A-->>M: resposta, citações verificadas, aviso
    M->>M: política de saída para cliente externo
    M-->>C: resposta e fontes
```

O grafo do agente, nó por nó, está em [Agentes](AGENTES.md).

### Peça com revisão humana (F4)

```mermaid
sequenceDiagram
    actor A as Advogado
    participant W as Console
    participant G as brief_drafting
    participant P as legal_research
    participant L as OpenAI
    A->>W: pede contestação com os fatos
    W->>G: inicia a execução
    G->>P: pesquisa cada tese em paralelo
    P-->>G: fontes verificadas
    G->>L: rascunho por template
    G->>L: crítica com rubrica
    G->>G: verifica citações
    G-->>W: interrupt, aguardando aprovação
    A->>W: aprova com edições
    W->>G: retoma do checkpoint
    G-->>W: DOCX com aviso de rascunho
```

### Prazos via DataJud (F5)

```mermaid
sequenceDiagram
    participant S as Agendador no worker
    participant K as case_monitor
    participant D as DataJud
    participant T as calculate_deadline
    participant B as Postgres
    participant E as E-mail
    S->>K: rodada diária
    K->>D: movimentações dos processos ativos
    D-->>K: metadados
    K->>K: diferença com o que já existe
    K->>K: classifica a movimentação
    K->>T: calcula o prazo
    T-->>K: vencimento e premissas
    K->>B: grava prazo e tarefa
    K->>E: avisa o responsável
```

### Roteamento por sigilo (F6)

```mermaid
flowchart LR
    req["Pedido com processo"] --> pol{"Nível de sigilo"}
    pol -->|"público ou restrito"| pii["Pseudonimiza PII"]
    pii --> nuvem["Papel default na OpenAI"]
    pol -->|"segredo de justiça"| local["Papel local no Ollama"]
    nuvem --> rehid["Reidrata para o usuário autorizado"]
    local --> rehid
    rehid --> out["Resposta e auditoria"]
```

## Modelo de dados

```mermaid
erDiagram
    users ||--o{ case_members : participa
    cases ||--o{ case_members : tem
    clients ||--o{ cases : contrata
    cases ||--o{ parties : envolve
    cases ||--o{ movements : recebe
    cases ||--o{ deadlines : gera
    cases ||--o{ documents : guarda
    sources ||--o{ chunks : divide
    cases ||--o{ chunks : restringe
    runs ||--o{ tool_calls : faz
    runs ||--o{ guardrail_events : registra
    runs ||--o{ approvals : pede
    users {
        uuid id
        text name
        text role
    }
    cases {
        uuid id
        text cnj_number
        text area
        text secrecy_level
    }
    chunks {
        uuid id
        uuid source_id
        uuid case_id
        text source_ref
        text body
        vector embedding
        tsvector tsv
        text secrecy_level
    }
    runs {
        uuid id
        text agent
        uuid user_id
        text model
        int input_tokens
        int output_tokens
        numeric cost_usd
    }
```

Schemas: `firm` (users, clients, cases, case_members, parties, deadlines, movements, documents, time_entries), `knowledge` (sources, chunks), `ai` (runs, tool_calls, guardrail_events, approvals, feedback, eval_runs, eval_results, budgets), `audit` (events, só inserção, com hash encadeado), `analytics` (views para o BI) e `langgraph` (checkpointer e store).

RLS nos chunks: trecho público (corpus) aparece para todos; trecho de processo só para quem é membro do processo.

```sql
ALTER TABLE knowledge.chunks ENABLE ROW LEVEL SECURITY;

CREATE POLICY chunks_read ON knowledge.chunks
FOR SELECT USING (
    case_id IS NULL
    OR EXISTS (
        SELECT 1
        FROM firm.case_members m
        WHERE m.case_id = knowledge.chunks.case_id
          AND m.user_id = current_setting('app.user_id')::uuid
    )
);
```

A aplicação faz `SET LOCAL app.user_id = '<id>'` em cada transação; o papel da aplicação não tem `BYPASSRLS`.

Busca textual em português sem acento: o acento sai no Python (`ementa_kit.rag.fold`), no documento e na consulta, antes do `to_tsvector('portuguese', ...)`. Assim a busca não depende da extensão `unaccent` estar instalada (decisão da F1, ver [ADR 0003](adr/0003-postgres-pgvector-busca-hibrida.md)):

```sql
search_text text NOT NULL,  -- título, caminho e texto, em minúsculas e sem acento
tsv tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', search_text)) STORED
```

## Grafo de conhecimento

| Nó | Exemplo | Origem |
|---|---|---|
| `Statute` | CDC, LGPD, CPC | Corpus |
| `Article` | `cdc:art49` | Parser |
| `Sumula` | `stj:sumula:385` | Corpus |
| `Decision` | Decisão da amostra do STJ | Dados abertos |
| `Court`, `Topic` | STJ; "cobrança indevida" | Metadados |
| `Client`, `Person`, `Company`, `Case`, `Lawyer` | Partes e responsáveis, por hash com sal | Postgres |

Arestas: `PART_OF`, `CITES`, `APPLIES`, `INTERPRETS`, `ABOUT`, `PARTY_IN {side}`, `RESPONSIBLE_FOR`, `RELATED_TO`.

Súmulas e decisões ligadas a um artigo (expansão do GraphRAG):

```cypher
MATCH (a:Article {id: $article_id})<-[:CITES|INTERPRETS]-(x)
WHERE x:Sumula OR x:Decision
RETURN x.id, labels(x)[0] AS kind
LIMIT 10
```

Conflito de interesses: caminho de até 3 saltos entre a nova parte contrária e um cliente atual.

```cypher
MATCH (novo:Person {hash: $party_hash}), (c:Client)
MATCH p = shortestPath((novo)-[:PARTY_IN|RELATED_TO*..3]-(c))
WHERE NOT EXISTS {
    MATCH (novo)-[s1:PARTY_IN]->(k:Case)<-[s2:PARTY_IN]-(c)
    WHERE s1.side = s2.side
}
RETURN c.id, length(p) AS hops, [n IN nodes(p) | labels(n)[0]] AS path
ORDER BY hops
LIMIT 5
```

## Modelos

| Papel | Uso | Referência | Observação |
|---|---|---|---|
| `reasoning` | Redação, crítica, contratos | GPT-6.1 Terra, effort high | Sol é candidato, medido na régua |
| `default` | Resposta da pesquisa, supervisor | GPT-6 Luna, effort medium | Terra antes de 15/11/2026; trocado por custo (10 vezes mais barato) |
| `fast` | Roteamento, extração, classificação | GPT-6 Luna | Candidato ao modelo local ajustado (F9) |
| `judge` | Avaliações, em amostra | GPT-6.1 Sol | Modelo diferente do que responde |
| `local` | Sigiloso, triagem ajustada | Modelo aberto de 3 a 4B no Ollama | CPU |
| `embeddings` | RAG | Multilíngue local | Escolhido por recall@k |
| `rerank` | RAG | Cross-encoder multilíngue local | F2 |

```yaml
# infra/models.yaml
roles:
  reasoning: {provider: openai, model: gpt-6.1-terra, effort: high}    # conferir id na F1
  default:   {provider: openai, model: gpt-6.1-terra, effort: medium}  # conferir id na F1
  fast:      {provider: openai, model: gpt-6-luna}                     # conferir id na F1
  judge:     {provider: openai, model: gpt-6.1-sol, effort: medium}    # conferir id na F1
  local:     {provider: ollama, model: "<modelo de 3 a 4B>"}
budget:
  usd_per_day: 2
  usd_per_month: 30
  on_exceed: stop
```

- Preços de referência: Terra US$ 1 / US$ 5 por milhão de tokens; Luna US$ 0,10 / US$ 0,50; Sol US$ 2 / US$ 10. A conferir na página oficial no começo da F1.
- Recursos da OpenAI no desenho: Responses API com `store: false` e raciocínio criptografado devolvido intacto a cada turno; Structured Outputs e function calling com `strict`; cache de prompt automático no prefixo estável, com `prompt_cache_key`; esforço de raciocínio por rota; endpoint de moderação como guardrail opcional. Nomes de parâmetro a conferir na F1.
- Acesso via chat models do LangChain (ChatOpenAI, ChatOllama e, por configuração, ChatAnthropic). Se o LangChain não expuser um recurso, a rota usa o SDK oficial direto.
- Detalhes e alternativas no [ADR 0005](adr/0005-camada-de-modelos-agnostica.md).

## RAG

| Etapa | Como | Fase |
|---|---|---|
| Coleta | Snapshot com URL, data e hash; testes rodam sobre o snapshot | F1 |
| Parsing | Lei, título, capítulo, artigo, parágrafo e inciso; id canônico (`cdc:art42:pu`) | F1 |
| Chunk | Um por artigo, com o caminho completo no cabeçalho; artigo longo dividido por parágrafo | F1 |
| Embedding | Modelo multilíngue local; versão gravada na linhagem | F1 |
| Índice | HNSW no `embedding`, GIN no `tsv` (português, acento tirado no Python) | F1 |
| Consulta | Citação explícita vira busca direta; senão multi-query com sinônimos jurídicos | F1 |
| Busca | Vetorial e textual, top 30 cada, fusão RRF com k = 60 | F1 |
| Rerank | Cross-encoder, top 8 | F2 |
| Grafo | Súmulas e decisões ligadas aos artigos recuperados | F5 |
| Permissão | RLS no próprio banco | F1 (corpus público), F6 (processos) |

Fusão RRF numa consulta só (versão resumida; a completa está em `ementa_kit/rag/store.py`). Os termos da consulta textual entram com OU, não com E, porque a pergunta vem em linguagem natural:

```sql
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY distance, id) AS r
    FROM (SELECT id, embedding <=> $1 AS distance
          FROM knowledge.chunks ORDER BY distance LIMIT 30) AS nearest
), query AS (
    SELECT replace(plainto_tsquery('portuguese', $2)::text, '&', '|')::tsquery AS tsq
), txt AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC, c.id) AS r
    FROM knowledge.chunks c, query q
    WHERE c.tsv @@ q.tsq
    ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC, c.id
    LIMIT 30
)
SELECT id, coalesce(1.0 / (60 + vec.r), 0) + coalesce(1.0 / (60 + txt.r), 0) AS score
FROM vec FULL OUTER JOIN txt USING (id)
ORDER BY score DESC
LIMIT 8;
```

Com várias consultas (a pergunta e as reescritas do modelo `fast`), cada uma roda essa busca e o Python funde os rankings de novo por RRF.

## Engenharia de prompt e de contexto

Prompts são arquivos versionados em `packages/ementa/src/ementa/prompts/`, com id, versão e changelog. Cada span de chamada grava `ementa.prompt.id` e `ementa.prompt.version`; mudou o prompt, roda a avaliação. O `PromptRegistry` do kit carrega todos de uma vez e falha ao montar se dois arquivos tiverem o mesmo id e versão ou se o changelog não tiver a entrada da própria versão (`v2 ...` no changelog da versão 2); `get_prompt` lê dele.

O montador de contexto (`ementa_kit.context`) monta cada chamada a partir de seções com orçamento de tokens:

| Ordem | Seção | Orçamento inicial | Observação |
|---|---|---|---|
| 1 | Sistema e regras | 1.500 | Congela no início da conversa; entra no cache |
| 2 | Definições de ferramentas | 1.000 | Lista fixa por conversa |
| 3 | Índice de skills | 300 | Só nome e descrição (F2) |
| 4 | Memórias relevantes | 600 | Top-k por namespace (F3) |
| 5 | Fontes recuperadas | 3.500 | Com id de fonte em cada trecho |
| 6 | Resumo da conversa | 800 | Só depois de compactar |
| 7 | Últimos turnos e resultados de ferramenta | 2.000 | Resultado truncado antes de entrar |
| 8 | Pergunta | — | Sempre por último |

O tamanho de cada seção vai como evento no trace: `build_context` grava `ementa.context.section_tokens` no span corrente (no `legal_research`, o `invoke_agent`), com `<seção>.chars` e `<seção>.tokens` para `system`, cada seção e a `pergunta`. Os tokens são aproximados, 4 caracteres por token; o número exato é o uso que o provedor devolve no span `chat`. As técnicas aparecem com nome: escrever (estado e rascunho no grafo), selecionar (busca, memória e skills sob demanda), comprimir (truncar resultado, resumir conversa) e isolar (cada subagente com o próprio contexto).

Regras do histórico só de acréscimo:

- O prompt de sistema e a lista de ferramentas congelam no início da conversa.
- Contexto novo entra como mensagem nova, nunca editando uma anterior.
- Resultado de ferramenta é truncado antes de o modelo ver, nunca depois.
- Compactar é começar uma conversa nova a partir de um resumo.
- Cada subagente tem a própria conversa.
- O raciocínio criptografado da OpenAI volta intacto a cada turno.
- O estado completo fica no checkpointer para auditoria; a visão que vai ao modelo é derivada dele.

O motivo é duplo: o cache de prompt só funciona com prefixo estável, e alguns provedores recusam histórico editado (o Claude devolve erro 400 quando o prefixo de um bloco de raciocínio muda; conferir na documentação da Anthropic). Um teste no CI compara pedidos consecutivos e falha se o prefixo mudar (F2): `assert_append_only` (`ementa_kit.context`) recebe os pedidos que um modelo falso gravou e aponta a mensagem e o caractere que mudaram; o caminho de nova tentativa do `legal_research` passa por ele.

## Memória

| Tipo | Onde | Exemplo | Fase |
|---|---|---|---|
| Curta | Checkpointer do LangGraph no Postgres, por thread | A conversa atual | F3 |
| Longa | Store do LangGraph com índice vetorial | Preferência de estilo do advogado; ficha do processo; teses aprovadas | F3 |
| Episódica | Store, namespace por tipo de peça | Rascunho aprovado e a edição do advogado | F4 |
| Procedural | Skills e prompts versionados | Como revisar um contrato de serviços | F2, F4 |

- Namespaces: `("lawyer", id)`, `("case", id)`, `("firm",)`.
- A memória herda a permissão do processo; a leitura passa pela mesma regra do RLS.
- Escrita depois da execução, com deduplicação por similaridade e confiança mínima.
- Direito do titular (LGPD, art. 18): `DELETE /v1/data-subjects/{id}/memories` apaga as memórias e os chunks ligados àquela pessoa.

## Padrões multiagente

| Padrão | Onde | Por quê |
|---|---|---|
| RAG agêntico com reflexão | `legal_research` | Refaz a busca quando a citação não se sustenta |
| Supervisor | `assistant` | Um ponto de entrada, roteamento barato pelo papel `fast` |
| Map-reduce | `contract_review` | Cláusulas independentes em paralelo (`Send`) |
| Plan-and-execute com crítico | `brief_drafting` | Peça longa precisa de plano, revisão e verificação |
| Workflow agendado | `case_monitor` | Previsível; LLM só onde precisa |
| Humano no circuito | `contract_review`, `brief_drafting`, `intake` | Responsabilidade do advogado |
| Agente remoto por protocolo | `lgpd_compliance` | Semantic Kernel conversando pelo MCP |

## MCP

| Item | Detalhe |
|---|---|
| Servidor | `ementa-mcp`, SDK oficial em Python, spec 2026-07-28 (sem estado: sem handshake nem sessão; conferir na F1) |
| Transportes | stdio local (F1); HTTP atrás de balanceador (F2) |
| Ferramentas | `search_legal_corpus`, `answer_legal_question`, `get_statute_article`, `calculate_deadline` (F1); `list_skills`, `load_skill` (F2); `review_contract` (F4); `lookup_case`, `check_conflict` (F5); `query_firm_analytics` (F6) |
| Recursos | `ementa://statutes/{id}/articles/{n}`, `ementa://templates/{kind}`, `ementa://playbooks/contracts` |
| Prompts | `review-contract`, `summarize-case` |
| Autenticação | stdio sem token; HTTP com JWT (F3) e OAuth (F6) |
| Saída | Cada ferramenta declara a classe de dado; restrito sai pseudonimizado; sigiloso é negado com aviso |

O cliente MCP de terceiro (Copilot, Claude Desktop) é tratado como modelo de nuvem: ele vê o que o usuário digita e o que a ferramenta devolve. Por isso a política de saída vale para toda resposta de ferramenta. Configuração no VS Code:

```json
{
  "servers": {
    "ementa": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "ementa-mcp"]
    }
  }
}
```

## Skills

| Tipo | Onde | Exemplos | Quem usa |
|---|---|---|---|
| Runtime | `skills/<nome>/SKILL.md` | `legal-research`, `deadline-calculation`, `contract-review-services`, `initial-petition-consumer`, `lgpd-checklist` | Agentes da Ementa |
| Desenvolvimento | `.github/skills/<nome>/SKILL.md` | `new-mcp-tool`, `new-legal-skill`, `new-eval-case`, `investigate-trace` | Copilot e Claude Code trabalhando no repo |

Os dois tipos seguem o padrão aberto Agent Skills. O loader do kit faz divulgação progressiva: o prompt recebe só nome e descrição; a ferramenta `load_skill` traz as instruções completas quando o agente precisa; scripts rodam só se estiverem na lista permitida, com tempo limite.

O loader de runtime (`ementa_kit.skills`, F2):

| Item | Como funciona |
|---|---|
| Onde | `skills/<nome>/SKILL.md` na raiz do repositório (`ementa.services.SKILLS_DIR`); `services.skill_library()` monta a biblioteca uma vez por processo |
| Formato | Front matter YAML com `name` (igual ao nome da pasta; minúsculas, dígitos e hífen simples; até 64 caracteres) e `description` (até 1.024); opcionais `license`, `compatibility`, `metadata` e `allowed-tools`. Campo desconhecido ou corpo vazio é erro |
| Falha | Uma skill malformada derruba a montagem da biblioteca, com o arquivo e o motivo |
| Índice | `index()` e `render_index()`: só nome e descrição, dentro dos 300 tokens da seção (teste) |
| Instruções | `load_skill(nome)`; pelo MCP, `list_skills` e `load_skill` |
| Scripts | `run_script(skill, script)` roda `scripts/<script>` da skill (só `.py`) se `"<skill>/<script>"` estiver em `EMENTA_SKILL_SCRIPTS_ALLOWED`, uma lista JSON vazia por padrão, como `["deadline-calculation/check.py"]`. Roda com o Python do processo, sem shell, na pasta da skill, com um ambiente reduzido (`PATH`, pastas temporárias e idioma; chaves e banco ficam de fora) e com o limite de `EMENTA_SKILL_SCRIPT_TIMEOUT_S` (10 s). O resto é recusado e registrado |

## API REST

| Rota | Faz | Fase |
|---|---|---|
| `POST /v1/legal-research` | Pesquisa com citações | F2 |
| `POST /v1/auth/token` | Login OAuth2 de senha; devolve JWT de 15 minutos | F3 |
| `GET /v1/audit/events` | Trilha de auditoria, paginada por cursor (sócia, DPO e TI) | F3 |
| `POST /v1/chat` | Conversa com o `assistant`, em SSE | F3 |
| `GET /v1/threads/{id}` | Histórico de uma conversa | F3 |
| `POST /v1/documents` | Upload e ingestão em fila | F3 |
| `GET /v1/jobs/{id}` | Estado de uma tarefa longa | F3 |
| `POST /v1/contracts/{id}/review` | Inicia a análise de contrato | F4 |
| `POST /v1/briefs` | Inicia a redação de peça | F4 |
| `POST /v1/approvals/{id}` | Aprova, rejeita ou edita um passo pendente | F4 |
| `GET /v1/deadlines` | Prazos a vencer | F5 |
| `POST /v1/intake` | Triagem e conflito | F5 |
| `POST /v1/analytics/query` | Pergunta de gestão em SQL guardado | F6 |
| `DELETE /v1/data-subjects/{id}/memories` | Esquecimento (LGPD) | F3 |
| `GET /healthz`, `GET /readyz` | Saúde e prontidão | F2 |

Convenções: JWT de curta duração com papel; erros no formato RFC 9457 (`application/problem+json`); `Idempotency-Key` nos POST que iniciam tarefa; paginação por cursor; limite de taxa por usuário no Redis; OpenAPI com exemplos.

## Falhas e resiliência

| Falha | Tratamento |
|---|---|
| Provedor lento | Timeout por papel; streaming com evento de progresso |
| Erro transitório (429, 5xx) | Retry com backoff exponencial e jitter, limitado |
| Provedor fora | Circuit breaker por provedor; fallback configurado por papel ou erro claro |
| Orçamento estourado | Parada antes da chamada, com erro explicando o limite |
| Saída fora do schema | Rejeita e refaz uma vez; depois falha com o motivo |
| Citação não verificada | Remove e avisa; em peça, bloqueia a finalização |
| Worker reiniciado | Retoma do checkpoint; tarefas idempotentes |
| DataJud indisponível | Nova tentativa na próxima rodada; alerta depois de 24 h |

## Glossário

| Português (docs) | Código | Observação |
|---|---|---|
| Processo | `case` | Número no padrão CNJ em `cnj_number` |
| Movimentação | `movement` | Vem do DataJud |
| Prazo | `deadline` | Calculado por `calculate_deadline` |
| Intimação | `start_date` | Termo inicial informado pelo usuário |
| Peça | `brief` | Petição, contestação, recurso |
| Súmula | `sumula` | Sem tradução fiel |
| Ementa | `ementa` | Nome do produto e resumo de decisão |
| Parte | `party` | Com lado (`side`) |
| Cliente | `client` | Pessoa ou empresa atendida |
| Sigilo | `secrecy_level` | `public`, `restricted`, `secret` |
| Barreira ética | RLS | Por `case_members` |
| Triagem | `intake` | Agente e fluxo |
| Escritório | `firm` | Schema e namespace |
| Fonte | `source` | Lei, súmula, decisão ou documento |
| Trecho | `chunk` | Unidade de busca |

---

[Voltar ao README](../README.md)
