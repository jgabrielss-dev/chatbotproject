"""Contas de usuário: validação do JWT do Supabase Auth e papel (role).

Por que validar chamando o GoTrue em vez de conferir a assinatura do JWT aqui:
    A assinatura exigiria a chave de assinatura do projeto (que muda de chave a
    cada rotação, com o JWKS embutido no token) e mais uma dependência. A
    chamada `GET /auth/v1/user` é uma ida e volta, já que o app faz poucas
    requisições por usuário, e devolve a resposta autoritativa: se a conta foi
    apagada, banida ou o token revogado, o GoTrue recusa. O resultado é cacheado
    por `sub` por `AUTH_CACHE_SEG`, porque o painel consulta a fila a cada 15 s.

A origem da verdade do papel é a tabela `perfis`, e não uma claim do JWT: assim
promover ou bloquear um usuário tem efeito no próximo request, sem esperar o
token expirar, e o cliente não tem como alegar um papel que o servidor não
concorda.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

from app import repositories as repo
from app.config import settings

log = logging.getLogger("auth")

ROLE_ADMIN = "admin"
ROLE_USUARIO = "usuario"

# Niveis de autenticacao do GoTrue. `aal1` e so a senha; `aal2` e senha +
# segundo fator confirmado. Token sem a claim `aal` e `aal1` por padrao --
# e isso que torna o item 5 seguro por padrao, e nao por otimismo.
AAL1 = "aal1"
AAL2 = "aal2"

# Codigo de erro que o navegador reconhece para abrir o campo do segundo
# fator. Texto em portugues seria ambíguo demais para o front confiar nele.
CODIGO_MFA_PENDENTE = "mfa_pendente"

# Tempo que a resposta do GoTrue (e o papel lido do banco) vale para o mesmo
# usuário. Curto o bastante para que bloqueio/promoção valham logo, longo o
# bastante para não transform cada clique do painel em uma ida ao Auth.
AUTH_CACHE_SEG = 60.0

_cache: dict[str, tuple[float, "Usuario"]] = {}


@dataclass(frozen=True)
class Usuario:
    """Quem está chamando a API."""

    id: str
    email: str
    role: str
    bloqueado: bool = False
    nome: str = ""
    aal: str = AAL1
    mfa_ativo: bool = False

    @property
    def eh_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def tem_segundo_fator(self) -> bool:
        """A conta usa 2FA E este token ainda nao o carregou."""
        return self.mfa_ativo and self.aal != AAL2

    @property
    def nome_exibido(self) -> str:
        """Nome de exibição, com o e-mail como reserva.

        O nome em branco é o caso comum das contas antigas (a coluna `nome`
        entrou depois), e mostrar "olá, " vazio numa tela de perfil é pior do
        que mostrar o e-mail.
        """
        return self.nome.strip() or self.email


ADMIN_EMERGENCIA = Usuario(id="admin-token", email="admin@local", role=ROLE_ADMIN)


def id_para_coluna_uuid(usuario: Usuario) -> str | None:
    """`usuario.id` quando ele serve para uma coluna uuid; `None` na emergência.

    A conta do `ADMIN_TOKEN` é um sentinela: não tem linha em `auth.users` e o
    id NÃO é um uuid, de propósito — nada pode gravá-lo em coluna com FK. O
    lado disso é que usá-lo como `dono_id` faz o Postgres devolver 500
    ("invalid input syntax for type uuid"), e essa entrada existe justamente
    para o operador destrancar a plataforma quando ela está com problema.

    `None` é a resposta certa nessas leituras: não existe conta para filtrar, e
    é exatamente o que um admin de verdade recebe em `_dono()`.
    """
    return None if usuario.id == ADMIN_EMERGENCIA.id else usuario.id


def limpar_cache(usuario_id: str | None = None) -> None:
    """Usado pelos testes e depois de mudar o papel/bloqueio de alguém.

    O cache é indexado pelo TOKEN (é o que a requisição traz), então apagar
    "por usuário" precisa varrer as entradas e casar o `Usuario.id` — apagar
    direto por `usuario_id` removia uma chave que nunca existia e a invalidação
    não fazia nada: quem acabava de ser bloqueado ou rebaixado continuava
    entrando pelos próximos 60 s, em toda requisição, com o papel antigo.
    """
    if usuario_id is None:
        _cache.clear()
        return
    alvo = str(usuario_id)
    for token in [t for t, (_, u) in _cache.items() if u.id == alvo]:
        _cache.pop(token, None)


# ---------------------------------------------------------------------------
# Validação do token
# ---------------------------------------------------------------------------

def nivel_do_token(token: str) -> str:
    """Claim `aal` do access_token, sem falar com o GoTrue.

    O payload do JWT é base64url (sem preenchimento) e NÃO é assinatura
    verificada aqui -- e nem precisa ser: quem valida a assinatura é o GoTrue,
    no `_usuario_no_gotrue`. Ler a claim serve só para saber em que nível o
    token foi emitido, e um token falsificado com `aal2` ainda passaria pela
    assinatura antes de chegar aqui.

    Token sem a claim, ou que não seja um JWT, conta como `aal1`. O pior caso
    de errar para baixo é a pessoa pedir o segundo fator de novo; errar para
    cima seria deixar passar quem não tem.
    """
    partes = token.split(".")
    if len(partes) < 2:
        return AAL1
    try:
        bruto = partes[1]
        bruto += "=" * (-len(bruto) % 4)
        dados = json.loads(base64.urlsafe_b64decode(bruto).decode("utf-8", "replace"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return AAL1
    nivel = dados.get("aal")
    return nivel if nivel in (AAL1, AAL2) else AAL1


async def _usuario_no_gotrue(token: str) -> dict | None:
    """Confere o token no Supabase Auth. None = token inválido/expirado."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={"apikey": settings.supabase_anon_key, "Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        # Falha de rede não é token inválido: Distinguir os dois evita que uma
        # indisponibilidade momentânea do Auth jogue todo mundo para fora.
        log.warning("Supabase Auth inacessível ao validar o token: %s", e)
        raise HTTPException(503, "Serviço de login indisponível. Tente de novo em instantes.")

    if r.status_code in (401, 403, 404):
        return None
    if r.status_code >= 500:
        raise HTTPException(503, "Serviço de login indisponível. Tente de novo em instantes.")
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


async def _carregar(usuario_id: str) -> Usuario:
    """Perfil do usuário, criando se o trigger do banco não alcançou a conta."""
    dados = await repo.obter_perfil(usuario_id)
    if dados is None:
        dados = await repo.criar_perfil(usuario_id, "")
    return Usuario(
        id=dados["id"],
        email=dados.get("email") or "",
        role=dados.get("role") or ROLE_USUARIO,
        bloqueado=bool(dados.get("bloqueado")),
        nome=dados.get("nome") or "",
        mfa_ativo=bool(dados.get("mfa_ativo")),
    )


async def usuario_do_token(token: str) -> Usuario | None:
    """Traduz o access_token do navegador em um Usuario, com cache curto."""
    agora = time.monotonic()
    guardado = _cache.get(token)
    if guardado and guardado[0] > agora:
        return guardado[1]

    dados = await _usuario_no_gotrue(token)
    if not dados or not dados.get("id"):
        return None

    usuario = await _carregar(str(dados["id"]))
    if usuario.email != (dados.get("email") or ""):
        # O Auth é a origem do e-mail (o usuário pode ter trocado lá).
        await repo.atualizar_email_perfil(usuario.id, str(dados.get("email") or ""))
        usuario = Usuario(usuario.id, str(dados.get("email") or ""), usuario.role,
                          usuario.bloqueado, usuario.nome, aal=usuario.aal,
                          mfa_ativo=usuario.mfa_ativo)

    # O nível vem do token, não do GoTrue: o `GET /user` só passou acima, então
    # este token é legítimo -- só falta saber se ele já carregou o 2FA.
    if usuario.aal != nivel_do_token(token):
        usuario = Usuario(usuario.id, usuario.email, usuario.role, usuario.bloqueado,
                          usuario.nome, nivel_do_token(token), usuario.mfa_ativo)

    _cache[token] = (agora + AUTH_CACHE_SEG, usuario)
    return usuario


# ---------------------------------------------------------------------------
# Acesso às requisições
# ---------------------------------------------------------------------------

def _token_do_cabecalho(request: Request) -> str:
    cabecalho = request.headers.get("authorization", "")
    if cabecalho.lower().startswith("bearer "):
        return cabecalho[7:].strip()
    return ""


def _token_de_emergencia(request: Request) -> bool:
    """ADMIN_TOKEN: entrada de emergência, só se o operador configurar."""
    if not settings.admin_token:
        return False
    informado = request.headers.get("x-admin-token", "")
    # `compare_digest` e não `==`: comparar segredo com igualdade comum deixa
    # o tempo de resposta vazar o quanto do prefixo acertou.
    return bool(informado) and hmac.compare_digest(
        informado.encode(), settings.admin_token.encode(),
    )


def usuario_atual(request: Request) -> Usuario:
    """Usuário da requisição. Erro 401 se não houver.

    Usado como dependência do FastAPI nas rotas /api/*. A página é pública (é só
    o shell do HTML); os dados é que exigem conta.
    """
    usuario = getattr(request.state, "usuario", None)
    if usuario is None:
        raise HTTPException(
            401,
            "Sessão ausente ou expirada. Faça login novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return usuario


def exigir_admin(usuario: Usuario) -> Usuario:
    """Administrador, ou 403. Não confunda com 401: a sessão é válida, o acesso que não é."""
    if not usuario.eh_admin:
        raise HTTPException(403, "Ação restrita ao administrador.")
    return usuario


def exigir_nao_bloqueado(usuario: Usuario) -> Usuario:
    if usuario.bloqueado:
        raise HTTPException(403, "Conta bloqueada. Fale com o administrador.")
    return usuario


def exigir_segundo_fator(usuario: Usuario) -> Usuario:
    """Barra token de nível 1 de quem tem 2FA ativo. 401, não 403.

    A sessão existe e é válida; o que falta é a prova do segundo fator. O
    corpo é um objeto com `codigo`, para o navegador abrir o campo do código
    em vez de mandar a pessoa para o login de novo.
    """
    if usuario.tem_segundo_fator:
        raise HTTPException(
            401,
            {
                "codigo": CODIGO_MFA_PENDENTE,
                "mensagem": "Informe o código do seu aplicativo autenticador para continuar.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return usuario


def exigir_token_aal2(usuario: Usuario) -> Usuario:
    """Exige token já no nível 2 para mexer na exigência do 2FA.

    É isso que impede registrar o "ativo" sem ter o autenticador: só o GoTrue
    emite `aal2`, e só depois de conferir um código. Sem esta checagem, bastava
    um POST para o campo ficar ligado e a conta trancada para sempre -- ou
    desligado por engano.
    """
    if usuario.aal != AAL2:
        raise HTTPException(
            400,
            {
                "codigo": CODIGO_MFA_PENDENTE,
                "mensagem": "Confirme um código do aplicativo autenticador para alterar "
                            "a verificação em duas etapas.",
            },
        )
    return usuario


async def resolver_usuario(request: Request) -> Usuario | None:
    """Chamado pelo middleware. Deixa None quando não há sessão reconhecível,
    para o middleware responder 401/503 com a mensagem certa."""
    if not settings.has_auth:
        return None
    if _token_de_emergencia(request):
        log.warning("Acesso por ADMIN_TOKEN (modo de emergencia). Revogue assim que normalizar.")
        return ADMIN_EMERGENCIA
    token = _token_do_cabecalho(request)
    if not token:
        return None
    return await usuario_do_token(token)
