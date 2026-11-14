# ADR 0004: Neo4j para o grafo jurídico e o conflito de interesses

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F5 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Agentes](../AGENTES.md)

## Contexto

Duas perguntas da Ementa são, por natureza, perguntas de grafo.

**Citação.** "Que súmulas e decisões interpretam o art. 42, parágrafo único, do CDC?" A resposta está nas ligações: a decisão cita o artigo, aplica a súmula, e a súmula interpreta o artigo. A busca vetorial acha o texto parecido, mas não segue a ligação.

**Conflito de interesses.** Antes de aceitar um cliente novo, o escritório precisa saber se ele, ou alguém ligado a ele, já está do outro lado de um processo do escritório. "Ligado" pode ser sócio de uma empresa ré, cônjuge, empresa do mesmo grupo. São caminhos de 2 ou 3 saltos entre pessoas, empresas e processos.

O Postgres continua sendo a fonte da verdade dos dados do escritório (ADR 0003). O grafo é uma visão derivada para essas duas consultas.

## Decisão

- **Neo4j Community** no perfil `grafo` do Docker Compose, a partir da F5.
- **Nós:** Statute, Article, Sumula, Decision, Court, Topic, Client, Person, Company, Case e Lawyer.
- **Arestas:** PART_OF, CITES, APPLIES, INTERPRETS, ABOUT, PARTY_IN {side}, RESPONSIBLE_FOR e RELATED_TO.
- **Conflito** (`check_conflict_graph`): uma consulta Cypher procura caminho de até 3 saltos entre a parte nova e um cliente do escritório que esteja do lado oposto (`PARTY_IN {side}`).
- **GraphRAG** (`expand_citation_graph`): depois da busca híbrida, o grafo traz as súmulas e as decisões ligadas aos artigos recuperados, com o id de fonte de cada uma.
- **Privacidade:** pessoas e empresas entram no grafo só por hash com sal do documento. O nome fica no Postgres, sob RLS.
- **Carga:** a partir do Postgres, por job no worker. O grafo nunca recebe escrita direta da aplicação.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Apache AGE (grafo dentro do Postgres) | Uma base só; openCypher | Extensão menos madura; ferramentas e comunidade menores; empacotamento no Docker do pgvector exige imagem própria | Risco operacional maior que o ganho de não ter um serviço a mais |
| SQL recursivo (`WITH RECURSIVE`) | Nenhum serviço novo; RLS vale direto | Caminhos variáveis de 3 saltos com tipos de aresta ficam ilegíveis e lentos de evoluir | Serve de linha de base no teste de conflito, não como solução |
| NetworkX em memória | Simples; ótimo para protótipo | Não persiste, não escala, recarrega a cada processo | Útil só em teste unitário |

## Consequências

**Positivas**

- A consulta de conflito fica curta e explicável. O caminho encontrado vira a justificativa mostrada à sócia.
- O grafo de citações dá à pesquisa um contexto que a busca vetorial não traz.

**Negativas e riscos**

- Mais um serviço: cerca de 1 GB de RAM, com heap limitado no perfil `grafo`. Ele só sobe quando a fase precisa.
- Duas cópias de parte dos dados. Mitigação: carga idempotente a partir do Postgres e teste de reconciliação.
- O Neo4j Community não tem controle de acesso fino. Por isso o grafo guarda hash, e não nome, e só o worker e o kit acessam o grafo.

## Como validar

- Testes de integração com casos de conflito plantados: conflito direto, por sócio de empresa e por cônjuge (positivos), e homônimo sem ligação (negativo).
- Ganho de recall nas perguntas sobre súmula, com e sem expansão pelo grafo, medido na F5 (ver [Avaliação](../AVALIACAO.md)).
- Gatilho para revisitar: o grafo não melhorar o recall de forma mensurável e o conflito couber em SQL recursivo com desempenho aceitável. Aí o Neo4j sai.

## Referências

- Neo4j e Cypher: <https://neo4j.com/docs/>
- Apache AGE: <https://age.apache.org/>

---

[Índice de ADRs](README.md)
