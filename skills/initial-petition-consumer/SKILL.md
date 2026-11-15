---
name: initial-petition-consumer
description: Rascunho de petição inicial de consumo (negativação indevida ou vício do produto) pelo agente de redação de peças, com teses pesquisadas, citação verificada e aprovação do advogado antes do DOCX. Use quando pedirem uma inicial de consumo.
---

# Petição inicial de consumo

O agente de redação de peças da Ementa rascunha a petição e o advogado responsável decide. Nada fica final sem a aprovação dele. O pedido entra pelo console do escritório; pelo MCP há só a pesquisa e o prazo (skills `legal-research` e `deadline-calculation`).

## Modelos do escritório

| Modelo | Quando usar |
|---|---|
| `initial-petition-credit-listing` | Nome inscrito em cadastro de inadimplentes sem dívida válida: inexistência do débito, dano moral e tutela de urgência |
| `initial-petition-product-defect` | Produto durável com vício que o fornecedor não consertou no prazo: substituição, restituição ou abatimento do preço, e dano moral quando os fatos o sustentam |

Os modelos e os pareceres aprovados pelo escritório ficam em `data/firm/`. Para outra peça não há modelo: diga isso, não improvise.

## O que a descrição do caso precisa trazer

- o cliente, com a qualificação (nacionalidade, estado civil, profissão, CPF e endereço), e contra quem vai a ação (até três réus);
- os fatos em ordem, com as datas e os valores;
- os documentos e as provas que o cliente tem;
- o que o cliente quer;
- o juízo e a cidade do foro.

Os dados pessoais podem vir como foram ditos. O agente os troca por marcadores antes de qualquer modelo de nuvem e põe os valores de volta no rascunho. O que faltar na descrição aparece no resumo da aprovação.

## Como o agente trabalha

1. Separa os fatos da descrição.
2. Planeja de uma a quatro teses, cada uma com uma pergunta de pesquisa, seguindo os pareceres do escritório.
3. Pesquisa as teses em paralelo, com o mesmo agente de pesquisa da skill `legal-research`. Tese sem fonte verificada fica fora da peça e é listada no resumo.
4. Escreve os fatos, os fundamentos e os pedidos e os põe no modelo, com as partes, o juízo, os pedidos fixos do modelo e a data.
5. Um crítico avalia completude, citações, pedidos e tom. Se reprovar, o rascunho é reescrito uma vez; o que ele ainda apontar vai para o resumo.
6. O verificador confere cada citação contra as fontes que a pesquisa verificou. Se uma falhar, o rascunho é reescrito uma vez; se falhar de novo, a frase sai e o resumo avisa.
7. Para antes de finalizar e espera a decisão do advogado, com o rascunho e o resumo do que conferir.

## Decisão do advogado

| Decisão | Resultado |
|---|---|
| Aprovar (`approve`) | Finaliza o rascunho exatamente como o advogado viu |
| Editar (`edit`) | Finaliza o texto do advogado, que vem com a decisão e é obrigatório |
| Rejeitar (`reject`) | Encerra sem peça |

Quem decidiu e quando ficam registrados com a peça final. Uma decisão incompleta, como editar sem mandar o texto, não encerra nada: a execução continua esperando.

## Regras

- Cite só o que a pesquisa verificou. Não acrescente artigo, súmula ou decisão de memória nem a partir de um parecer.
- No rascunho, a citação aparece por extenso, como "(art. 14 do CDC)"; os ids de fonte ficam na lista de citações do resultado.
- Não calcule valor nem data. O valor da causa, o valor da indenização que o caso não traz e a assinatura ficam entre colchetes para o advogado completar.
- Peças aprovadas antes (até três) mostram ao agente as edições dos advogados. Nomes, fatos e valores delas nunca entram na peça nova.

## DOCX

O arquivo sai com "Rascunho para conferência do advogado responsável" no cabeçalho de cada página. Depois da aprovação, o cabeçalho passa a dizer quem aprovou e quando, no horário de Brasília.
