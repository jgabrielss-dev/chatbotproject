-- 0003 - fila sem perda, sessao do Instagram no banco e unificacao do indice.
--
-- 1) caixa_entrada.processando_em: sem ela, reenfileirar_processando usava
--    criado_em e devolvia a fila um item que estava sendo processado AGORA
--    (assim que ele passasse de 10min de idade), duplicando a resposta.
-- 2) uq_caixa_origem passa a ser NAO parcial: e o unico formato que o PostgREST
--    (upsert da edge function, onConflict "canal_id,origem") consegue inferir.
--    Com indice parcial, a edge function volta a falhar em toda mensagem.
--    Como o indice agora cobre origem vazia, origem passa a ser NOT NULL e cada
--    mensagem precisa de um id de evento unico (o app ja faz isso).
-- 3) instagram_sessoes: o disco do Render e efemero, entao a sessao era perdida
--    a cada cold start e o login repetido estourava o limite do Instagram.

ALTER TABLE caixa_entrada ADD COLUMN IF NOT EXISTS processando_em TIMESTAMPTZ;

-- Dados legados com origem vazia deixariam de caber no indice novo; sao
-- duvidosos (o app nunca gravou duplicatas), mas ficam rastreaveis em vez de
-- sumirem num Conflict silencioso.
UPDATE caixa_entrada SET origem = 'legado:id:' || id::text WHERE origem = '';

ALTER TABLE caixa_entrada ALTER COLUMN origem SET NOT NULL;

DROP INDEX IF EXISTS uq_caixa_origem;
CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem);

-- Reinicia a contagem das mensagens que estavam presas em 'processando' durante
-- o deploy: processando_em e NULL nelas, entao reenfileirar_processando as
-- devolve para 'erro' e elas voltam ao fim da fila para uma nova tentativa.
UPDATE caixa_entrada SET status = 'erro', ultimo_erro = COALESCE(ultimo_erro, 'redeploy'), proxima_tentativa = now()
WHERE status = 'processando';

CREATE TABLE IF NOT EXISTS instagram_sessoes (
  username TEXT PRIMARY KEY,
  dados JSONB NOT NULL DEFAULT '{}'::jsonb,
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS ligado e sem policy: so o service_role (usado pelo app) le e escreve.
-- Sem isto, os cookies de sessao do Instagram ficariam legiveis via PostgREST
-- com a anon key publica do projeto.
ALTER TABLE instagram_sessoes ENABLE ROW LEVEL SECURITY;
