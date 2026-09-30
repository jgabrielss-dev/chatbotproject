"""Liga (ou confere) o MFA/TOTP do projeto no Supabase.

Por que um script: o item 5 pede "2FA pelo supabase auth", mas o GoTrue só
aceita `/auth/v1/factors` se o projeto tiver o TOTP habilitado — e isso fica na
configuração do projeto, não no código. Sem isto, o cadastro perguntaria "ativar
2FA?", a pessoa escanearia o QR e o servidor responderia 422 "MFA not enabled".

Duas coisas que este script ensina do jeito errado, se escritos a olho:

  * A API de configuração é de chaves PLANAS (`mfa_totp_enroll_enabled`), não do
    objeto aninhado `MFA: {...}` que a documentação do GoTrue sugere. Mandar o
    objeto aninhado responde 200 e não muda NADA — o "sucesso" silencioso que
    faz a gente acreditar que ligou.
  * `mfa_allow_low_aal: false` (o padrão) é o que impede uma sessão de nível 1
    de chamar os endpoints de MFA. Deixar ligado é o comportamento correto: é o
    que garante que o segundo fator só vale se foi realmente confirmado.

Idempotente: só escreve quando algo está diferente, para não derrubar sessão de
ninguém numa repetição.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

CREDENCIAIS = pathlib.Path(r"E:\chatbots project\credentials.txt")
PROMPT = pathlib.Path(r"E:\chatbots project\prompt.txt")
REF = "ophhmvnascjpayoxqdkz"
BASE = f"https://api.supabase.com/v1/projects/{REF}/config/auth"

# O que precisa estar ligado para o item 5 funcionar.
QUERIDO = {
    "mfa_totp_enroll_enabled": True,   # POST /factors (inscrever o autenticador)
    "mfa_totp_verify_enabled": True,   # POST /factors/:id/verify (conferir o código)
    "mfa_allow_low_aal": False,        # sessão de nível 1 NÃO valida MFA
    "mfa_max_enrolled_factors": 10,
}


def _cred(chave: str) -> str:
    """Lê a credencial sem imprimir o valor."""
    padroes = {
        "pat": [r"supabase\s*pat", r"sbp_[A-Za-z0-9]+"],
    }[chave]
    for arquivo in (CREDENCIAIS, PROMPT):
        if not arquivo.exists():
            continue
        texto = arquivo.read_text(encoding="utf-8", errors="replace")
        for pad in padroes:
            for m in re.finditer(pad, texto, re.I):
                valor = re.match(r"(\S+)", texto[m.end():].lstrip(": \t"))
                if valor and len(valor.group(1)) > 20:
                    return valor.group(1)
    raise SystemExit(f"'{chave}' nao encontrada")


import httpx  # noqa: E402  (depois de _cred, que pode sair com SystemExit)

H = {"Authorization": f"Bearer {_cred('pat')}", "Content-Type": "application/json"}


def estado() -> dict:
    r = httpx.get(BASE, headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


atual = estado()
faltando = {k: v for k, v in QUERIDO.items() if atual.get(k) != v}

print("MFA agora:", json.dumps(
    {k: atual.get(k) for k in QUERIDO}, ensure_ascii=False))

if not faltando:
    print("ja esta no estado certo; nada a fazer")
    sys.exit(0)

print("corrigindo:", ", ".join(sorted(faltando)))
r = httpx.patch(BASE, headers=H, json=faltando, timeout=30)
if not r.is_success:
    print(r.text[:2000])
    raise SystemExit("a API recusou a mudanca")

depois = estado()
erros = [k for k, v in QUERIDO.items() if depois.get(k) != v]
print("depois:", json.dumps({k: depois.get(k) for k in QUERIDO}, ensure_ascii=False))
if erros:
    raise SystemExit("a API aceitou mas nao aplicou: " + ", ".join(erros))
print("MFA TOTP habilitado e verificado")
