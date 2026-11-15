---
name: deadline-calculation
description: Cálculo de prazo processual em dias úteis com a ferramenta calculate_deadline, que aplica o CPC (arts. 219, 220 e 224), os feriados e a suspensão de 20/12 a 20/01. Use sempre que o pedido envolver vencimento, último dia ou contagem de um prazo; o modelo nunca conta dias sozinho.
---

# Cálculo de prazo

Prazo é calculado por código testado, nunca pelo modelo. Não conte dias, não some datas e não refaça a conta para conferir: chame `calculate_deadline` e use o que ela devolver.

## Quando usar

- O pedido quer o vencimento ou o último dia de um prazo processual em dias úteis.
- A ferramenta conta só dias úteis, pelas regras do CPC. Se o prazo da lei não é em dias úteis, ela não se aplica: diga o prazo como a lei diz e deixe a data para o advogado.

## Como chamar

| Campo | O que pôr |
|---|---|
| `start_date` | O dia do começo do prazo, em AAAA-MM-DD. Ele depende da forma de intimação (CPC, art. 231). A ferramenta exclui esse dia e começa a contar no dia útil seguinte (CPC, art. 224). |
| `business_days` | O prazo em dias úteis, de 1 a 365. A ferramenta não aplica prazo em dobro nem outra regra especial: o número de dias é o que o usuário ou a lei indicar. |
| `state` | A UF do tribunal. Hoje só `RS`. |
| `extra_closed_days` | Feriados municipais e dias sem expediente do tribunal que o usuário informar, até 60 datas. |

Se faltar o dia do começo ou o número de dias, pergunte. Não suponha.

## Premissas da ferramenta

- Só dias úteis contam (CPC, art. 219).
- O prazo fica suspenso de 20/12 a 20/01, inclusive (CPC, art. 220).
- Exclui o dia do começo e inclui o dia do vencimento (CPC, art. 224).
- Entram os feriados nacionais, a Sexta-feira Santa e o feriado estadual do RS (20 de setembro).
- Feriado municipal e dia sem expediente do tribunal só entram se vierem em `extra_closed_days`.

## Como responder

1. Dê o vencimento (`due_date`) exatamente como a ferramenta devolveu.
2. Liste as premissas (`premises`), principalmente a dos feriados municipais, para o advogado conferir o calendário do tribunal.
3. Se perguntarem por que a data caiu ali, mostre os dias pulados (`skipped_days`) com o motivo de cada um.
4. Se um dado mudar (outra data, outro número de dias, mais um feriado), chame a ferramenta de novo. Não ajuste a data à mão.
5. Termine com o aviso de rascunho (`notice`).
