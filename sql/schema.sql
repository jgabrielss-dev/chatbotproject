-- Chatbot Project SaaS - schema inicial (sem cobrança)

CREATE TABLE IF NOT EXISTS agentes (
  id SERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS canais (
  id SERIAL PRIMARY KEY,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  tipo TEXT NOT NULL CHECK (tipo IN ('telegram', 'whatsapp', 'instagram', 'webhook')),
  nome TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canais_agente ON canais (agente_id);
CREATE INDEX IF NOT EXISTS idx_canais_tipo ON canais (tipo);

-- Colunas abertas: "memoria" guarda o que o system prompt mandar a IA extrair do usuário
CREATE TABLE IF NOT EXISTS sessoes (
  id SERIAL PRIMARY KEY,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  canal_id INTEGER NOT NULL REFERENCES canais(id) ON DELETE CASCADE,
  usuario_externo TEXT NOT NULL,
  memoria JSONB NOT NULL DEFAULT '{}'::jsonb,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canal_id, usuario_externo)
);

CREATE INDEX IF NOT EXISTS idx_sessoes_agente ON sessoes (agente_id);

CREATE TABLE IF NOT EXISTS mensagens (
  id BIGSERIAL PRIMARY KEY,
  sessao_id INTEGER NOT NULL REFERENCES sessoes(id) ON DELETE CASCADE,
  de_ia BOOLEAN NOT NULL,
  texto TEXT NOT NULL,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mensagens_sessao ON mensagens (sessao_id, criado_em);

-- Caixa de entrada durável: nenhuma mensagem recebida é ignorada.
-- O webhook apenas persiste a mensagem e um worker processa com retry (backoff).
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem) WHERE origem <> '';
CREATE INDEX IF NOT EXISTS idx_caixa_fila ON caixa_entrada (status, proxima_tentativa);
CREATE INDEX IF NOT EXISTS idx_caixa_canal ON caixa_entrada (canal_id, criado_em);