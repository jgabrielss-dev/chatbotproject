from __future__ import annotations

import asyncio
import logging

from app import midia
from app import repositories as repo
from app.ai import gemini
from app.channels import evolution, meta_oficial, telegram

log = logging.getLogger("pipeline")

#: Tempo de download de um anexo. Maior que o do Gemini (45 s) porque a
#: requisição dele é só o texto: aqui entra a rede de fora, e um vídeo de 15 MB
#: numa conexão lenta é lento. Cortar antes de terminar é jogar a mensagem fora.
TIMEOUT_ANEXO_SEG = 90.0


async def _baixar_anexo(canal: dict, m: midia.Midia) -> tuple[str, bytes]:
    """Baixa os bytes de um anexo, usando as credenciais do canal.

    Devolve (mime, bytes) ou levanta. Três caminhos, porque cada canal tem a sua
    forma de dar a chave do arquivo:

    - `base64` já vem no corpo: decodifica, sem rede.
    - `url` vem pronta (Evolution, webhook genérico): GET direto.
    - `ref` precisa do token do canal: o Telegram devolve `file_id` e a URL só
      sai depois de um `getFile`; a Meta devolve `id` e a URL só sai da Graph.

    Um anexo de 200 MB de link externo é barrado DEPOIS do download, em
    `_resolver_anexos`, e não aqui: descobrir o tamanho antes exigiria um HEAD em
    toda URL, e servidor que não responde a HEAD não é raro.
    """
    fonte = m.fonte
    if "base64" in fonte:
        dados = midia.decodificar(str(fonte["base64"]))
        if dados is None:
            raise ValueError("base64 invalido")
        return m.mime, dados

    if "url" in fonte:
        # O chat interno (itens 7, 12, 13) chega por este caminho e a URL é
        # escrita pelo próprio visitante, sem nenhum canal por trás:aceder a
        # `http://169.254.169.254/` daqui seria o servidor pedindo a
        # configuração dele para quem não tem conta. O navegador manda os
        # arquivos como `data:` ou base64, que é o ramo de cima; se alguém
        # mandar URL mesmo assim, o anexo é recusado e a conversa continua.
        if (canal or {}).get("tipo") == "site":
            # `data:` nao sai da maquina: o navegador le o arquivo e entrega os
            # bytes na propria mensagem. Aceitar so essa forma e recusar o resto.
            bruto = str(fonte["url"])
            if bruto.startswith("data:"):
                dados = midia.decodificar(bruto)
                if dados is None:
                    raise ValueError("base64 invalido")
                return m.mime, dados
            raise ValueError("o chat do site so aceita arquivo enviado na propria mensagem")
        import httpx

        async with httpx.AsyncClient(timeout=TIMEOUT_ANEXO_SEG, follow_redirects=True) as http:
            resposta = await http.get(str(fonte["url"]))
        resposta.raise_for_status()
        dados = resposta.content
        # O content-type do servidor ganha: link de "download" costuma mandar
        # application/octet-stream para o que é um JPEG, e aí o modelo receberia
        # bytes de imagem declarados como arquivo sem tipo.
        mime = (str(resposta.headers.get("content-type") or m.mime)
                .split(";")[0].strip() or m.mime)
        return mime, dados

    ref = str(fonte.get("ref") or "")
    tipo_canal = (canal or {}).get("tipo", "")
    cfg = (canal or {}).get("config") or {}
    if tipo_canal == "telegram":
        return await telegram.baixar_anexo(cfg.get("token", ""), ref)
    if tipo_canal in ("whatsapp_oficial", "instagram_oficial"):
        return await meta_oficial.baixar_anexo(cfg, tipo_canal, ref)
    if tipo_canal == "whatsapp":
        return await evolution.baixar_anexo(
            cfg.get("instance_name", ""), ref,
        )
    raise ValueError(f"canal {tipo_canal} nao devolve arquivo por ref")


async def _resolver_anexos(
    canal: dict, anexos: list[midia.Midia]
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Baixa o que der. Devolve (prontos, falhas).

    Um anexo que falha NÃO derruba a resposta. Perder a foto é ruim; perder a
    mensagem é pior, e a alternativa (deixar a exceção subir) faz o worker
    devolver o mesmo item para a fila para sempre, consumindo cota. O que falhou
    vira texto, e o prompt de sistema manda o agente dizer isso em vez de
    inventar o conteúdo.
    """
    prontos: list[tuple[str, bytes]] = []
    falhas: list[str] = []
    total = 0
    for m in anexos:
        try:
            mime, dados = await asyncio.wait_for(_baixar_anexo(canal, m),
                                                 TIMEOUT_ANEXO_SEG)
        except Exception as e:
            log.warning("Nao consegui baixar o anexo %s: %s", m.nome, str(e)[:120])
            falhas.append(f"{m.nome} ({m.mime}): {str(e)[:80] or 'falha no download'}")
            continue
        if not dados:
            falhas.append(f"{m.nome} ({m.mime}): veio vazio")
            continue
        tamanho = len(dados)
        if tamanho > midia.LIMITE_POR_ARQUIVO:
            falhas.append(
                f"{m.nome} ({m.mime}): {midia.tamanho_legivel(tamanho)}, o limite e "
                f"{midia.tamanho_legivel(midia.LIMITE_POR_ARQUIVO)}")
            continue
        if total + tamanho > midia.LIMITE_TOTAL:
            falhas.append(
                f"{m.nome} ({m.mime}): o conjunto passaria de "
                f"{midia.tamanho_legivel(midia.LIMITE_TOTAL)}")
            continue
        total += tamanho
        prontos.append((mime, dados))
    return prontos, falhas


def _texto_com_aviso(registro: str, falhas: list[str]) -> str:
    """Junta a mensagem com o que não chegou.

    A lista vai na mensagem, e não no prompt de sistema: é informação sobre ESTE
    turno. No prompt seria mentira (ele diria que o anexo não leu em toda
    conversa) e o modelo não teria como saber a qual mensagem se refere.
    """
    if not falhas:
        return registro
    linhas = "\n".join(f"- {f}" for f in falhas)
    return f"{registro}\n\n[Anexos que NAO chegaram ate mim]\n{linhas}"


#: Nomes públicos das duas funções acima. Os chats internos (itens 7, 12 e 13)
#: precisam do mesmo comportamento de anexo — é o item 15 pedindo "também nos
#: chats internos do site" — e o caminho certo para isso é o MESMO caminho, não
#: uma cópia. Chamar o privado daqui seria duas linhas de `from ... import
#: _resolver_anexos` que denunciam que o certo era expor.
async def resolver_anexos(canal: dict, anexos: list[midia.Midia]) -> tuple[list, list]:
    return await _resolver_anexos(canal, anexos)


def texto_com_aviso(registro: str, falhas: list[str]) -> str:
    return _texto_com_aviso(registro, falhas)


async def processar_mensagem(
    agente: dict,
    canal: dict,
    usuario_externo: str,
    texto: str,
    anexos: list[midia.Midia] | None = None,
) -> str:
    """Fluxo completo: sessão -> salvar user -> Gemini(hist+memoria) -> salvar IA -> memória.

    `anexos` é a lista normalizada de `app/midia.py` (item 15). O que vai para
    `mensagens.texto` é a descrição do que chegou, não os bytes: o histórico fica
    legível (e barato) mesmo depois de um áudio de 3 minutos, e a pessoa vê na
    tela o que mandou.
    """
    sessao = await repo.obter_ou_criar_sessao(agente["id"], canal["id"], usuario_externo)

    anexos = anexos or []
    registro = (texto or "").strip() or midia.descrever(anexos)
    if not registro:
        # `caixa_entrada.texto` é NOT NULL e uma mensagem só com sticker de
        # Telegram chega aqui sem texto nenhum. Registrar algo é melhor do que
        # deixar a mensagem existir só na fila.
        registro = "(mensagem sem texto e sem anexo reconhecivel)"
    await repo.salvar_mensagem(sessao["id"], False, registro)

    # A cota é do DONO do agente, não da sessão: o item 9 fala em limite mensal
    # por agente, e o dono é quem tem assinatura. `dono_id` nulo é agente legado
    # sem dono (só o admin responde por ele), e aí não há o que cobrar.
    await repo.registrar_consumo(agente.get("dono_id"), agente["id"])

    historico = await repo.historico_sessao(sessao["id"])
    memoria = gemini.ler_memoria(sessao.get("memoria"))

    baixados, falhas = await _resolver_anexos(canal, anexos)
    resposta = await gemini.responder(
        agente["system_prompt"], historico, _texto_com_aviso(registro, falhas),
        memoria, baixados,
    )
    await repo.salvar_mensagem(sessao["id"], True, resposta)

    trechos = await repo.ultimos_trechos(sessao["id"], 6)
    nova_memoria = await gemini.atualizar_memoria(agente["system_prompt"], memoria, trechos)
    if nova_memoria != memoria:
        await repo.atualizar_memoria(sessao["id"], nova_memoria)

    return resposta
