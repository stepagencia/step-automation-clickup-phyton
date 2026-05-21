"""
STEP - Sincronização automática de Reels Teste
==============================================

Quando uma tarefa na lista Agendamentos do ClickUp recebe a etiqueta
`reels teste`, este script cria uma cópia dessa tarefa, na MESMA lista,
com:

  - Nome: '[REELS TESTE] ' + nome original (com colchetes literais)
  - Custom fields copiados: Cliente, Editorias, Link do Post, Tipo,
    Rede Social
  - Custom fields em branco: Copy, Design, Pessoas (não tocamos)
  - Status: 'agendar postagem'
  - Comentário fixo orientando a publicação como Reels teste
  - Tarefa SOLTA na lista (sem parent, NÃO é subtarefa)

A tarefa original recebe a tag `reels teste processado` para não ser
reprocessada na próxima execução do cron. A tag `reels teste` original
é mantida (histórico).

Performance
-----------
Só considera tarefas atualizadas nos últimos LOOKBACK_MONTHS meses
(default 6) para não varrer histórico antigo.

Segurança
---------
- API token vem da env var CLICKUP_API_TOKEN
- DRY_RUN=1 → só loga o que faria, sem tocar na API
- Idempotente: tag `reels teste processado` impede reprocessamento
- Em caso de falha ao criar a nova tarefa, a original NÃO é marcada
  como processada (a próxima execução tenta de novo)

Como rodar localmente:
    export CLICKUP_API_TOKEN="pk_..."
    export DRY_RUN=1            # opcional, pra teste seco
    python reels_teste_sync.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuração — IDs do workspace da STEP
# ---------------------------------------------------------------------------

WORKSPACE_ID = "9013038195"
LIST_AGENDAMENTOS = "901306281642"

# Etiquetas (tags)
TAG_REELS_TESTE = "reels teste"
TAG_REELS_TESTE_PROCESSADO = "reels teste processado"

# Custom field IDs (globais do workspace) — os campos que devem ser copiados
CF_CLIENTE = "e41a916f-7818-44b6-9e93-fb003f52ad53"
CF_EDITORIAS = "7a155e2e-5b70-467c-894f-98f7f4cc1722"
CF_LINK_POST = "3ee94567-b2f1-4819-91f9-726fcb4378c0"
CF_TIPO = "4fc73c67-8c6e-4e73-ad8e-885df6586260"
CF_REDE_SOCIAL = "5293fb4f-2741-4aab-bb1c-518e9e1d2030"
CF_LEGENDA = "322837ee-3eba-41a8-8a5e-82b61fa15366"

COPIABLE_FIELDS = [
    CF_CLIENTE,
    CF_EDITORIAS,
    CF_LINK_POST,
    CF_TIPO,
    CF_REDE_SOCIAL,
    CF_LEGENDA,
]

# Dropdowns precisam ser setados via UUID da opção, não pelo orderindex bruto
DROPDOWN_FIELDS = {CF_CLIENTE, CF_EDITORIAS, CF_TIPO, CF_REDE_SOCIAL}

# Campos URL: rede de segurança — algumas integrações do ClickUp aceitam
# melhor o valor envelopado em {"value": "..."}, embora a observação atual
# seja que strings cruas funcionam. Tratamos via fluxo dedicado pra cobrir
# eventual falha intermitente.
URL_FIELDS = {CF_LINK_POST}

# Status da nova tarefa — ClickUp aceita o nome em minúsculo no payload
NEW_TASK_STATUS = "agendar postagem"

# Comentário fixo a ser adicionado na nova tarefa
COMMENT_TEXT = (
    "Fazer o agendamento desse conteúdo no formato de Reels teste pelo "
    "Facebook. Importante: esse Reels é publicado somente para a base de "
    "não seguidores."
)

# Janela de busca — só olha tarefas atualizadas nos últimos N meses
LOOKBACK_MONTHS = 6
LOOKBACK_MS = LOOKBACK_MONTHS * 30 * 24 * 60 * 60 * 1000  # aproximado

# ---------------------------------------------------------------------------
# Cliente ClickUp
# ---------------------------------------------------------------------------

log = logging.getLogger("reels_teste_sync")

API_BASE = "https://api.clickup.com/api/v2"


class ClickUp:
    """Wrapper sobre a API do ClickUp com retry e DRY_RUN."""

    def __init__(self, token: str, dry_run: bool = False) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })
        self.dry_run = dry_run

    def _req(self, method: str, path: str, *, write: bool = False,
             **kwargs: Any) -> Any:
        if write and self.dry_run:
            log.info("    [DRY_RUN] %s %s %s", method, path,
                     kwargs.get("json") or kwargs.get("params") or "")
            # Em dry-run retornamos um stub plausível para create_task
            if method == "POST" and "/task" in path and path.endswith("/task"):
                return {"id": "DRYRUN_NEW_TASK", "name": "[dry-run]"}
            return {}
        for attempt in range(3):
            resp = self.session.request(method, f"{API_BASE}{path}",
                                        timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, aguardando %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("ClickUp %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json() if resp.text else None
        resp.raise_for_status()

    # ----- Leitura -----

    def filter_team_tasks(self, list_ids: list[str],
                          tags: Optional[list[str]] = None,
                          date_updated_gt: Optional[int] = None) -> list[dict]:
        """Filtered search via /team/{team_id}/task. Tarefas publicadas ficam
        'closed'; include_closed=true é obrigatório. date_updated_gt (unix ms):
        se fornecido, só retorna tarefas atualizadas depois desse timestamp."""
        out: list[dict] = []
        page = 0
        params: list[tuple[str, Any]] = [
            ("subtasks", "true"),
            ("include_closed", "true"),
            ("archived", "false"),
        ]
        for lid in list_ids:
            params.append(("list_ids[]", lid))
        for tag in (tags or []):
            params.append(("tags[]", tag))
        if date_updated_gt is not None:
            params.append(("date_updated_gt", date_updated_gt))

        while True:
            page_params = params + [("page", page)]
            data = self._req("GET", f"/team/{WORKSPACE_ID}/task",
                             params=page_params)
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1
        return out

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/task/{task_id}",
                         params={"include_subtasks": "false"})

    # ----- Escrita -----

    def create_task(self, list_id: str, payload: dict) -> dict:
        return self._req("POST", f"/list/{list_id}/task",
                         json=payload, write=True)

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> None:
        self._req("POST", f"/task/{task_id}/field/{field_id}",
                  json={"value": value}, write=True)

    def add_tag(self, task_id: str, tag: str) -> None:
        self._req("POST", f"/task/{task_id}/tag/{tag}", write=True)

    def add_comment(self, task_id: str, text: str, notify_all: bool = False) -> None:
        self._req("POST", f"/task/{task_id}/comment",
                  json={"comment_text": text, "notify_all": notify_all},
                  write=True)

    def link_tasks(self, task_id: str, links_to: str) -> None:
        """Cria link bidirecional entre duas tarefas (relação lateral,
        não parent/child). Endpoint: POST /task/{id}/link/{outro_id}."""
        self._req("POST", f"/task/{task_id}/link/{links_to}", write=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cf_value(task: dict, field_id: str) -> Any:
    """Valor bruto de um custom field. Cuidado: 0 e '' são válidos."""
    for cf in task.get("custom_fields", []):
        if cf["id"] == field_id:
            return cf.get("value")
    return None


def dropdown_option_id(task: dict, field_id: str) -> Optional[str]:
    """UUID da opção selecionada num dropdown. Trata `value is None`
    explicitamente (0 é orderindex válido)."""
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        val = cf.get("value")
        if val is None:
            return None
        options = cf.get("type_config", {}).get("options", [])
        if isinstance(val, int):
            if 0 <= val < len(options):
                return options[val]["id"]
            return None
        if isinstance(val, str):
            return val or None
    return None


def tag_names(task: dict) -> set[str]:
    return {t["name"] for t in task.get("tags", [])}


def apply_custom_fields(cu: ClickUp, task_id: str, source: dict) -> None:
    """Copia os campos da tarefa original para a nova, um por um."""
    for field_id in COPIABLE_FIELDS:
        raw = cf_value(source, field_id)
        if raw is None or raw == "":
            continue
        try:
            if field_id in DROPDOWN_FIELDS:
                opt_id = dropdown_option_id(source, field_id)
                if opt_id:
                    cu.set_custom_field(task_id, field_id, opt_id)
            elif field_id in URL_FIELDS:
                # Rede de segurança para campos URL: 2 tentativas com backoff.
                # Cobre falhas intermitentes da API observadas em execução
                # (mesma rodada do cron criou 5 cópias com Link e 3 sem).
                last_exc = None
                for attempt in range(2):
                    try:
                        cu.set_custom_field(task_id, field_id, raw)
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt == 0:
                            log.warning("    URL %s falhou (tentativa 1), "
                                        "tentando de novo: %s", field_id, exc)
                            time.sleep(1)
                if last_exc:
                    raise last_exc
            else:
                cu.set_custom_field(task_id, field_id, raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("    Falha ao preencher %s em %s: %s",
                        field_id, task_id, exc)


# ---------------------------------------------------------------------------
# Fluxo principal
# ---------------------------------------------------------------------------

def process_reels_teste(cu: ClickUp, candidates: list[dict]) -> None:
    """Para cada tarefa com 'reels teste' mas SEM 'reels teste processado',
    cria a cópia [REELS TESTE] na lista Agendamentos."""
    pendentes = [
        t for t in candidates
        if TAG_REELS_TESTE in tag_names(t)
        and TAG_REELS_TESTE_PROCESSADO not in tag_names(t)
    ]
    log.info("FLUXO: %d tarefa(s) a processar", len(pendentes))

    for orig_summary in pendentes:
        orig_id = orig_summary["id"]
        try:
            # get_task pra garantir custom_fields completos
            orig = cu.get_task(orig_id)
            create_reels_teste_copy(cu, orig)
        except Exception as exc:  # noqa: BLE001
            log.exception("Falhou ao processar %s: %s", orig_id, exc)


def create_reels_teste_copy(cu: ClickUp, orig: dict) -> None:
    orig_id = orig["id"]
    orig_name = orig["name"]

    # Defesa em profundidade — o filter já exclui processadas, mas re-checamos
    # na tarefa hidratada (caso o estado tenha mudado entre o filter e o get).
    if TAG_REELS_TESTE_PROCESSADO in tag_names(orig):
        log.info("  %s já tem 'reels teste processado' — pulando", orig_id)
        return

    new_name = f"[REELS TESTE] {orig_name}"

    # --- Cria nova tarefa solta na lista Agendamentos ---
    # Status no payload de criação: ClickUp aceita o nome em minúsculo.
    # Se 'agendar postagem' não existir na lista, o POST retorna 400 e
    # abortamos sem marcar a original como processada.
    new_payload: dict[str, Any] = {
        "name": new_name,
        "status": NEW_TASK_STATUS,
    }
    try:
        new_task = cu.create_task(LIST_AGENDAMENTOS, new_payload)
    except Exception as exc:  # noqa: BLE001
        log.error("FALHA ao criar cópia de %s: %s. Original NÃO marcada "
                  "como processada — rode de novo após corrigir.",
                  orig_id, exc)
        return
    new_id = (new_task or {}).get("id")
    if not new_id:
        log.error("FALHA: create_task não retornou ID pra original %s. "
                  "NÃO marcada como processada.", orig_id)
        return
    log.info("  Cópia criada: %s (%s)", new_id, new_name)

    # --- Copia os 5 custom fields ---
    apply_custom_fields(cu, new_id, orig)

    # --- Adiciona o comentário fixo ---
    try:
        cu.add_comment(new_id, COMMENT_TEXT)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao adicionar comentário em %s: %s", new_id, exc)

    # --- Vincula original ↔ cópia (link bidirecional do ClickUp) ---
    # Se a vinculação falhar, NÃO marcamos a original como processada.
    # Assim a próxima execução do cron pode tentar de novo. Mesma defesa
    # usada para falha de create_task.
    try:
        cu.link_tasks(orig_id, new_id)
        log.info("  Original %s vinculada à cópia %s", orig_id, new_id)
    except Exception as exc:  # noqa: BLE001
        log.error("FALHA ao vincular original %s à cópia %s: %s. "
                  "Original NÃO marcada como processada — rode de novo após "
                  "corrigir.", orig_id, new_id, exc)
        return

    # --- Marca a ORIGINAL como processada (anti-loop) ---
    # Só chega aqui se a criação + link deram certo. Tag 'reels teste'
    # original é mantida (histórico).
    try:
        cu.add_tag(orig_id, TAG_REELS_TESTE_PROCESSADO)
        log.info("  Original %s marcada como 'reels teste processado'",
                 orig_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao adicionar 'reels teste processado' em %s: %s. "
                    "ATENÇÃO: a cópia foi criada — na próxima execução pode "
                    "duplicar se a tag não for adicionada manualmente.",
                    orig_id, exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        log.error("CLICKUP_API_TOKEN ausente.")
        return 1

    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    if dry_run:
        log.info("*** MODO DRY_RUN: nenhuma escrita será feita ***")

    cu = ClickUp(token, dry_run=dry_run)

    try:
        now_ms = int(time.time() * 1000)
        date_gt = now_ms - LOOKBACK_MS
        log.info("Pré-carregando tarefas (últimos %d meses)...", LOOKBACK_MONTHS)

        candidates = cu.filter_team_tasks(
            list_ids=[LIST_AGENDAMENTOS],
            tags=[TAG_REELS_TESTE],
            date_updated_gt=date_gt,
        )
        log.info("  Tarefas com 'reels teste' em Agendamentos: %d",
                 len(candidates))

        process_reels_teste(cu, candidates)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro fatal: %s", exc)
        return 1

    log.info("Sincronização concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
