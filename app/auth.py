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

    @property
    def eh_admin(self) -> bool:
        return self.role == ROLE_ADMIN


ADMIN_EMERGENCIA = Usuario(id="admin-token", email="admin@local", role=ROLE_ADMIN)


def limpar_cache(usuario_id: str | None = None) -> None:
    """Usado pelos testes e depois de mudar o papel de alguém."""
    if usuario_id is None:
        _cache.clear()
    else:
        _cache.pop(usuario_id, None)


# ---------------------------------------------------------------------------
# Validação do token
# ---------------------------------------------------------------------------

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
    linha = await repo.obter_perfil(usuario_id)
    if linha is None:
        linha = await repo.criar_perfil(usuario_id, "")
    return Usuario(
        id=linha["id"],
        email=linha.get("email") or "",
        role=linha.get("role") or ROLE_USUARIO,
        bloqueado=bool(linha.get("bloqueado")),
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
        usuario = Usuario(usuario.id, str(dados.get("email") or ""), usuario.role, usuario.bloqueado)

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
    return bool(informado) and informado == settings.admin_token


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
