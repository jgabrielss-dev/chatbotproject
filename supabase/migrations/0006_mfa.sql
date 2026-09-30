"""Migração 0006: coluna para o segundo fator estar ativo.

O `mfa_ativo` aqui NÃO é a fonte da verdade do 2FA -- quem manda nisso é o
Supabase Auth (os fatores ficam em `auth.mfa_factors`). Esta coluna serve para
a NOSSA API exigir o segundo fator: sem ela, um token de nível `aal1` continua
valendo depois que a pessoa ativa o 2FA, e o segundo fator viraria enfeite.

Quem escreve o valor é o próprio usuário, depois de apresentar um código que
só o GoTrue consegue validar (a rota exige token `aal2`), então o cliente não
tem como ligar o 2FA sem saber gerá-lo -- nem desligar: apagar o fator no
GoTrue também exige `aal2`.
"""
ALTER TABLE perfis ADD COLUMN IF NOT EXISTS mfa_ativo BOOLEAN NOT NULL DEFAULT false;

-- Índice não é necessário: a coluna é lida junto do perfil inteiro (o token já
-- traz o id do usuário) e escrita uma vez por usuário.
