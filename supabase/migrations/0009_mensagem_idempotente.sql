-- Retry da fila não pode duplicar mensagem nem queimar cota duas vezes.
--
-- O problema que esta coluna resolve: `processar_mensagem` gravava a mensagem do
-- usuário e chamava `registrar_consumo` ANTES de chamar o Gemini. Se o Gemini
-- (ou o envio da resposta) falhasse depois disso, o worker devolvia o item para
-- a fila com backoff e tentava de novo — e cada tentativa gravava uma linha nova
-- em `mensagens` e somava 1 na cota do mesmo texto. Três falhas seguidas davam
-- três cópias da mesma pergunta na tela e três mensalidades contadas para uma
-- mensagem só.
--
-- `chave` amarra a linha ao item da fila que a produziu (`caixa:<id>` para a
-- mensagem do usuário, `caixa:<id>:ia` para a resposta). O índice é único e
-- parcial de propósito:
--   * única — o segundo `INSERT` da mesma chave é o que o ON CONFLICT bloqueia;
--   * parcial (WHERE chave IS NOT NULL) — todo o chat do navegador, que não vem
--     da fila, continua com `chave` nula e vários NULL convivem sem colidir.

ALTER TABLE mensagens
  ADD COLUMN IF NOT EXISTS chave TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS mensagens_chave_uniq
  ON mensagens (chave)
  WHERE chave IS NOT NULL;

COMMENT ON COLUMN mensagens.chave IS
  'item da fila que gerou a linha (caixa:<id>[:ia]); impede retry de duplicar';