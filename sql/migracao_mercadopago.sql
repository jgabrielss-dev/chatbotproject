-- Migração: checkout PIX (Mercado Pago) nas tabelas existentes.
-- Idempotente: pode rodar mais de uma vez com segurança.
-- O `schema.sql` já cria estas colunas em base nova; aqui só o ALTER chega
-- às bases que existem antes do deploy.
ALTER TABLE pagamentos ADD COLUMN IF NOT EXISTS qr_code TEXT NOT NULL DEFAULT '';
ALTER TABLE pagamentos ADD COLUMN IF NOT EXISTS expira_em TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_pagamentos_referencia ON pagamentos (referencia);