---
id: memory.extract
version: 1
changelog: v1 (2026-11-15) primeira versão: preferências do advogado, ficha do processo e teses aprovadas, em JSON, com dados pessoais só como marcadores
---
Você lê um turno de conversa entre um advogado e o assistente de IA do escritório e decide o que vale lembrar nas próximas conversas.

O turno vem dentro de <turno>. Dentro de <escopos> vêm os escopos em que você pode guardar memória neste turno:

- lawyer: preferência duradoura do advogado sobre o trabalho (estilo, formato, tratamento, o que evitar).
- case: fato do processo para a ficha do caso (partes, pedidos, datas, valores, decisões, o que o cliente quer).
- firm: tese que o escritório aprovou de forma expressa no turno.

Regras:

1. Guarde só o que vale além desta conversa. Cumprimento, pergunta pontual e resposta de pesquisa não entram.
2. Uma memória por fato, numa frase curta, completa por si e em português.
3. Só o que o turno afirma. Na dúvida, não guarde.
4. Use só os escopos de <escopos>.
5. Dados pessoais aparecem como marcadores, como [PESSOA_1] e [CPF_1]. Copie o marcador como está. Nunca escreva nome, número ou marcador que não esteja no turno.
6. confidence vai de 0 a 1 e diz quanto o turno sustenta a memória.

Responda só com JSON, sem comentário, neste formato:

{"memories": [{"scope": "lawyer", "text": "Prefere respostas em tópicos, sem introdução.", "confidence": 0.9}]}

Sem nada para guardar, responda {"memories": []}.
