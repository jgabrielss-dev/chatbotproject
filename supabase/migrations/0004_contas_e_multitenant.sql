-- 0004 - contas de usuario (Supabase Auth) e separacao multi-tenant.
--
-- O tenant e o proprio usuario: `agentes.dono_id` aponta para auth.users(id).
-- Nenhum agente existe sem dono, entao um usuario so enxerga (e so gerencia) os
-- agentes cadastrados por ele. O admin ve tudo.
--
-- ALEM disso este arquivo fecha um buraco de exposicao que existia desde a
-- 0001/0003: `GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon` e as
-- tabelas de negocio NAO tinham RLS. Como a anon key do Supabase e publica por
-- natureza (ela vai no navegador), qualquer pessoa podia ler via PostgREST o
-- token do bot do Telegram, o sessionid do Instagram e o access_token da Meta
-- guardados em canais.config, alem de todas as mensagens e da memoria dos
-- clientes. A redacao em app/repositories.py so protege a API do FastAPI: o
-- PostgREST lia a tabela crua e nao passava por ela. Aqui as policies passam a
-- valer e o `anon` perde o SELECT.

-- ---------------------------------------------------------------------------
-- Configuracao interna, escrita so por SQL (o operador)
-- ---------------------------------------------------------------------------
-- Guarda a lista de e-mails que nascem como admin. Fica no banco de proposito:
-- o trigger abaixo le daqui, e nao de variavel de ambiente, porque o app le
-- esta tabela a cada login. Se a lista vivesse no .env, bastaria trocar o
-- e-mail da conta no Supabase para cair na lista e virar admin. O trigger roda
-- apenas em INSERT de auth.users, entao mudar o e-mail depois nao promove nada.
CREATE TABLE IF NOT EXISTS app_config (
  chave TEXT PRIMARY KEY,
  valor TEXT NOT NULL DEFAULT '',
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sem policy nenhuma: so o dono da tabela (o papel postgres, com que o app
-- conecta via DATABASE_URL) e a service_role leem. O anon/authenticated do
-- PostgREST nao veem a lista de administradores.
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE app_config IS
  'Configuracao interna escrita por SQL. Sem policy de RLS: leitura restrita ao service_role.';

-- ---------------------------------------------------------------------------
-- Perfis: um por usuario do Supabase Auth
-- ---------------------------------------------------------------------------
-- `role` e 'admin' ou 'usuario'. Nao existe policy de INSERT/UPDATE para
-- `authenticated`, entao o usuario nao consegue se promover sozinho: o unico
-- caminho e o trigger (na criacao da conta) ou um UPDATE manual do operador.
CREATE TABLE IF NOT EXISTS perfis (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'usuario' CHECK (role IN ('admin', 'usuario')),
  -- Permite desligar o acesso sem apagar a conta e todo o historico do tenant.
  bloqueado BOOLEAN NOT NULL DEFAULT FALSE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_perfis_role ON perfis (role);
CREATE INDEX IF NOT EXISTS idx_perfis_email ON perfis (lower(email));

ALTER TABLE perfis ENABLE ROW LEVEL SECURITY;

CREATE POLICY perfis_le_proprio ON perfis
  FOR SELECT TO authenticated USING (id = auth.uid());

-- ---------------------------------------------------------------------------
-- Trigger: toda conta nova do Auth ganha um perfil
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.criar_perfil() RETURNS TRIGGER AS $$
DECLARE
  permitidos TEXT;
  email_novo TEXT;
BEGIN
  email_novo := lower(coalesce(NEW.email, ''));
  permitidos := lower(replace(coalesce(
    (SELECT valor FROM public.app_config WHERE chave = 'admin_emails'), ''), ' ', ''));

  INSERT INTO public.perfis (id, email, role)
  VALUES (
    NEW.id,
    email_novo,
    CASE
      WHEN email_novo <> ''
       AND position(',' || email_novo || ',' in ',' || permitidos || ',') > 0
      THEN 'admin'
      ELSE 'usuario'
    END
  )
  ON CONFLICT (id) DO UPDATE
    SET email = EXCLUDED.email;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.criar_perfil();

-- Uma conta criada antes deste arquivo (com o trigger ja instalado depois) fica
-- sem perfil. O app garante o perfil no primeiro login, e o INSERT abaixo faz
-- o backfill de quem ficou de fora.
INSERT INTO public.perfis (id, email, role)
SELECT u.id, lower(coalesce(u.email, '')), 'usuario'
FROM auth.users u
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Dono do agente
-- ---------------------------------------------------------------------------
-- NULL = agente legado, criado antes do multi-tenant. O primeiro admin que
-- logar reivindica todos eles (repositories.reivindicar_agentes_sem_dono), o
-- que evita perder os agentes que ja estavam em uso.
ALTER TABLE agentes ADD COLUMN IF NOT EXISTS dono_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_agentes_dono ON agentes (dono_id);

COMMENT ON COLUMN agentes.dono_id IS
  'auth.users.id do dono. NULL apenas em agentes legados, reivindicados pelo primeiro admin.';

-- ---------------------------------------------------------------------------
-- RLS das tabelas de negocio
-- ---------------------------------------------------------------------------
-- O app e a edge function acessam com service_role/papel postgres, que ignoram
-- RLS. Estas policies valem para quem vai pelo PostgREST com o JWT do
-- usuario: cada um so enxerga a propria arvore (agente -> canais -> sessoes ->
-- mensagens -> fila).
ALTER TABLE agentes ENABLE ROW LEVEL SECURITY;
ALTER TABLE canais ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessoes ENABLE ROW LEVEL SECURITY;
ALTER TABLE mensagens ENABLE ROW LEVEL SECURITY;
ALTER TABLE caixa_entrada ENABLE ROW LEVEL SECURITY;

CREATE POLICY agentes_do_dono ON agentes
  FOR ALL TO authenticated USING (dono_id = auth.uid()) WITH CHECK (dono_id = auth.uid());

CREATE POLICY canais_do_dono ON canais
  FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()));

CREATE POLICY sessoes_do_dono ON sessoes
  FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()));

CREATE POLICY mensagens_do_dono ON mensagens
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()
  ));

CREATE POLICY caixa_do_dono ON caixa_entrada
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM canais c JOIN agentes a ON a.id = c.agente_id
    WHERE c.id = caixa_entrada.canal_id AND a.dono_id = auth.uid()
  ));

-- ---------------------------------------------------------------------------
-- Grants: o anon perde o SELECT
-- ---------------------------------------------------------------------------
-- A 0003 concedeu SELECT a anon e authenticated em TODAS as tabelas. Sem RLS
-- acima isso expunha os segredos dos canais; com RLS o authenticated pode
-- manter o SELECT (as policies filtram), mas o anon nao tem nenhuma policy e
-- portanto nao le nada.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- O padrao do schema public e o que vale para tabelas criadas depois.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO authenticated;

-- ---------------------------------------------------------------------------
-- Como virar admin
-- ---------------------------------------------------------------------------
-- INSERT INTO app_config (chave, valor) VALUES ('admin_emails', 'voce@exemplo.com');
-- INSERT ... ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now();
--
-- Vale para as contas criadas DEPOIS. Para promover uma conta que ja existe:
--   UPDATE perfis SET role = 'admin' WHERE email = 'voce@exemplo.com';
-- E para conferir o resultado:
--   SELECT email, role, bloqueado FROM perfis ORDER BY criado_em;
