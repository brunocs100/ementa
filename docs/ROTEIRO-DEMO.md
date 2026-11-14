# Roteiro da demo

Como mostrar a Ementa em entrevista: preparação, demo de 5 minutos do Marco 1, demo de 15 minutos dos marcos seguintes, perguntas prováveis e plano B.

**Status:** Documentado (a demo de 5 minutos roda a partir do Marco 1) · **Ver também:** [Resumo](RESUMO.md), [Matriz de competências](COMPETENCIAS.md), [Agentes](AGENTES.md)

> [!IMPORTANT]
> A demo é ao vivo, com tela compartilhada, e usa só dados fictícios e corpus público. O plano B é uma gravação. Os comandos abaixo ficam disponíveis no Marco 1.

## Preparação

Faça 15 minutos antes, não na frente do entrevistador.

1. Subir banco e observabilidade (disponível no Marco 1):

   ```powershell
   docker compose --profile core --profile obs up -d
   ```

2. Ingerir o CDC, se o banco for novo (disponível no Marco 1):

   ```powershell
   uv run ementa ingest cdc
   ```

3. Abrir o VS Code no repositório, conferir que o `ementa-mcp` aparece ativo nas ferramentas do Copilot em modo agente (configurado em `.vscode/mcp.json`).
4. Abrir o Grafana em `http://localhost:3000`, na tela de Explore com o Tempo selecionado.
5. Abrir o repositório no GitHub, na aba Actions, com o último CI verde.
6. Fazer uma pergunta de aquecimento, para o cache e o modelo de embeddings já estarem carregados.

Checklist rápido:

| Item | Como conferir |
|---|---|
| Postgres de pé | `docker compose ps` mostra `postgres` saudável |
| Corpus carregado | A ferramenta `search_legal_corpus` devolve o `cdc:art49` |
| Chave da OpenAI | `.env` preenchido; orçamento do dia não estourado |
| Grafana | Trace da pergunta de aquecimento aparece no Explore |

## Demo de 5 minutos (Marco 1)

### 1. Pergunta com citação verificada

- **Fazer:** no Copilot do VS Code, em modo agente: "Comprei pela internet e me arrependi. Tenho quanto tempo para desistir?"
- **Aparece:** o Copilot chama `answer_legal_question`; a resposta diz 7 dias e cita o art. 49 do CDC (`cdc:art49`), marcado como verificado, com link para o texto da lei.
- **Dizer:** "A busca é híbrida, vetorial e textual em português, com fusão RRF no Postgres. O modelo só pode citar fonte que estava no contexto, e um verificador determinístico confere cada citação antes de a resposta sair."

### 2. Prazo calculado por ferramenta

- **Fazer:** "Fui intimado na quinta, 03/12/2026, para contestar em 15 dias úteis no TJRS. Qual o último dia?"
- **Aparece:** o Copilot chama `calculate_deadline`; vencimento terça, 26/01/2027. A saída lista os dias pulados (fins de semana, feriados, suspensão de 20/12 a 20/01 do art. 220 do CPC), as regras aplicadas (arts. 219, 224 e 220) e as premissas (termo inicial informado pelo usuário; feriados municipais e portarias do tribunal fora do cálculo).
- **Dizer:** "Modelo de linguagem não conta dias úteis. O cálculo é código com teste fixo e testes por propriedade; a ferramenta declara as premissas para o advogado conferir."

### 3. Citação inventada é removida

- **Fazer:** "Cite a Súmula 999 do STJ sobre direito de arrependimento."
- **Aparece:** a resposta não traz a súmula; um aviso diz que a citação não foi encontrada no corpus e foi removida.
- **Dizer:** "Citação falsa é o risco mais conhecido de IA em peça jurídica. Aqui ela não passa: o verificador remove e avisa; em peça, a partir da F4, ele bloqueia."

### 4. O trace no Grafana

- **Fazer:** abrir no Grafana o trace da pergunta do passo 1.
- **Aparece:** a cascata `invoke_agent`, `retrieval` (vetorial, textual, fusão), `chat` (modelo, tokens de entrada e saída, custo em `ementa.cost_usd`, id e versão do prompt) e o span do verificador.
- **Dizer:** "Os spans seguem as convenções GenAI do OpenTelemetry, então o mesmo trace vai para qualquer backend compatível com OTLP. Custo por execução é atributo do span, não planilha."

### 5. Engenharia no GitHub

- **Fazer:** mostrar o CI verde, um teste (o caso fixo do prazo) e a lista de ADRs.
- **Aparece:** `ci.yml` com ruff, mypy e pytest; o teste de integração da busca; os 12 ADRs.
- **Dizer:** "O que vem depois está no plano com status honesto. Cada decisão tem ADR com as alternativas que eu descartei e por quê."

> [!WARNING]
> O modelo do cliente MCP (Copilot, Claude Desktop) vê o que o usuário digita. A Ementa protege as chamadas dela ao modelo e o que as ferramentas devolvem. Por isso a demo de 5 minutos só usa dados públicos.

## Demo de 15 minutos (marcos seguintes)

Começa pelos 5 passos acima e acrescenta estes, conforme o marco já entregue.

| Passo | Marco | O que fazer | O que mostrar | O que dizer |
|---|---|---|---|---|
| Pseudonimização | 2 | Pergunta com um CPF fictício pela rota `POST /v1/legal-research` | No trace, o CPF virou marcador antes da chamada ao modelo da Ementa e voltou na resposta | "O provedor de nuvem nunca vê o dado pessoal; a reidratação acontece do nosso lado" |
| Peça com aprovação humana | 3 | Pedir uma petição inicial de consumo no console | O `brief_drafting` para em `human_review`; o advogado edita e aprova; sai o DOCX | "A IA rascunha, o advogado decide; a edição dele vira memória episódica" |
| Conflito de interesses | 3 | Triagem de um cliente cuja parte contrária é sócia de um cliente atual | O `intake` aponta o conflito pelo caminho no grafo | "Conflito é pergunta de relacionamento; o grafo responde em uma consulta Cypher" |
| Sigilo para o modelo local | 3 | Pergunta sobre um processo marcado como segredo de justiça | O trace mostra `ementa.policy.route = local` e a chamada indo para o Ollama | "A política decide a rota antes da chamada; sigiloso não sai da máquina" |
| Avaliação no PR | 2 | Abrir um PR que muda o prompt da pesquisa | O `evals.yml` comenta a tabela de recall, citação e custo contra o `main` | "Mudou prompt, roda a régua; regressão bloqueia o merge" |

## Perguntas prováveis

| Pergunta | Resposta curta | Onde está |
|---|---|---|
| Por que LangGraph e não um loop próprio? | Grafo explícito, checkpointer, `interrupt()` para HITL e `Send` para map-reduce, sem reinventar | [ADR 0002](adr/0002-langgraph-como-orquestrador.md) |
| Por que pgvector e não um banco vetorial dedicado? | Uma base só, RLS aplicando permissão no próprio RAG, FTS em português; volume pequeno | [ADR 0003](adr/0003-postgres-pgvector-busca-hibrida.md) |
| Como evita citação inventada? | O modelo só cita ids do contexto; verificador determinístico remove ou bloqueia | [Agentes](AGENTES.md), [ADR 0006](adr/0006-guardrails-em-pipeline.md) |
| Como protege dado de cliente? | Pseudonimização antes da nuvem, roteamento por sigilo, RLS, classificação de saída no MCP | [Segurança e governança](SEGURANCA-E-GOVERNANCA.md) |
| E se trocar de provedor? | Camada de modelos por papel; troca é configuração; histórico só de acréscimo deixa a troca segura | [ADR 0005](adr/0005-camada-de-modelos-agnostica.md) |
| Como sabe que uma mudança não piorou? | Avaliação com gabarito tirado do texto da lei, gate no CI contra o `main`, teto de custo | [Avaliação](AVALIACAO.md) |
| Quanto custa? | Cerca de US$ 0,015 por pergunta e US$ 0,75 por rodada de avaliação; orçamento com parada automática | [Observabilidade](OBSERVABILIDADE.md) |
| O que é MCP e por que usar? | Protocolo padrão para expor ferramentas a clientes de IA; o mesmo servidor serve Copilot, Claude e Semantic Kernel | [ADR 0007](adr/0007-mcp-como-padrao-de-integracao.md) |
| Onde entra o fine-tuning? | Triagem com modelo pequeno ajustado, servido local, comparado com Luna e Terra | [ML e fine-tuning](ML-E-FINE-TUNING.md) |
| O que já está pronto? | O status de cada competência está na matriz e é atualizado por fase | [Matriz de competências](COMPETENCIAS.md) |

## Como fechar

Em 30 segundos, depois da demo:

1. O problema: escritório precisa de IA que não invente citação, não erre prazo e não vaze dado de cliente.
2. A resposta: verificação determinística onde o erro é caro, política antes de toda chamada ao modelo, trace de tudo.
3. O jeito de trabalhar: decisão com ADR, avaliação no CI, status honesto por fase.
4. O próximo passo: o que o próximo marco entrega, lido direto do [Plano](PLANO.md).

O que evitar:

- Dizer que algo está pronto quando a matriz diz "Documentado".
- Mostrar dado que pareça real: CPF, nome e processo da demo são fictícios.
- Prometer acerto jurídico: a Ementa rascunha; quem assina é o advogado.

## Plano B

- **Gravação:** gravar a demo de 5 minutos assim que o Marco 1 fechar e guardar o link junto do README.
- **Prints:** salvar em `docs/assets/` a resposta com citação verificada, a saída do prazo, a citação removida e o trace no Grafana.
- **API da OpenAI fora do ar ou orçamento estourado:** `calculate_deadline` e `search_legal_corpus` funcionam sem LLM. Mostrar o prazo, a busca devolvendo o `cdc:art49` e um trace já gravado.
- **Docker com problema:** mostrar os prints, o CI no GitHub e os docs; a arquitetura se explica pelos diagramas.
- **Copilot sem cota no plano Free:** usar o Claude Desktop ou o MCP Inspector apontando para o mesmo `ementa-mcp` (conferir limites do plano Free na F1).

---

[Voltar ao README](../README.md)
