---
name: legal-research
description: Pesquisa jurídica com citação verificada no corpus público da Ementa (CDC, LGPD, CPC, súmulas do STJ sobre consumo e súmulas vinculantes do STF). Use quando o pedido for uma pergunta de direito, o texto de um artigo de lei ou uma resposta que precise citar a fonte.
---

# Pesquisa jurídica com citação

A resposta é rascunho para o advogado responsável conferir, nunca aconselhamento ao cliente final. O corpus é o que `search_legal_corpus` alcança: CDC, LGPD, CPC, súmulas do STJ sobre consumo e súmulas vinculantes do STF. Fora disso, diga que não há fonte.

## Qual ferramenta usar

| Pedido | Ferramenta |
|---|---|
| Pergunta de direito, com resposta citada | `answer_legal_question` |
| Achar os artigos sobre um tema | `search_legal_corpus` |
| Texto oficial de um artigo ou dispositivo | `get_statute_article` |
| Vencimento de prazo em dias úteis | `calculate_deadline`, com a skill `deadline-calculation` |

## Como pesquisar

1. Se o pedido já cita o artigo ("art. 49 do CDC"), leia o texto oficial com `get_statute_article` e o id de fonte, como `cdc:art49`, `cpc:art219` ou `stj:sumula:385`.
2. Para uma pergunta, use `answer_legal_question`. Ela busca as fontes, escreve a resposta e passa pelo verificador de citações.
3. Leia a saída inteira. O que está em `removed` não foi verificado e saiu da resposta: não ponha de volta.
4. Se `grounded` vier falso, diga que as fontes não bastam e que a pergunta precisa de pesquisa do advogado. Não complete de memória.
5. Para mostrar outros artigos, busque com `search_legal_corpus` e confira o texto com `get_statute_article` antes de citar.

## Regras de citação

- Cite só ids de fonte que as ferramentas devolveram nesta conversa, entre colchetes: [cdc:art49].
- Use o id mais específico: parágrafo único `cdc:art49:pu`, parágrafo `cdc:art10:p1`, inciso `cdc:art6:III`, alínea `cdc:art6:II:a`.
- Toda frase que afirma uma regra termina com o id que a sustenta. Se escrever "art. 49" no texto, ponha o id na mesma frase.
- Nunca cite artigo, súmula, lei ou decisão de memória ou de fora do corpus. Se a pergunta depende disso, diga que precisa de pesquisa do advogado.
- Não conte prazo nem calcule data ou valor: diga o prazo como a lei diz e use `calculate_deadline` para a data.
- Mantenha o aviso de rascunho (`notice`) e o link oficial (`url`) de cada fonte citada.
