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
  -- 'whatsapp' e 'instagram' são integrações NÃO OFICIAIS (Evolution/Baileys e
  -- instagrapi). As oficiais da Meta ficam em 'whatsapp_oficial' e
  -- 'instagram_oficial'. Ver supabase/migrations/0003_canais_meta_oficial.sql.
  tipo TEXT NOT NULL CHECK (tipo IN ('telegram', 'whatsapp', 'instagram', 'webhook', 'whatsapp_oficial', 'instagram_oficial')),
  nome TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canais_agente ON canais (agente_id);
CREATE INDEX IF NOT EXISTS idx_canais_tipo ON canais (tipo);
-- Roteamento dos webhooks oficiais da Meta (o canal é resolvido pelo
-- phone_number_id / ig_user_id do evento, sem segredo na URL).
CREATE INDEX IF NOT EXISTS idx_canais_tipo_identificador
  ON canais (tipo, (config ->> 'phone_number_id'));
CREATE INDEX IF NOT EXISTS idx_canais_tipo_ig_user
  ON canais (tipo, (config ->> 'ig_user_id'));

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
  -- Id único do evento (tg:update_id, wa:key.id, ig:thread:msg, web:uuid).
  -- NUNCA vazio: ver uq_caixa_origem.
  origem TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pendente'
    CHECK (status IN ('pendente', 'processando', 'respondido', 'sem_resposta', 'erro')),
  tentativas INTEGER NOT NULL DEFAULT 0,
  proxima_tentativa TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultimo_erro TEXT,
  resposta TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Quando a resposta foi entregue.
  processado_em TIMESTAMPTZ,
  -- Quando a mensagem entrou em 'processando'. Sem isto, reenfileirar o que
  -- ficou preso usaria criado_em e devolveria à fila um item que estava sendo
  -- processado AGORA, respondendo a mesma mensagem duas vezes.
  processando_em TIMESTAMPTZ
);

-- Índice NÃO parcial: o PostgREST (upsert da edge function, onConflict
-- "canal_id,origem") não consegue provar o predicado de um índice parcial e a
-- inserção falharia. Consequência: 'origem' precisa ser única e não-vazia em
-- todas as mensagens, o que o app garante gerando id por evento.
CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem);
CREATE INDEX IF NOT EXISTS idx_caixa_fila ON caixa_entrada (status, proxima_tentativa);
CREATE INDEX IF NOT EXISTS idx_caixa_canal ON caixa_entrada (canal_id, criado_em);

-- Sessões do Instagram guardadas no Postgres (grátis, junto do resto).
-- O disco do Render é efêmero: sem isto, todo cold start relogaria a conta e
-- a conta cairia no limite de logins/bloqueio do Instagram.
-- A chave é 'sessionid:<sha1[:8]>' (nunca o sessionid cru, que é segredo).
CREATE TABLE IF NOT EXISTS instagram_sessoes (
  username TEXT PRIMARY KEY,
  dados JSONB NOT NULL DEFAULT '{}'::jsonb,
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS ligado e sem policy: o app acessa com service_role (que ignora RLS) e o
-- anon/authenticated do PostgREST não enxerga nada. Sem isto, qualquer pessoa
-- com a public anon key leria os cookies de sessão do Instagram pela API.
ALTER TABLE instagram_sessoes ENABLE ROW LEVEL SECURITY;

-- ==========================================================================
-- Contas e multi-tenant
--
-- Este bloco e o mesmo da migration 0004_contas_e_multitenant.sql, mantido
-- aqui porque scripts/init_db.py aplica ESTE arquivo (nao as migrations) numa
-- base nova. Os dois precisam ficar em sincronia: mudar a migration sem
-- mudar isto faz o banco de um dev ficar diferente do de producao.
-- ==========================================================================

-- Configuracao interna. Sem policy de RLS: so o service_role (nosso backend)
-- le e escreve. O papel de admin vem daqui, nao de um campo no navegador.
CREATE TABLE IF NOT EXISTS app_config (
  chave TEXT PRIMARY KEY,
  valor TEXT NOT NULL DEFAULT '',
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;

-- Um perfil por conta do Supabase Auth. `id` e o mesmo uuid de auth.users.
CREATE TABLE IF NOT EXISTS perfis (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  role TEXT NOT NULL DEFAULT 'usuario' CHECK (role IN ('admin', 'usuario')),
  bloqueado BOOLEAN NOT NULL DEFAULT false,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perfis_role ON perfis (role);
ALTER TABLE perfis ENABLE ROW LEVEL SECURITY;

-- Cada cliente so le o proprio perfil. Quem adminstra papel usa a API, que
-- conecta com service_role e ignora RLS de proposito.
CREATE POLICY perfis_own_select ON perfis
  FOR SELECT USING (auth.uid() = id);

-- Trigger de cadastro: toda conta nova ganha um perfil na hora, sem depender
-- de ninguem lembrar de criar. O papel vem de app_config.admin_emails, que e
-- uma lista de e-mails separada por virgula.
CREATE OR REPLACE FUNCTION public.criar_perfil() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_email TEXT := lower(coalesce(NEW.email, ''));
  v_admins TEXT := '';
  v_role TEXT := 'usuario';
BEGIN
  SELECT valor INTO v_admins FROM app_config WHERE chave = 'admin_emails';
  IF v_admins IS NOT NULL AND v_admins <> '' THEN
    IF lower(v_admins) LIKE '%' || v_email || '%' THEN
      v_role := 'admin';
    END IF;
  END IF;

  INSERT INTO perfis (id, email, role) VALUES (NEW.id, v_email, v_role)
  ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.criar_perfil();

-- Contas que ja existiam antes desta migration entram como 'usuario'. O
-- primeiro admin precisa ser promovido na mao (ver comentarios da migration).
INSERT INTO perfis (id, email, role)
SELECT id, lower(email), 'usuario' FROM auth.users
ON CONFLICT (id) DO NOTHING;

-- Dono do agente. NULL = agente legado, sem dono; o admin logando reivindica
-- (app/main.py -> /api/eu). ON DELETE CASCADE: deletar a conta leva o agente,
-- e nao deixa um bot orfao respondendo no WhatsApp de um ex-cliente.
ALTER TABLE agentes ADD COLUMN IF NOT EXISTS dono_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_agentes_dono ON agentes (dono_id);

-- RLS por tenant. O app hoje fala com o banco pela service_role e passa o
-- `dono_id` na propria query, entao estas policies sao a segunda camada: se
-- alguem plugar o PostgREST com a anon key, `authenticated` so enxerga a si
-- mesmo. Vale mais como rede de seguranca do que como mecanismo principal.
ALTER TABLE agentes ENABLE ROW LEVEL SECURITY;
CREATE POLICY agentes_own ON agentes FOR ALL
  USING (dono_id = auth.uid())
  WITH CHECK (dono_id = auth.uid());

ALTER TABLE canais ENABLE ROW LEVEL SECURITY;
CREATE POLICY canais_own ON canais FOR ALL
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()));

ALTER TABLE sessoes ENABLE ROW LEVEL SECURITY;
CREATE POLICY sessoes_own ON sessoes FOR ALL
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()));

ALTER TABLE mensagens ENABLE ROW LEVEL SECURITY;
CREATE POLICY mensagens_own ON mensagens FOR ALL
  USING (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()));

ALTER TABLE caixa_entrada ENABLE ROW LEVEL SECURITY;
CREATE POLICY caixa_own ON caixa_entrada FOR ALL
  USING (EXISTS (
    SELECT 1 FROM canais c JOIN agentes a ON a.id = c.agente_id
    WHERE c.id = caixa_entrada.canal_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (
    SELECT 1 FROM canais c JOIN agentes a ON a.id = c.agente_id
    WHERE c.id = caixa_entrada.canal_id AND a.dono_id = auth.uid()));

-- Grants do schema public. Um `CREATE SCHEMA public` (recuperacao de banco,
-- restore de backup) recria o schema SEM os grants que o Supabase instala, e a
-- edge function passa a falhar com "permission denied for schema public".
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC GRANT ALL ON SEQUENCES TO service_role;

-- O SELECT para anon era o buraco: qualquer pessoa com a public anon key lia
-- canais.config (token do Telegram, sessionid do Instagram, access_token e
-- app_secret da Meta) direto pelo PostgREST, sem passar pela nossa API. Agora
-- `anon` nao tem nada, e `authenticated` fica so com leitura — a escrita e a
-- checagem de posse continuam sendo do backend.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC REVOKE ALL ON SEQUENCES FROM anon;

-- Leitura para quem tem sessao. As policies acima filtram pelo dono, entao
-- isto nao abre nada: apenas permite que o RLS possa ser avaliado.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC GRANT SELECT ON TABLES TO authenticated;
