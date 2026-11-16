---
name: contract-review-services
description: Revisão de contrato de consumo de serviços (academia, curso, internet, plano de saúde) pelo playbook do escritório, com matriz de risco no CDC, redações sugeridas e aprovação do advogado antes de enviar.
---

# Revisão de contrato de serviços

O escritório revisa contratos de adesão de serviços ao consumidor com um playbook: regras que o código confere e critérios que pedem julgamento, cada um ligado a um dispositivo do CDC ou a uma súmula do STJ do corpus. O resultado é rascunho para o advogado responsável. Nada vai ao cliente sem a aprovação dele.

## Quando usar

- Contrato de serviço ao consumidor pessoa física: academia, curso, internet e TV, streaming, plano de saúde ou odontológico, serviço residencial, estacionamento.
- O pedido quer saber que cláusulas são abusivas, quanto pesam e como reescrevê-las.
- Fora disso (contrato entre empresas, locação, trabalho), o playbook não se aplica: diga que a revisão precisa do advogado.

## Como o agente de revisão trabalha

1. Lê o arquivo: Markdown, texto ou PDF com camada de texto, até 2 MiB. PDF digitalizado não tem texto e é recusado.
2. Divide o texto em cláusulas pelos títulos ("Cláusula 5ª – Do foro", "CLÁUSULA QUINTA", "## 5. Do foro", "5. DO FORO"). O preâmbulo com as partes e o fecho com local, data e assinaturas ficam fora. Sem nenhum título, cada parágrafo vira uma cláusula.
3. Em cada cláusula, em paralelo: primeiro as regras, por código e sem modelo; depois os critérios, pelo modelo rápido, que vê só aquela cláusula e com os dados pessoais trocados por marcadores ([PESSOA_1], [CPF_1]).
4. Monta a matriz de risco: cláusula, problema, gravidade, fundamento e origem (regra ou modelo).
5. Confere os fundamentos: todo id citado precisa existir no corpus. Frase que cita fonte fora dele, ou artigo sem o id, sai; achado que fica sem fundamento sai também, e o relatório lista o que saiu.
6. Escreve as redlines: troca cada frase apontada pela redação do escritório (regra) ou pela sugestão do modelo (critério), mantendo o número do item ("5.2"). Quando o trecho apontado não está na cláusula, a redação vai no fim dela.
7. Para e espera o advogado: aprovar publica o rascunho; editar publica o texto do advogado; rejeitar não publica nada e encerra a revisão. Editar sem mandar o texto não decide, e o pedido de aprovação volta.

A revisão pelo agente ainda não é uma ferramenta deste servidor MCP. Aqui, use o playbook abaixo e confira cada dispositivo com `get_statute_article`.

## Regras do playbook `consumer-services` v1

| Regra | O que acha | Fundamento |
|---|---|---|
| `limitacao-de-responsabilidade` | Exclui ou limita a responsabilidade do fornecedor | `cdc:art51:I`, `cdc:art25` |
| `transferencia-de-responsabilidade` | Põe em terceiros a responsabilidade do fornecedor | `cdc:art51:III` |
| `sem-reembolso` | Tira o reembolso do que já foi pago | `cdc:art51:II`, `cdc:art20:II`, `cdc:art49:pu` |
| `arrependimento` | Restringe a desistência em 7 dias da contratação fora do estabelecimento | `cdc:art49`, `cdc:art49:pu` |
| `onus-da-prova` | Inverte o ônus da prova contra o consumidor | `cdc:art51:VI`, `cdc:art6:VIII` |
| `arbitragem-compulsoria` | Impõe a arbitragem | `cdc:art51:VII` |
| `preco-unilateral` | O fornecedor muda o preço sozinho | `cdc:art51:X` |
| `cancelamento-unilateral` | Só o fornecedor cancela quando quer | `cdc:art51:XI` |
| `custos-de-cobranca` | Custos da cobrança só para o consumidor | `cdc:art51:XII` |
| `alteracao-unilateral` | O fornecedor muda o serviço sozinho | `cdc:art51:XIII` |
| `acesso-ao-judiciario` | Condiciona ou limita o acesso à Justiça | `cdc:art51:XVII`, `cdc:art6:VII` |
| `carencia-por-atraso` | Carência ou bloqueio depois de pago o atraso | `cdc:art51:XVIII` |
| `multa-de-mora` | Multa de mora acima de 2% | `cdc:art52:p1`, `cdc:art52` |
| `foro-de-eleicao` | Foro longe do domicílio do consumidor | `cdc:art101:I`, `cdc:art51:XV` |
| `internacao-limitada` | Limita no tempo a internação em plano de saúde | `stj:sumula:302`, `cdc:art51:IV`, `cdc:art51:p1:II` |
| `carencia-de-urgencia` | Carência acima de 24 horas para urgência ou emergência | `stj:sumula:597`, `cdc:art51:IV` |

Critérios que pedem julgamento:

| Critério | O que acha | Fundamento |
|---|---|---|
| `desvantagem-exagerada` | Obrigação desproporcional, como multa de cancelamento sobre tudo o que falta | `cdc:art51:IV`, `cdc:art51:p1:III` |
| `redacao-obscura` | Redação que dificulta entender a cláusula, como remissão a documento não entregue | `cdc:art46`, `cdc:art54:p3` |
| `venda-casada` | Condiciona o serviço à compra de outro produto ou serviço | `cdc:art39:I` |
| `prazo-a-criterio` | Prazo da obrigação do fornecedor a critério dele | `cdc:art39:XII` |

## Como revisar com o playbook

1. Separe as cláusulas pelos títulos e leia uma por vez.
2. Para cada cláusula, procure nas tabelas o que ela faz. Marque só o que o texto mostra; na dúvida, não marque.
3. Leia o texto oficial de cada fundamento com `get_statute_article`, passando o id em `source_id`. Cite o dispositivo pelo id, entre colchetes, e só depois de ler o `provision_text`: [cdc:art51:IV].
4. Diga a gravidade como o escritório a dá: média para `multa-de-mora`, `foro-de-eleicao`, `custos-de-cobranca`, `redacao-obscura` e `prazo-a-criterio`; alta para as demais.
5. Sugira a redação nova como proposta do escritório, não como lei.

## Como ler o relatório do agente

- A gravidade (alta, média, baixa) vem do playbook, não do modelo.
- Origem "regra": achado por código, repetível. Origem "modelo": julgamento, com o motivo que o modelo deu.
- A seção "Atenção" diz o que não foi feito: só as regras rodaram, o modelo não avaliou alguma cláusula ou o verificador tirou algo.
- "Fundamentos" traz o texto literal de cada dispositivo citado, tirado do corpus.

## Regras para quem usa o resultado

- Não acrescente fundamento que o playbook ou o relatório não trouxe, e cite só ids do corpus.
- A redação sugerida é a posição do escritório, não lei. Adapte ao caso antes de propor ao fornecedor.
- O limite de 2% da multa de mora está no artigo que trata do fornecimento com outorga de crédito ou financiamento [cdc:art52:p1]. Confira se o pagamento em prestações do contrato se enquadra.
- Com consumidor pessoa jurídica, a indenização pode ser limitada em situações justificáveis [cdc:art51:I]. As regras não sabem quem é o consumidor: confira.
- O CDC se aplica aos planos de saúde, salvo os de autogestão [stj:sumula:608].
- Destaque das cláusulas que limitam direito e tamanho da letra não são conferidos: o texto extraído não guarda a formatação.
- As regras leem uma cláusula por vez. Uma reciprocidade escrita noutra cláusula (o consumidor também pode cancelar) não é vista: confira `cancelamento-unilateral` contra o contrato inteiro.
