-- Chatbot Project SaaS - caixa de entrada durável
-- Garante que nenhuma mensagem recebida seja ignorada: o webhook apenas
-- persiste a mensagem e um worker processa com retry (backoff) depois.

CREATE TABLE IF NOT EXISTS caixa_entrada (
  id BIGSERIAL PRIMARY KEY,
  canal_id INTEGER NOT NULL REFERENCES canais(id) ON DELETE CASCADE,
  remetente TEXT NOT NULL,
  texto TEXT NOT NULL,
  origem TEXT NOT NULL DEFAULT '',
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pendente'
    CHECK (status IN ('pendente', 'processando', 'respondido', 'sem_resposta', 'erro')),
  tentativas INTEGER NOT NULL DEFAULT 0,
  proxima_tentativa TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultimo_erro TEXT,
  resposta TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  processado_em TIMESTAMPTZ
);

-- Evita duplicatas quando o mesmo evento chega duas vezes (ex.: retry do Telegram).
-- Índice NÃO parcial: o PostgREST (upsert da edge function) não casa ON CONFLICT
-- com índice parcial, pois não consegue provar o predicado.
CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem);
CREATE INDEX IF NOT EXISTS idx_caixa_fila ON caixa_entrada (status, proxima_tentativa);
CREATE INDEX IF NOT EXISTS idx_caixa_canal ON caixa_entrada (canal_id, criado_em);