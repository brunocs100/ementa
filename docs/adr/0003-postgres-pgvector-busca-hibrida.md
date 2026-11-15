# ADR 0003: Postgres com pgvector e busca híbrida

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Segurança e governança](../SEGURANCA-E-GOVERNANCA.md)

## Contexto

A pesquisa jurídica precisa achar o artigo certo por dois caminhos:

- **Sentido:** "me arrependi da compra" tem de achar o art. 49 do CDC.
- **Termo exato:** "repetição do indébito", "parágrafo único", "Súmula 385".

Busca só vetorial erra termo técnico; busca só textual erra paráfrase.

Há também a questão da permissão. O corpus público (leis, súmulas) é aberto. Os documentos internos e os documentos de processo só podem aparecer para quem trabalha no caso: são as barreiras éticas do escritório. Se esse filtro ficar só na aplicação, um bug na consulta vaza documento de outro cliente.

O volume é pequeno: alguns milhares de chunks no Marco 1, dezenas de milhares no Marco 3. O resto dos dados do escritório (processos, prazos, auditoria) já vai para o Postgres.

## Decisão

- **Postgres 17 com pgvector** como base de conhecimento, no schema `knowledge`.
- Índice **HNSW** na coluna `embedding`.
- **Busca textual (FTS) em português sem acento**: a configuração `portuguese`, aplicada numa coluna `tsv` gerada a partir de uma coluna de texto já sem acento, com índice GIN. Atualização da F1: o acento sai no Python (`ementa_kit.rag.fold`), no documento e na consulta, em vez do dicionário `unaccent`. Assim a busca roda em qualquer Postgres com pgvector, com ou sem a extensão.
- **Busca híbrida em SQL**: os 30 melhores de cada lado (vetorial e textual), fundidos por RRF (Reciprocal Rank Fusion, a soma de 1/(k + posição), com k = 60).
- **RLS** (Row Level Security) em `knowledge.chunks`. A política libera o trecho público (`case_id IS NULL`) e o trecho de processo só para quem está em `case_members`, a partir de `current_setting('app.user_id')`. O sigilo não entra na policy de leitura: é tratado pelo roteamento de modelo (ADR 0005). Assim o banco devolve só o que o usuário pode ver, qualquer que seja a consulta.
- O kit acessa a base pela porta `VectorStore`; o adaptador pgvector é a implementação padrão.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Qdrant | Feito para vetor; filtros rápidos; escala horizontal | Mais um serviço; a permissão vira filtro na aplicação; dados do caso em dois lugares | O volume não pede; perderia o RLS no banco |
| Weaviate | Busca híbrida pronta; módulos | Mais um serviço com modelo de dados próprio; mais RAM | Mesmo motivo; mais peso para a máquina de dev de 15 GB |
| OpenSearch | BM25 maduro, analisadores de português, vetor | Pesado para rodar local; outra stack para operar | Excesso para o volume e para a máquina de dev |

## Consequências

**Positivas**

- Uma base só: transação única entre o processo e os chunks dele, um backup, uma conexão.
- A permissão do RAG é aplicada pelo banco. O teste de RLS prova isso sem depender do código da aplicação.
- O SQL da busca híbrida é curto e legível.

**Negativas e riscos**

- O HNSW tem parâmetros (`m`, `ef_construction`, `ef_search`) que precisam de ajuste medido.
- O índice vetorial e o RLS juntos podem piorar o plano de execução. Mitigação: medir com `EXPLAIN ANALYZE` na F2.
- A busca textual depende de a consulta passar pelo mesmo `fold` do documento. Se não passar, palavra com acento não casa.

## Como validar

- Recall@5 ≥ 0,85 no conjunto dourado de perguntas, medido pela avaliação (ver [Avaliação](../AVALIACAO.md)).
- Teste de integração de RLS: o advogado A não recupera nenhum chunk do processo B, nem pela busca vetorial, nem pela textual.
- Gatilho para revisitar (plano de saída): passar de alguns milhões de chunks ou o p95 da busca passar de 300 ms. Aí avaliar o Qdrant atrás da mesma porta `VectorStore`, com a permissão replicada como filtro de payload.

## Referências

- pgvector: <https://github.com/pgvector/pgvector>
- Busca textual e dicionário `unaccent` no Postgres: <https://www.postgresql.org/docs/current/textsearch.html> e <https://www.postgresql.org/docs/current/unaccent.html>
- Row Level Security: <https://www.postgresql.org/docs/current/ddl-rowsecurity.html>

---

[Índice de ADRs](README.md)
