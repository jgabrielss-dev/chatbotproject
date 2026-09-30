/* Chats internos da plataforma (itens 7, 12, 13) e o "digitando..." (item 8).
 *
 * Um único arquivo para os três chats, porque o comportamento é o mesmo e não
 * pode divergir entre telas:
 *
 *   * a home (index.html) fala com o agente "produto" — venda, planos e conta;
 *   * o painel abre o gerador de prompt ("prompt") ao lado do campo de
 *     instrução e tem o botão flutuante do "suporte";
 *   * o admin tem o mesmo botão flutuante do "suporte".
 *
 * O que cada página fornece é só o lugar (ids) e o que fazer com ações e
 * prompts gerados. O indicador "digitando..." (item 8) mora aqui e aparece em
 * TODOS os chats: a bolha de três pontos fica na tela do momento em que a
 * pessoa envia até a resposta do agente chegar.
 *
 * Anexo (item 15): o arquivo vira `data:` URL no navegador (FileReader) e viaja
 * como `{mime, nome, base64}`. O servidor decide o tipo (foto, áudio, vídeo,
 * arquivo) e recusa URL externa no canal 'site' — o navegador nunca manda URL.
 *
 * Nenhum caminho absoluto, nenhum prefixo de assets: o mesmo Auth.API_BASE que o
 * auth.js usa, para o Render e o GitHub Pages apontarem para o mesmo backend.
 */
(function () {
  "use strict";

  var CHAVE_SESSAO = "chatbotproject.chat.sessao.";
  var MAX_ANEXOS = 5;                      // espelho de app/midia.py
  var POR_ARQUIVO = 15 * 1024 * 1024;      // espelho de app/midia.py
  var TOTAL = 18 * 1024 * 1024;            // espelho de app/midia.py

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var s = new Date().getTime().toString(16);
    for (var i = 0; i < 4; i++) s += Math.floor(Math.random() * 0xffff).toString(16);
    return "navegador-" + s;
  }

  function sessaoDaConversa(chave) {
    var nome = CHAVE_SESSAO + chave;
    var v = null;
    try { v = localStorage.getItem(nome); } catch (e) { /* modo privado */ }
    if (!v) {
      v = uuid();
      try { localStorage.setItem(nome, v); } catch (e) { /* modo privado */ }
    }
    return v;
  }

  function esc(t) {
    return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function rolarParaBaixo(lista) {
    lista.scrollTop = lista.scrollHeight;
  }

  function bolha(lista, texto, deIa, extra) {
    var b = document.createElement("div");
    b.className = "bolha " + (deIa ? "ia" : "voce") + (extra ? " " + extra : "");
    b.textContent = texto;
    lista.appendChild(b);
    rolarParaBaixo(lista);
    return b;
  }

  /* -------------------------------------------- o indicador do item 8 ------ */

  function digitando(lista, ligar) {
    var t = lista.querySelector(".bolha.digitando");
    if (ligar) {
      if (t) return;
      var b = document.createElement("div");
      b.className = "bolha ia digitando";
      b.innerHTML = "<span></span><span></span><span></span>";
      lista.appendChild(b);
      rolarParaBaixo(lista);
    } else if (t) {
      t.remove();
    }
  }

  /* ------------------------------------------------------------------ POST -- */

  function postar(chave, corpo) {
    var cab = { "Content-Type": "application/json" };
    var auth = Auth.cabecalhoAuth();
    for (var k in auth) if (Object.prototype.hasOwnProperty.call(auth, k)) cab[k] = auth[k];
    return fetch(Auth.API_BASE + "/api/interno/" + chave, {
      method: "POST",
      headers: cab,
      body: JSON.stringify(corpo),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.ok) return d;
        var msg = (d && d.detail)
          ? (typeof d.detail === "string" ? d.detail : "Pedido recusado pelo servidor.")
          : "Erro " + r.status + " do servidor.";
        var err = new Error(msg);
        err.status = r.status;
        throw err;
      });
    }).catch(function (e) {
      if (e && e.status) throw e;
      if (!e || e.name !== "TypeError") throw e;
      throw new Error("Servidor fora do ar. Tente de novo em instantes.");
    });
  }

  function carregarHistorico(chat) {
    if (!chat.opcoes.comHistorico) return Promise.resolve();
    // Mesmo cabecalho do `postar`: o historico de um atendente que exige conta
    // (o suporte do painel, item 13) responde 401 sem isso — e o `!r.ok`
    // abaixo devolvia lista vazia em silencio, entao o F5 apagava a conversa
    // na tela sem nenhuma mensagem de erro.
    return fetch(Auth.API_BASE + "/api/interno/" + chat.chave
                 + "/historico?sessao=" + encodeURIComponent(chat.sessao),
                 { headers: Auth.cabecalhoAuth() })
      .then(function (r) {
        if (!r.ok) return { mensagens: [] };
        return r.json();
      })
      .then(function (d) {
        var msgs = (d && d.mensagens) ? d.mensagens : [];
        for (var i = 0; i < msgs.length; i++) {
          bolha(chat.lista, msgs[i].texto, !!msgs[i].de_ia);
        }
      })
      .catch(function () {
        // Histórico é extra: sem rede, a conversa começa do zero e ninguém fica
        // travado na tela por causa disso.
      });
  }

  /* ------------------------------------------------------------- anexos ---- */

  function chipDeAnexo(chat, f, onRemover) {
    var area = chat.chipsArea;
    if (!area) return;
    var s = document.createElement("span");
    s.className = "chat-anexo";
    var nome = document.createElement("span");
    nome.textContent = f.nome;
    var x = document.createElement("button");
    x.className = "chat-anexo-x";
    x.textContent = "×";
    x.title = "Remover anexo";
    x.onclick = function () {
      chat.anexos = chat.anexos.filter(function (a) { return a !== f; });
      s.remove();
      onRemover();
    };
    s.appendChild(nome);
    s.appendChild(x);
    area.appendChild(s);
  }

  function anexar(chat, arquivo) {
    if (chat.anexos.length >= MAX_ANEXOS) {
      bolha(chat.lista, "Limite de " + MAX_ANEXOS + " anexos por mensagem.", true, "erro");
      return;
    }
    if (arquivo.size > POR_ARQUIVO) {
      bolha(chat.lista, arquivo.name + " tem mais de 15 MB: o limite de cada arquivo.", true, "erro");
      return;
    }
    var total = chat.anexos.reduce(function (s, a) { return s + a.tamanho; }, 0) + arquivo.size;
    if (total > TOTAL) {
      bolha(chat.lista, "Os anexos desta mensagem passam de 18 MB no total.", true, "erro");
      return;
    }
    var leitor = new FileReader();
    leitor.onerror = function () {
      bolha(chat.lista, "Não consegui ler " + arquivo.name + ". Tente de novo.", true, "erro");
    };
    leitor.onload = function () {
      var f = { nome: arquivo.name, mime: arquivo.type || "application/octet-stream",
                base64: String(leitor.result), tamanho: arquivo.size };
      chat.anexos.push(f);
      chipDeAnexo(chat, f, function () {
        if (chat.anexarBotao) chat.anexarBotao.toggleAttribute("data-tem-anexo",
          chat.anexos.length > 0);
      });
      if (chat.anexarBotao) chat.anexarBotao.setAttribute("data-tem-anexo", "1");
    };
    leitor.readAsDataURL(arquivo);
  }

  /* -------------------------------------------------------------- envio ---- */

  function enviar(chat) {
    if (chat.ocupado) return;
    var texto = (chat.campo.value || "").trim();
    if (!texto && !chat.anexos.length) return;
    var anexos = chat.anexos.map(function (a) {
      return { nome: a.nome, mime: a.mime, base64: a.base64 };
    });

    chat.ocupado = true;
    chat.enviarBotao.disabled = true;
    chat.campo.disabled = true;
    if (chat.anexarBotao) chat.anexarBotao.disabled = true;

    if (texto) bolha(chat.lista, texto, false);
    for (var i = 0; i < anexos.length; i++) {
      bolha(chat.lista, "📎 " + anexos[i].nome, false);
    }
    chat.campo.value = "";
    chat.anexos = [];
    if (chat.chipsArea) chat.chipsArea.innerHTML = "";  // reseta ao enviar (item 15)
    if (chat.anexarBotao) chat.anexarBotao.removeAttribute("data-tem-anexo");

    digitando(chat.lista, true);

    postar(chat.chave, { sessao: chat.sessao, texto: texto, anexos: anexos })
      .then(function (r) {
        digitando(chat.lista, false);
        if (r.resposta) bolha(chat.lista, r.resposta, true);
        if (r.prompt_gerado) {
          chat.ultimoPrompt = r.prompt_gerado;
          if (chat.opcoes.aoPromptGerado) chat.opcoes.aoPromptGerado(r.prompt_gerado);
        }
        if (r.acao && chat.opcoes.aoAcao) {
          chat.opcoes.aoAcao(r.acao, chat.lista, r);
        }
      })
      .catch(function (e) {
        digitando(chat.lista, false);
        if (e && e.status === 401) {
          bolha(chat.lista, "Sua sessão expirou. Entre de novo para continuar.", true, "erro");
          if (chat.opcoes.aoSemSessao) chat.opcoes.aoSemSessao();
          else if (typeof Auth.irParaLogin === "function") Auth.irParaLogin();
        } else {
          bolha(chat.lista, "⚠ " + (e && e.message ? e.message : "Tente de novo."), true, "erro");
        }
      })
      .then(function () {
        chat.ocupado = false;
        chat.enviarBotao.disabled = false;
        chat.campo.disabled = false;
        if (chat.anexarBotao) chat.anexarBotao.disabled = false;
        chat.campo.focus();
      });
  }

  function autosize(campo) {
    campo.style.height = "auto";
    campo.style.height = Math.min(campo.scrollHeight + 2, 120) + "px";
  }

  function iniciar(opcoes) {
    var chat = {
      chave: opcoes.chave,
      opcoes: opcoes,
      lista: document.getElementById(opcoes.lista),
      campo: document.getElementById(opcoes.campo),
      enviarBotao: document.getElementById(opcoes.enviar),
      anexarBotao: opcoes.botaoAnexar
        ? document.getElementById(opcoes.botaoAnexar) : null,
      chipsArea: opcoes.chips ? document.getElementById(opcoes.chips) : null,
      sessao: sessaoDaConversa(opcoes.chave),
      anexos: [],
      ocupado: false,
      ultimoPrompt: null,
    };
    if (!chat.lista || !chat.campo || !chat.enviarBotao) return null;

    chat.enviarBotao.onclick = function () { enviar(chat); };
    chat.campo.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        enviar(chat);
      }
    });
    chat.campo.addEventListener("input", function () { autosize(chat.campo); });

    if (opcoes.arquivo && opcoes.botaoAnexar) {
      var entrada = document.getElementById(opcoes.arquivo);
      chat.anexarBotao.onclick = function () {
        if (!chat.ocupado) entrada.click();
      };
      entrada.onchange = function () {
        var arquivos = entrada.files ? Array.prototype.slice.call(entrada.files) : [];
        for (var i = 0; i < arquivos.length; i++) anexar(chat, arquivos[i]);
        entrada.value = "";
      };
    }

    carregarHistorico(chat);
    return chat;
  }

  window.Chat = {
    iniciar: iniciar,
  };
})();